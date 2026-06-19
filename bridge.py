#!/usr/bin/env python3
"""Cradlewise crib → local RTSP bridge (for Frigate / any NVR ingest).

The Cradlewise crib has no local video stream: its live feed is a **Janus
videoroom** published to Cradlewise's cloud SFU (see ``cradlewise/video.py``).
Home Assistant's ``camera.cradlewise_*`` entity subscribes to it *on demand* and
re-broadcasts over WebRTC. An NVR like Frigate, however, needs a continuous
pullable RTSP source.

This bridge discovers **every crib on the account** and holds one always-on
Janus subscriber session per crib (via the vendored
``cradlewise.CradlewiseVideoClient``), re-publishing each received H.264 video
(and, optionally, the crib's audio) as its own local RTSP path:

    crib → Cradlewise cloud Janus (SFU) → [this bridge] → mediamtx RTSP
         → your NVR pulls rtsp://<host>:8554/cradlewise_<baby_name>

Because Janus is an SFU, the crib only ever uploads one feed regardless of how
many subscribers exist; we are the only continuous subscriber.

The ``videoRoom`` REST endpoint only accepts a **provisioned** device_id (a raw
string returns ``501 Not Implemented``), so we ``provision_device`` once at
startup using ``CRADLEWISE_DEVICE_NAME`` and reuse the server-assigned device_id
for every session (provisioning is idempotent per name). NOTE: a Cradlewise
account has a device-slot limit; if provisioning a brand-new name returns
``422 DEVICE_ASSIGNMENT_FAILED``, reuse an existing device-name (e.g. the one the
HA integration already registered) — but then don't run both against it at once.

Run as the publisher process of a co-located mediamtx (``runOnInit`` in
``mediamtx.yml``), which serves the RTSP that the NVR pulls and restarts this
process if it exits.

⚠️ If the crib does not publish 24/7 with no app/HA viewer present, ``start()``
raises "No publishers in the video room yet"; the supervisor below retries with
backoff, so recording auto-recovers whenever the crib is publishing again (gaps
are expected in that case).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
from pathlib import Path

from aiortc.contrib.media import MediaRecorder, MediaRelay

from cradlewise import (
    CradlewiseAuth,
    CradlewiseClient,
    CradlewiseVideoClient,
    get_app_config,
)
from cradlewise.certs import provision_device

_LOG = logging.getLogger("cradlewise-bridge")

# ── config (env) ──────────────────────────────────────────────────────────────
EMAIL = os.environ["CRADLEWISE_EMAIL"]
PASSWORD = os.environ["CRADLEWISE_PASSWORD"]
# Device NAME used to provision a stable, registered device_id (the videoRoom
# endpoint 501s for an unregistered id). CRADLEWISE_DEVICE_ID is a legacy alias.
DEVICE_NAME = (
    os.getenv("CRADLEWISE_DEVICE_NAME")
    or os.getenv("CRADLEWISE_DEVICE_ID")
    or "cradlewise-rtsp-bridge"
)
# Base RTSP target inside the container; each crib is published to
# "<RTSP_BASE>/cradlewise_<baby_name>" (the mediamtx all_others path accepts any).
RTSP_BASE = os.getenv("RTSP_BASE", "rtsp://127.0.0.1:8554").rstrip("/")
CACHE_DIR = Path(os.getenv("CRADLEWISE_CACHE", "/cache"))
WANT_AUDIO = os.getenv("CRADLEWISE_AUDIO", "1").lower() not in ("0", "false", "no")
START_TIMEOUT = float(os.getenv("CRADLEWISE_START_TIMEOUT", "120"))
# No-frame watchdog: if the upstream stalls this long, tear down and reconnect.
STALL_TIMEOUT = float(os.getenv("CRADLEWISE_STALL_TIMEOUT", "30"))
RETRY_MIN = float(os.getenv("CRADLEWISE_RETRY_MIN", "5"))
RETRY_MAX = float(os.getenv("CRADLEWISE_RETRY_MAX", "60"))


def _slug(value: str | None) -> str:
    """Lowercase, collapse non-alphanumerics to single underscores, strip ends."""
    return re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")


def _assign_paths(cradles: dict[str, object]) -> dict[str, str]:
    """Map each cradle_id → a unique RTSP path ``cradlewise_<baby_name>``.

    Falls back to the cradle_id when a crib has no baby_name, and disambiguates
    collisions (e.g. two babies with the same name) with a short cradle_id suffix.
    """
    paths: dict[str, str] = {}
    used: set[str] = set()
    for cradle in cradles.values():
        base = _slug(cradle.baby_name) or _slug(cradle.cradle_id) or "crib"
        name = base
        if name in used:
            suffix = (cradle.cradle_id or "").replace("-", "")[:8] or str(len(used))
            name = f"{base}_{suffix}"
        used.add(name)
        paths[cradle.cradle_id] = f"cradlewise_{name}"
    return paths


async def _setup() -> tuple[CradlewiseClient, dict[str, object], str]:
    """One-time: auth, discover ALL cribs, and provision a registered device_id.

    Returns (client, cradles, device_id). Provisioning is idempotent for
    DEVICE_NAME (same device_id each call); we run it once per distinct baby_id so
    the single device is paired with every baby, then reuse that device_id for all
    sessions — re-running per reconnect would needlessly re-hit the backend / S3.
    """
    app_config = await get_app_config(cache_dir=CACHE_DIR)
    auth = CradlewiseAuth(EMAIL, PASSWORD, app_config)
    await auth.authenticate()
    client = CradlewiseClient(auth)

    cradles = await client.discover_cradles()
    if not cradles:
        raise RuntimeError("No cradles found on this Cradlewise account")

    # videoRoom 501s for an unregistered device_id; provision one stable device and
    # reuse it for every crib (idempotent per name → same id, paired per baby).
    device_id: str | None = None
    for baby_id in dict.fromkeys(c.baby_id for c in cradles.values() if c.baby_id):
        device_id, _cert, _key, _ca = await provision_device(client, baby_id, DEVICE_NAME)
    if device_id is None:
        raise RuntimeError("No baby_id on any discovered cradle; cannot provision device")
    _LOG.info(
        "Provisioned device_id=%s (name=%s) for %d cradle(s)",
        device_id, DEVICE_NAME, len(cradles),
    )
    return client, cradles, device_id


def _pin_encoder_size(recorder: MediaRecorder, width: int, height: int) -> None:
    """Fix the video encoder's output size to the source before recording starts.

    aiortc's MediaRecorder only sets the libx264 stream size from its first frame
    inside the recording loop; for format="rtsp" the muxer can open the encoder at
    libx264's 640x480 default first and scale the real source down (and the
    outcome is racy across restarts). Setting width/height up front — and marking
    the context started so the loop doesn't re-adjust — makes the published RTSP
    carry the true resolution deterministically.
    """
    tracks = getattr(recorder, "_MediaRecorder__tracks", None)
    if not tracks:
        _LOG.warning("Could not pin encoder size (aiortc internal layout changed)")
        return
    for ctx in tracks.values():
        stream = ctx.stream
        if getattr(stream, "type", None) == "video":
            stream.width = width
            stream.height = height
            stream.pix_fmt = "yuv420p"
            ctx.started = True


async def _stream_session(
    client: CradlewiseClient, cradle: object, device_id: str, rtsp_url: str
) -> None:
    """One video session: subscribe to one crib and publish RTSP until it ends.

    Returns when the upstream track ends/stalls (so the supervisor reconnects);
    raises on handshake/publish failure (also handled by the supervisor).
    """
    label = cradle.baby_name or cradle.cradle_id
    video = CradlewiseVideoClient(client, cradle.cradle_id, device_id=device_id)
    recorder: MediaRecorder | None = None
    try:
        track = await video.start(timeout=START_TIMEOUT)
        relay = MediaRelay()

        # Peek the first frame to learn the true source resolution. For
        # format="rtsp", aiortc's MediaRecorder lets libx264 open at its 640x480
        # default before its loop adjusts to the first frame, so the RTSP header
        # locks 640x480 and the real source gets scaled down (racy across
        # restarts). Pin the encoder size up front to avoid that.
        # Stop the probe once we have the first frame. MediaRelay.subscribe()
        # defaults to buffered=True (an unbounded asyncio.Queue), and the relay
        # worker pushes every decoded frame to every subscribed proxy whether or
        # not it's ever read. Leaving the probe open after this one recv() would
        # grow its queue at full frame rate for the whole 24/7 session (a ~GB/min
        # leak). probe.stop() only discards this proxy; it doesn't cancel the
        # source worker, so the recorder/monitor subscriptions keep streaming.
        probe = relay.subscribe(track)
        try:
            first = await asyncio.wait_for(probe.recv(), timeout=START_TIMEOUT)
            src_w, src_h = first.width, first.height
        finally:
            probe.stop()

        recorder = MediaRecorder(rtsp_url, format="rtsp", options={"rtsp_transport": "tcp"})
        recorder.addTrack(relay.subscribe(track))
        if WANT_AUDIO and video.audio_track is not None:
            recorder.addTrack(relay.subscribe(video.audio_track))
            _LOG.info("[%s] Publishing video %dx%d + audio → %s", label, src_w, src_h, rtsp_url)
        else:
            if WANT_AUDIO:
                _LOG.info("[%s] Crib published no audio track; video only", label)
            _LOG.info("[%s] Publishing video %dx%d → %s", label, src_w, src_h, rtsp_url)
        _pin_encoder_size(recorder, src_w, src_h)
        await recorder.start()

        # Block until the upstream feed ends or stalls. We watch our own relay
        # consumer (the relay fans frames out, so this doesn't steal from the
        # recorder) with a no-frame watchdog for hung sessions.
        monitor = relay.subscribe(track)
        while True:
            try:
                await asyncio.wait_for(monitor.recv(), timeout=STALL_TIMEOUT)
            except asyncio.TimeoutError:
                _LOG.warning("[%s] No frame for %ss — assuming stalled; reconnecting", label, STALL_TIMEOUT)
                return
            except Exception as err:  # noqa: BLE001 — track ended / PC failed
                _LOG.info("[%s] Upstream track ended: %s", label, err)
                return
    finally:
        if recorder is not None:
            with contextlib.suppress(Exception):
                await recorder.stop()
        with contextlib.suppress(Exception):
            await video.stop()


async def _supervise_cradle(
    client: CradlewiseClient,
    cradle: object,
    device_id: str,
    rtsp_url: str,
    stop: asyncio.Event,
) -> None:
    """Keep one crib's RTSP publish alive: (re)connect with backoff until stop."""
    label = cradle.baby_name or cradle.cradle_id
    backoff = RETRY_MIN
    while not stop.is_set():
        run_task = asyncio.ensure_future(
            _stream_session(client, cradle, device_id, rtsp_url)
        )
        stop_task = asyncio.ensure_future(stop.wait())
        await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

        if stop.is_set():
            run_task.cancel()
            with contextlib.suppress(Exception):
                await run_task
            break

        stop_task.cancel()
        err = run_task.exception()
        if err is not None:
            _LOG.error("[%s] Session failed: %s", label, err, exc_info=err)
        else:
            backoff = RETRY_MIN  # a clean end (not a failure) → reset backoff

        _LOG.info("[%s] Reconnecting in %.0fs", label, backoff)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=backoff)
        backoff = min(backoff * 2, RETRY_MAX)


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    # Auth / discover / provision once, retrying with backoff so a transient
    # failure (or a not-yet-freed device slot → 422) doesn't hard-exit the process.
    client = cradles = device_id = None
    backoff = RETRY_MIN
    while not stop.is_set():
        try:
            client, cradles, device_id = await _setup()
            break
        except Exception as err:  # noqa: BLE001 — auth/discovery/provision failure
            _LOG.error("Setup failed: %s", err, exc_info=err)
            _LOG.info("Retrying setup in %.0fs", backoff)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, RETRY_MAX)
    if stop.is_set():
        _LOG.info("Bridge stopped")
        return

    # One supervised, independently-reconnecting publish session per crib.
    paths = _assign_paths(cradles)
    for cradle in cradles.values():
        _LOG.info(
            "Bridging cradle %s (baby=%s) → %s/%s",
            cradle.cradle_id, cradle.baby_name, RTSP_BASE, paths[cradle.cradle_id],
        )
    await asyncio.gather(
        *(
            _supervise_cradle(
                client, cradle, device_id, f"{RTSP_BASE}/{paths[cradle.cradle_id]}", stop
            )
            for cradle in cradles.values()
        )
    )

    _LOG.info("Bridge stopped")


if __name__ == "__main__":
    asyncio.run(main())
