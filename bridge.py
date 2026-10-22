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
from datetime import datetime
from pathlib import Path

import numpy as np
from aiortc import MediaStreamTrack
from aiortc.contrib.media import MediaRecorder, MediaRelay
from av import VideoFrame
from PIL import Image, ImageDraw, ImageFont

from cradlewise import (
    CradlewiseAuth,
    CradlewiseClient,
    CradlewiseVideoClient,
    get_app_config,
)
from cradlewise.certs import provision_device
from rtsp_auth import RedactingFormatter, publish_url

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
# Publishing is authenticated: rtsp_auth.py (the container entrypoint) puts the
# localhost-only publisher credentials in RTSP_PUBLISH_USER/_PASSWORD, which this
# process inherits from mediamtx. They're added to the URL only where the
# recorder opens it (rtsp_auth.publish_url); every logged URL stays credential-free.
RTSP_BASE = os.getenv("RTSP_BASE", "rtsp://127.0.0.1:8554").rstrip("/")
CACHE_DIR = Path(os.getenv("CRADLEWISE_CACHE", "/cache"))
WANT_AUDIO = os.getenv("CRADLEWISE_AUDIO", "1").lower() not in ("0", "false", "no")
START_TIMEOUT = float(os.getenv("CRADLEWISE_START_TIMEOUT", "120"))
# No-frame watchdog: if the upstream stalls this long, tear down and reconnect.
STALL_TIMEOUT = float(os.getenv("CRADLEWISE_STALL_TIMEOUT", "30"))
RETRY_MIN = float(os.getenv("CRADLEWISE_RETRY_MIN", "5"))
RETRY_MAX = float(os.getenv("CRADLEWISE_RETRY_MAX", "60"))

# Burn-in timestamp. A single env var both enables the overlay and says where it
# goes: CRADLEWISE_TIMESTAMP=bottom-left (or top-left/top-center/top-right/
# bottom-center/bottom-right, "tl"/"br"/… and "_" separators also accepted).
# Unset — the default — or 0/false/no/off means no overlay at all; a bare
# 1/true/yes/on enables it at TIMESTAMP_DEFAULT.
TIMESTAMP_DEFAULT = "bottom-left"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_TIMESTAMP_POSITIONS = (
    "top-left", "top-center", "top-right",
    "bottom-left", "bottom-center", "bottom-right",
)
_TIMESTAMP_ALIASES = {
    "tl": "top-left", "tc": "top-center", "tr": "top-right",
    "bl": "bottom-left", "bc": "bottom-center", "br": "bottom-right",
}
# fonts-dejavu-core; falls back to Pillow's built-in if the package is missing.
_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)


