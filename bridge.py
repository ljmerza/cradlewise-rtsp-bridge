#!/usr/bin/env python3
"""Cradlewise crib → local RTSP bridge (for Frigate / any NVR ingest).

The Cradlewise crib has no local video stream: its live feed is a **Janus
videoroom** published to Cradlewise's cloud SFU (see ``cradlewise/video.py``).
Home Assistant's ``camera.cradlewise_*`` entity subscribes to it *on demand* and
re-broadcasts over WebRTC. An NVR like Frigate, however, needs a continuous
pullable RTSP source.

This bridge holds a **single, always-on** Janus subscriber session (via the
vendored ``cradlewise.CradlewiseVideoClient``) and re-publishes the received
H.264 video (and, optionally, the crib's audio) as a local RTSP stream:

    crib → Cradlewise cloud Janus (SFU) → [this bridge] → mediamtx RTSP
         → your NVR pulls rtsp://<host>:8554/cradlewise

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
# Match a cradle_id or baby_name; empty → first cradle on the account.
CRADLE_SELECTOR = os.getenv("CRADLEWISE_CRADLE", "").strip()
# Device NAME used to provision a stable, registered device_id (the videoRoom
# endpoint 501s for an unregistered id). CRADLEWISE_DEVICE_ID is a legacy alias.
DEVICE_NAME = (
    os.getenv("CRADLEWISE_DEVICE_NAME")
    or os.getenv("CRADLEWISE_DEVICE_ID")
    or "cradlewise-rtsp-bridge"
)
RTSP_URL = os.getenv("RTSP_URL", "rtsp://127.0.0.1:8554/cradlewise")
CACHE_DIR = Path(os.getenv("CRADLEWISE_CACHE", "/cache"))
WANT_AUDIO = os.getenv("CRADLEWISE_AUDIO", "1").lower() not in ("0", "false", "no")
START_TIMEOUT = float(os.getenv("CRADLEWISE_START_TIMEOUT", "120"))
# No-frame watchdog: if the upstream stalls this long, tear down and reconnect.
STALL_TIMEOUT = float(os.getenv("CRADLEWISE_STALL_TIMEOUT", "30"))
RETRY_MIN = float(os.getenv("CRADLEWISE_RETRY_MIN", "5"))
RETRY_MAX = float(os.getenv("CRADLEWISE_RETRY_MAX", "60"))


def _pick_cradle(cradles: dict[str, object]):
    """Choose which crib to bridge from the discovered cradles."""
    if CRADLE_SELECTOR:
        for cradle in cradles.values():
            if CRADLE_SELECTOR in (cradle.cradle_id, cradle.baby_name):
                return cradle
        _LOG.warning(
            "CRADLEWISE_CRADLE=%r matched nothing; falling back to first cradle",
            CRADLE_SELECTOR,
        )
    return next(iter(cradles.values()))


async def _setup() -> tuple[CradlewiseClient, object, str]:
    """One-time: auth, discover the crib, and provision a registered device_id.

    Returns (client, cradle, device_id). Provisioning is idempotent for
    DEVICE_NAME; we cache the result and run it once per process — re-running per
    reconnect would needlessly re-hit the backend / S3 and churn device slots.
    """
    app_config = await get_app_config(cache_dir=CACHE_DIR)
    auth = CradlewiseAuth(EMAIL, PASSWORD, app_config)
    await auth.authenticate()
    client = CradlewiseClient(auth)

    cradles = await client.discover_cradles()
    if not cradles:
        raise RuntimeError("No cradles found on this Cradlewise account")
    cradle = _pick_cradle(cradles)

    # videoRoom 501s for an unregistered device_id; provision a stable one.
    device_id, _cert, _key, _ca = await provision_device(
        client, cradle.baby_id, DEVICE_NAME
    )
    _LOG.info(
        "Provisioned device_id=%s (name=%s) for cradle %s (baby=%s)",
        device_id, DEVICE_NAME, cradle.cradle_id, cradle.baby_name,
    )
    return client, cradle, device_id


async def _stream_session(client: CradlewiseClient, cradle_id: str, device_id: str) -> None:
    """One video session: subscribe to the crib and publish RTSP until it ends.

    Returns when the upstream track ends/stalls (so the supervisor reconnects);
    raises on handshake/publish failure (also handled by the supervisor).
    """
    video = CradlewiseVideoClient(client, cradle_id, device_id=device_id)
    recorder: MediaRecorder | None = None
    try:
        track = await video.start(timeout=START_TIMEOUT)
        relay = MediaRelay()

        recorder = MediaRecorder(RTSP_URL, format="rtsp", options={"rtsp_transport": "tcp"})
        recorder.addTrack(relay.subscribe(track))
        if WANT_AUDIO and video.audio_track is not None:
            recorder.addTrack(relay.subscribe(video.audio_track))
            _LOG.info("Publishing video + audio → %s", RTSP_URL)
        else:
            if WANT_AUDIO:
                _LOG.info("Crib published no audio track; video only")
            _LOG.info("Publishing video → %s", RTSP_URL)
        await recorder.start()

        # Block until the upstream feed ends or stalls. We watch our own relay
        # consumer (the relay fans frames out, so this doesn't steal from the
        # recorder) with a no-frame watchdog for hung sessions.
        monitor = relay.subscribe(track)
        while True:
            try:
                await asyncio.wait_for(monitor.recv(), timeout=STALL_TIMEOUT)
            except asyncio.TimeoutError:
                _LOG.warning("No frame for %ss — assuming stalled; reconnecting", STALL_TIMEOUT)
                return
            except Exception as err:  # noqa: BLE001 — track ended / PC failed
                _LOG.info("Upstream track ended: %s", err)
                return
    finally:
        if recorder is not None:
            with contextlib.suppress(Exception):
                await recorder.stop()
        with contextlib.suppress(Exception):
            await video.stop()


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

    # Persisted across reconnects so we auth/discover/provision only once.
    state: dict[str, object] = {}

    async def _iteration() -> None:
        if "client" not in state:
            client, cradle, device_id = await _setup()
            state.update(client=client, cradle=cradle, device_id=device_id)
            _LOG.info("Bridging cradle %s (baby=%s)", cradle.cradle_id, cradle.baby_name)
        await _stream_session(
            state["client"], state["cradle"].cradle_id, state["device_id"]
        )

    backoff = RETRY_MIN
    while not stop.is_set():
        run_task = asyncio.ensure_future(_iteration())
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
            _LOG.error("Session failed: %s", err, exc_info=err)
        else:
            backoff = RETRY_MIN  # a clean end (not a failure) → reset backoff

        _LOG.info("Reconnecting in %.0fs", backoff)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=backoff)
        backoff = min(backoff * 2, RETRY_MAX)

    _LOG.info("Bridge stopped")


if __name__ == "__main__":
    asyncio.run(main())
