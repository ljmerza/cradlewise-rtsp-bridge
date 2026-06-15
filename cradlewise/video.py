"""Live-video WebRTC client for the Cradlewise Smart Crib.

Reverse-engineered from the Cradlewise Android app (``com.cradlewise.nini.app``):
the live feed is a **Janus videoroom** stream. The app:

1. ``GET /cradles/{cradleId}/videoRoom?deviceId=...`` → ``VideoRoomResponse``
   (``lb_endpoint`` WebSocket URL, ``room_id``, ``pin``, ``opaque_id``,
   ``video_room_auth_secret``).
2. Opens a WebSocket to ``lb_endpoint`` (subprotocol ``janus-protocol``) with signed
   ``X-*`` headers (see :func:`build_ws_auth_headers`).
3. Janus flow: ``create`` session → ``attach`` ``janus.plugin.videoroom`` → ``join`` as
   *publisher* to enumerate feeds → pick the cradle's publisher → attach a second
   (subscriber) handle → ``join`` as *subscriber* to that feed → Janus sends an SDP
   **offer** → we reply with an **answer** → ICE (Google STUN; the app's Amazon TURN
   relay is dead, so it is unused) → ``start``.

This module reproduces that flow with :mod:`aiortc` as the answering peer and exposes
the received :class:`aiortc.mediastreams.MediaStreamTrack` so callers (e.g. a Home
Assistant camera) can consume or re-publish the H.264 video.

Requires the ``video`` extra: ``pip install 'pycradlewise[video]'``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .client import CradlewiseClient
from .exceptions import CradlewiseApiError, CradlewiseError

try:  # the heavy media stack is optional
    from aiortc import (
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.sdp import candidate_from_sdp

    AIORTC_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the extra
    AIORTC_AVAILABLE = False

_LOGGER = logging.getLogger(__name__)

# STUN server, hardcoded in the app (WebRtcConstants.IceServer). The app also defines
# an Amazon TURN relay, but it is unreachable (dead) and unnecessary from an ordinary
# (cone) NAT, so the videoroom client uses STUN only.
GOOGLE_STUN = "stun:stun.l.google.com:19302"

# WebSocket auth (WebSocketController.createWebSocket / AwsSignature).
_X_ORIGIN_VALUE = "20000"
_SIGNED_KEYS = "X-Origin,X-CId,X-DId,X-Timestamp,X-SId"
_JANUS_SUBPROTOCOL = "janus-protocol"
_VIDEOROOM_PLUGIN = "janus.plugin.videoroom"

_KEEPALIVE_SECS = 25
_STEP_TIMEOUT = 20.0


def _now_utc_z() -> str:
    """UTC timestamp matching the app's getLocalTimeInUTCZ.

    The app uses ``SimpleDateFormat("yyyyMMddHHmmssSSSSSS")`` + ``"Z"`` (compact, e.g.
    ``20260615034851000525Z``) — NOT ISO-8601. The videoRoom server independently
    parses ``X-Timestamp`` for freshness in exactly this format, so an ISO-8601 value
    makes the WebSocket upgrade fail with 403 even though the HMAC signature is valid.
    """
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S") + f"{dt.microsecond // 1000:06d}" + "Z"


def build_ws_auth_headers(
    cradle_id: str, device_id: str, auth_secret: str
) -> dict[str, str]:
    """Build the signed ``X-*`` + ``Authorization`` headers for the Janus WebSocket.

    Reproduces ``AwsSignature.generateHmacSignature``:
    canonical = "\\n".join(f"{key.lower()}:{value}" for key in X-Signed-Keys);
    sha = sha256_hex(canonical); Authorization = "HMAC " + hmac_sha256_hex(secret, sha).
    """
    headers = {
        "X-Origin": _X_ORIGIN_VALUE,
        "X-CId": cradle_id,
        "X-DId": device_id,
        "X-Timestamp": _now_utc_z(),
        "X-SId": str(uuid.uuid4()),
        "X-Signed-Keys": _SIGNED_KEYS,
    }
    canonical = "\n".join(
        f"{key.lower()}:{headers[key]}" for key in _SIGNED_KEYS.split(",")
    )
    sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    sig = hmac.new(auth_secret.encode("utf-8"), sha.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = f"HMAC {sig}"
    return headers


def _txn() -> str:
    return uuid.uuid4().hex[:12]


class CradlewiseVideoClient:
    """Subscribe to a cradle's Janus videoroom feed and expose the video track.

    Usage::

        client = CradlewiseClient(auth)
        video = CradlewiseVideoClient(client, cradle_id)
        track = await video.start()          # waits until the video track arrives
        frame = await track.recv()           # aiortc VideoFrame (decoded)
        if video.audio_track:                # audio is published in the same stream
            ...                              # re-publish video.audio_track too
        await video.stop()
    """

    def __init__(
        self,
        client: CradlewiseClient,
        cradle_id: str,
        *,
        device_id: str | None = None,
        on_track: Callable[[Any], None] | None = None,
    ) -> None:
        if not AIORTC_AVAILABLE:
            raise CradlewiseError(
                "aiortc is not installed; install with: pip install 'pycradlewise[video]'"
            )
        self._client = client
        self._cradle_id = cradle_id
        # The app sends a per-install device id; any stable UUID works for our client.
        self._device_id = device_id or f"ha-{uuid.uuid4().hex[:12]}"
        self._on_track = on_track

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pc: RTCPeerConnection | None = None
        self._recv_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None

        self._session_id: int | None = None
        self._pub_handle: int | None = None
        self._sub_handle: int | None = None
        self._private_id: int | None = None
        self._feed_id: int | None = None
        self._room_id: int | str | None = None
        self._pin: str = ""

        # transaction -> queue of matching messages
        self._pending: dict[str, asyncio.Queue] = {}
        # Janus trickles its ICE candidates immediately after the SDP offer — before
        # _answer creates self._pc. Buffer those early arrivals and flush them once the
        # PC + remoteDescription exist (see _answer / _add_remote_candidate).
        self._pending_cands: list[dict[str, Any]] = []
        self._ice_ready: bool = False
        self._track_ready: asyncio.Future | None = None
        self.track: Any = None
        # The cradle publishes audio in the same Janus stream as video; the Android
        # app simply gates playback with a local mute (AudioTrack.setEnabled). We
        # keep the received audio track here so callers can re-publish it. It stays
        # None when Janus's offer carries no audio m-line.
        self.audio_track: Any = None

    # ── public API ────────────────────────────────────────────────────────

    async def start(self, timeout: float = 45.0) -> Any:
        """Run the full handshake and return the received video MediaStreamTrack."""
        self._track_ready = asyncio.get_running_loop().create_future()
        await self._connect_ws()
        self._recv_task = asyncio.ensure_future(self._receive_loop())
        try:
            await asyncio.wait_for(self._handshake(), timeout=timeout)
            self.track = await asyncio.wait_for(self._track_ready, timeout=timeout)
        except Exception:
            await self.stop()
            raise
        self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())
        return self.track

    async def stop(self) -> None:
        for task in (self._keepalive_task, self._recv_task):
            if task:
                task.cancel()
        if self._pc:
            try:
                await self._pc.close()
            except Exception:
                pass
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    # ── connection / signaling ──────────────────────────────────────────────

    async def _connect_ws(self) -> None:
        await self._client.auth.ensure_valid()
        details = await self._client.get_video_room_details(
            self._cradle_id, self._device_id
        )
        lb_endpoint = details.get("lb_endpoint")
        secret = details.get("video_room_auth_secret")
        self._room_id = details.get("room_id")
        self._pin = details.get("pin") or ""
        self._opaque_id = details.get("opaque_id") or f"ha-{uuid.uuid4().hex[:8]}"
        if not lb_endpoint or not secret:
            raise CradlewiseApiError(
                f"videoRoom response missing lb_endpoint/secret: {details}"
            )
        headers = build_ws_auth_headers(self._cradle_id, self._device_id, secret)
        _LOGGER.debug("Connecting Janus WS %s (room=%s)", lb_endpoint, self._room_id)
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(
            lb_endpoint, protocols=(_JANUS_SUBPROTOCOL,), headers=headers, heartbeat=None
        )

    async def _send(self, msg: dict[str, Any]) -> None:
        assert self._ws is not None
        _LOGGER.debug("→ janus %s", msg.get("janus"))
        await self._ws.send_str(json.dumps(msg))

    async def _request(
        self, msg: dict[str, Any], accept: Callable[[dict], bool]
    ) -> dict[str, Any]:
        """Send a message with a transaction id and await the matching reply.

        Skips intermediate ``ack`` frames; raises on ``error``.
        """
        transaction = msg.setdefault("transaction", _txn())
        queue: asyncio.Queue = asyncio.Queue()
        self._pending[transaction] = queue
        try:
            await self._send(msg)
            while True:
                reply = await asyncio.wait_for(queue.get(), timeout=_STEP_TIMEOUT)
                if reply.get("janus") == "ack":
                    continue
                if reply.get("janus") == "error" or reply.get("error"):
                    raise CradlewiseApiError(f"Janus error: {reply}")
                pd_err = (
                    reply.get("plugindata", {}).get("data", {}).get("error")
                )
                if pd_err:
                    raise CradlewiseApiError(f"videoroom error: {pd_err}")
                if accept(reply):
                    return reply
        finally:
            self._pending.pop(transaction, None)

    async def _handshake(self) -> None:
        # 1) create session
        reply = await self._request(
            {"janus": "create"}, lambda m: m.get("janus") == "success"
        )
        self._session_id = reply["data"]["id"]

        # 2) attach videoroom plugin (publisher handle)
        reply = await self._request(
            {
                "janus": "attach",
                "plugin": _VIDEOROOM_PLUGIN,
                "session_id": self._session_id,
                "opaque_id": self._opaque_id,
            },
            lambda m: m.get("janus") == "success",
        )
        self._pub_handle = reply["data"]["id"]

        # 3) join the room as publisher to enumerate the cradle's feed
        joined = await self._request(
            {
                "janus": "message",
                "session_id": self._session_id,
                "handle_id": self._pub_handle,
                "body": {
                    "request": "join",
                    "ptype": "publisher",
                    "room": self._room_id,
                    "pin": self._pin,
                    "display": f"{self._device_id}_{int(datetime.now(timezone.utc).timestamp()*1000)}_ha",
                },
            },
            lambda m: m.get("plugindata", {}).get("data", {}).get("videoroom")
            == "joined",
        )
        data = joined["plugindata"]["data"]
        self._private_id = data.get("private_id")
        publishers = data.get("publishers") or []
        if not publishers:
            raise CradlewiseError(
                "No publishers in the video room yet — is the cradle awake/streaming?"
            )
        # The crib is normally the only publisher; prefer one whose display mentions it.
        feed = next(
            (p for p in publishers if self._cradle_id in str(p.get("display", ""))),
            publishers[0],
        )
        self._feed_id = feed["id"]
        _LOGGER.debug("Cradle feed id=%s (private_id=%s)", self._feed_id, self._private_id)

        # 4) attach a second handle for the subscriber
        reply = await self._request(
            {
                "janus": "attach",
                "plugin": _VIDEOROOM_PLUGIN,
                "session_id": self._session_id,
                "opaque_id": self._opaque_id,
            },
            lambda m: m.get("janus") == "success",
        )
        self._sub_handle = reply["data"]["id"]

        # 5) join as subscriber → Janus answers with an SDP offer (in jsep)
        offer_msg = await self._request(
            {
                "janus": "message",
                "session_id": self._session_id,
                "handle_id": self._sub_handle,
                "body": {
                    "request": "join",
                    "ptype": "subscriber",
                    "room": self._room_id,
                    "pin": self._pin,
                    "private_id": self._private_id,
                    "feed": self._feed_id,
                    "streams": [{"feed": self._feed_id}],
                },
            },
            lambda m: m.get("jsep", {}).get("type") == "offer",
        )
        await self._answer(offer_msg["jsep"]["sdp"])

    async def _answer(self, offer_sdp: str) -> None:
        # The hardcoded Amazon TURN relay (WebRtcConstants) is dead — unreachable on
        # both UDP and TCP — and only adds ALLOCATE-timeout latency to gathering.
        # Google STUN (server-reflexive) is enough to reach the cloud Janus media
        # server from an ordinary (endpoint-independent / cone) NAT.
        config = RTCConfiguration(iceServers=[RTCIceServer(urls=GOOGLE_STUN)])
        pc = RTCPeerConnection(configuration=config)
        self._pc = pc

        @pc.on("track")
        def _on_track(track):  # noqa: ANN001
            _LOGGER.info("Received %s track from cradle", track.kind)
            if track.kind == "video" and not self._track_ready.done():
                self._track_ready.set_result(track)
            elif track.kind == "audio":
                self.audio_track = track
            if self._on_track:
                self._on_track(track)

        @pc.on("connectionstatechange")
        async def _on_state():
            _LOGGER.debug("PC connection state: %s", pc.connectionState)
            if pc.connectionState == "failed" and not self._track_ready.done():
                self._track_ready.set_exception(
                    CradlewiseError("WebRTC connection failed (ICE)")
                )

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer_sdp, type="offer")
        )
        # remoteDescription is set, so addIceCandidate is valid now. Flush any of
        # Janus's trickle candidates that arrived during the handshake (before
        # self._pc existed) — without this its only routable media address is lost
        # and ICE stays in "checking" until Janus hangs up.
        self._ice_ready = True
        pending, self._pending_cands = self._pending_cands, []
        for cand in pending:
            await self._add_remote_candidate(cand)

        answer = await pc.createAnswer()
        # aiortc completes ICE gathering before this returns, so the answer SDP is
        # already non-trickle (candidates embedded).
        await pc.setLocalDescription(answer)

        # 6) start: send the SDP answer back to Janus
        await self._send(
            {
                "janus": "message",
                "session_id": self._session_id,
                "handle_id": self._sub_handle,
                "transaction": _txn(),
                "body": {"request": "start", "room": self._room_id},
                "jsep": {"type": "answer", "sdp": pc.localDescription.sdp},
            }
        )

    async def _add_remote_candidate(self, cand: dict[str, Any]) -> None:
        """Apply a trickled ICE candidate from Janus to the peer connection.

        Janus begins trickling candidates the moment it sends the offer — before
        :meth:`_answer` has created ``self._pc`` / called ``setRemoteDescription``.
        Buffer anything that arrives early and flush it from ``_answer`` once ICE is
        ready; otherwise Janus's candidates (its only routable media address, which
        rotates every session) are dropped and ICE never leaves ``checking``.
        """
        if cand.get("completed"):
            return
        if not self._ice_ready or not self._pc:
            self._pending_cands.append(cand)
            return
        sdp = cand.get("candidate")
        if not sdp:
            return
        try:
            ice = candidate_from_sdp(sdp.split(":", 1)[1] if sdp.startswith("candidate:") else sdp)
            ice.sdpMid = cand.get("sdpMid")
            ice.sdpMLineIndex = cand.get("sdpMLineIndex")
            await self._pc.addIceCandidate(ice)
        except Exception as err:  # pragma: no cover
            _LOGGER.debug("Failed to add remote candidate %s: %s", cand, err)

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if raw.type != aiohttp.WSMsgType.TEXT:
                    continue
                msg = json.loads(raw.data)
                transaction = msg.get("transaction")
                janus = msg.get("janus")

                # route trickled candidates from Janus into aiortc
                if janus == "trickle":
                    await self._add_remote_candidate(msg.get("candidate", {}))
                    continue

                if transaction and transaction in self._pending:
                    await self._pending[transaction].put(msg)
                    continue

                # async, unsolicited events (webrtcup, media, hangup, slowlink…)
                if janus in ("webrtcup", "media", "slowlink"):
                    _LOGGER.debug("janus async: %s", janus)
                elif janus in ("hangup", "detached", "timeout"):
                    _LOGGER.info("janus session ended: %s", janus)
                    if not self._track_ready.done():
                        self._track_ready.set_exception(
                            CradlewiseError(f"Janus ended session: {janus}")
                        )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pragma: no cover
            _LOGGER.debug("receive loop error: %s", err)

    async def _keepalive_loop(self) -> None:
        try:
            while self._ws and not self._ws.closed and self._session_id:
                await asyncio.sleep(_KEEPALIVE_SECS)
                await self._send(
                    {
                        "janus": "keepalive",
                        "session_id": self._session_id,
                        "transaction": _txn(),
                    }
                )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pragma: no cover
            _LOGGER.debug("keepalive stopped: %s", err)