def _timestamp_position(value: str | None) -> str | None:
    """Parse CRADLEWISE_TIMESTAMP into a corner, or None when it's disabled."""
    raw = (value or "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return None
    if raw in ("1", "true", "yes", "on"):
        return TIMESTAMP_DEFAULT
    name = raw.replace("_", "-").replace(" ", "-")
    name = _TIMESTAMP_ALIASES.get(name, name)
    if name not in _TIMESTAMP_POSITIONS:
        _LOG.warning(
            "Unknown CRADLEWISE_TIMESTAMP=%r (expected one of %s); using %s",
            value, ", ".join(_TIMESTAMP_POSITIONS), TIMESTAMP_DEFAULT,
        )
        return TIMESTAMP_DEFAULT
    return name


TIMESTAMP_POSITION = _timestamp_position(os.getenv("CRADLEWISE_TIMESTAMP"))


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


def _load_font(size_px: int) -> ImageFont.ImageFont:
    """Best available mono font at ``size_px``, degrading to Pillow's built-in."""
    for path in _FONT_PATHS:
        with contextlib.suppress(OSError):
            return ImageFont.truetype(path, size_px)
    with contextlib.suppress(TypeError):  # sized default needs Pillow >= 10.1
        return ImageFont.load_default(size=size_px)
    return ImageFont.load_default()


def _place(position: str, w: int, h: int, mw: int, mh: int, margin: int) -> tuple[int, int]:
    """Top-left pixel for an ``mw``x``mh`` stamp inside a ``w``x``h`` frame.

    Both coordinates are forced even so the 2x2-subsampled chroma rectangle that
    matches the stamp lands on exact U/V pixel boundaries.
    """
    vertical, _, horizontal = position.partition("-")
    if horizontal == "left":
        x = margin
    elif horizontal == "right":
        x = w - mw - margin
    else:
        x = (w - mw) // 2
    y = margin if vertical == "top" else h - mh - margin
    return max(0, min(x, w - mw)) & ~1, max(0, min(y, h - mh)) & ~1


class _TimestampTrack(MediaStreamTrack):
    """Burn the wall clock into each video frame, luma-side only.

    The text is rasterised once per distinct clock string (so once a second, not
    once a frame) into a small greyscale stamp, then alpha-blended into the Y
    plane of a yuv420p copy of the frame; the matching U/V rectangle is pulled
    toward neutral (128) so the glyphs stay grey instead of taking on whatever
    colour is underneath. Staying in YUV avoids the two full-frame RGB
    conversions a ``to_image()``/``from_image()`` round-trip would cost.

    The frame is copied, never mutated in place: MediaRelay hands the *same*
    VideoFrame object to every subscriber (here the recorder and the stall
    monitor), and decoded frames aren't guaranteed to be writable.

    Failures are not fatal — one warning, then frames pass through unstamped, so
    a drawing bug can't take the 24/7 publish down.
    """

    kind = "video"

    def __init__(self, source: MediaStreamTrack, position: str) -> None:
        super().__init__()
        self._source = source
        self._position = position
        self._font: ImageFont.ImageFont | None = None
        self._font_px = 0
        self._key: tuple[str, int] | None = None
        self._lum: np.ndarray | None = None   # glyph luma, 0..255
        self._mask: np.ndarray | None = None  # glyph coverage, 0..255
        self._failed = False

    async def recv(self) -> VideoFrame:
        frame = await self._source.recv()
        if self._failed:
            return frame
        try:
            return self._stamp(frame)
        except Exception as err:  # noqa: BLE001 — never kill the publish over an overlay
            _LOG.warning("Timestamp overlay disabled for this session: %s", err, exc_info=err)
            self._failed = True
            return frame

    def _rasterise(self, text: str, height: int) -> None:
        """(Re)build the stamp bitmaps for ``text`` at a frame height's font size."""
        font_px = max(12, height // 30)
        if font_px != self._font_px or self._font is None:
            self._font = _load_font(font_px)
            self._font_px = font_px
        stroke = max(1, font_px // 12)

        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        left, top, right, bottom = probe.textbbox(
            (0, 0), text, font=self._font, stroke_width=stroke
        )
        mw, mh = max(1, right - left), max(1, bottom - top)
        mw += mw & 1  # even dims keep the half-res chroma rect exact
        mh += mh & 1
        origin = (-left, -top)

        # Two passes over the same glyphs: one for the luma to paint (light text,
        # dark outline, so it reads over both a lit room and an IR night frame),
        # one for how much of each pixel that text actually covers.
        lum = Image.new("L", (mw, mh), 0)
        ImageDraw.Draw(lum).text(
            origin, text, font=self._font, fill=235, stroke_width=stroke, stroke_fill=16
        )
        cover = Image.new("L", (mw, mh), 0)
        ImageDraw.Draw(cover).text(
            origin, text, font=self._font, fill=255, stroke_width=stroke, stroke_fill=255
        )
        self._lum = np.asarray(lum, dtype=np.uint16)
        self._mask = np.asarray(cover, dtype=np.uint16)

    def _stamp(self, frame: VideoFrame) -> VideoFrame:
        w, h = frame.width, frame.height
        text = datetime.now().strftime(TIMESTAMP_FORMAT)
        if self._key != (text, h):
            self._rasterise(text, h)
            self._key = (text, h)

        mask, lum = self._mask, self._lum
        mh, mw = mask.shape
        if mw > w or mh > h:  # absurdly small frame — nothing sensible to draw
            return frame
        x, y = _place(self._position, w, h, mw, mh, max(6, self._font_px // 2))

        # to_ndarray(yuv420p) gives one tightly-packed buffer: Y, then U, then V.
        # Index it flat rather than by row, so odd plane heights can't misalign.
        arr = np.ascontiguousarray(frame.to_ndarray(format="yuv420p"))
        flat = arr.reshape(-1)
        y_size, c_size = w * h, (w // 2) * (h // 2)
        luma = flat[:y_size].reshape(h, w)
        box = luma[y:y + mh, x:x + mw].astype(np.uint16)
        luma[y:y + mh, x:x + mw] = (
            (box * (255 - mask) + lum * mask + 127) // 255
        ).astype(np.uint8)

        cmask = mask[::2, ::2]
        cmh, cmw = cmask.shape
        cy, cx = y // 2, x // 2
        for start in (y_size, y_size + c_size):
            plane = flat[start:start + c_size].reshape(h // 2, w // 2)
            box = plane[cy:cy + cmh, cx:cx + cmw].astype(np.uint16)
            plane[cy:cy + cmh, cx:cx + cmw] = (
                (box * (255 - cmask) + 128 * cmask + 127) // 255
            ).astype(np.uint8)

        stamped = VideoFrame.from_ndarray(arr, format="yuv420p")
        stamped.pts, stamped.time_base = frame.pts, frame.time_base
        return stamped


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

        recorder = MediaRecorder(
            publish_url(rtsp_url), format="rtsp", options={"rtsp_transport": "tcp"}
        )
        video_track = relay.subscribe(track)
        if TIMESTAMP_POSITION is not None:
            # Stamp between the relay and the recorder, so only the published
            # copy is drawn on (the stall monitor below keeps the raw frames).
            video_track = _TimestampTrack(video_track, TIMESTAMP_POSITION)
        recorder.addTrack(video_track)
        stamp = f" (timestamp {TIMESTAMP_POSITION})" if TIMESTAMP_POSITION else ""
        if WANT_AUDIO and video.audio_track is not None:
            recorder.addTrack(relay.subscribe(video.audio_track))
            _LOG.info(
                "[%s] Publishing video %dx%d%s + audio → %s",
                label, src_w, src_h, stamp, rtsp_url,
            )
        else:
            if WANT_AUDIO:
                _LOG.info("[%s] Crib published no audio track; video only", label)
            _LOG.info(
                "[%s] Publishing video %dx%d%s → %s",
                label, src_w, src_h, stamp, rtsp_url,
            )
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
    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format=log_format)
    # Scrub RTSP credentials from every line (incl. tracebacks / libav messages).
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter(log_format))

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
