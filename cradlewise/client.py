"""Cradlewise REST API client."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from .auth import CradlewiseAuth
from .const import SLEEP_PHASE_MAP
from .exceptions import CradlewiseApiError
from .models import CradlewiseCradle, SleepAnalytics

_LOGGER = logging.getLogger(__name__)


class CradlewiseClient:
    """Async client for the Cradlewise REST API."""

    def __init__(self, auth: CradlewiseAuth) -> None:
        self._auth = auth
        self._cradles: dict[str, CradlewiseCradle] = {}
        self._analytics: dict[str, SleepAnalytics] = {}

    @property
    def auth(self) -> CradlewiseAuth:
        return self._auth

    def _sign_request(
        self, method: str, url: str, body: str | None = None
    ) -> dict[str, str]:
        """Sign a request with SigV4."""
        creds = self._auth.credentials
        if not creds:
            raise CradlewiseApiError("Not authenticated")
        parsed = urlparse(url)
        headers = {
            "Host": parsed.hostname,
            "Content-Type": "application/json",
        }
        request = AWSRequest(method=method, url=url, headers=headers, data=body or "")
        region = self._auth.app_config.cognito_region
        SigV4Auth(creds.aws, "execute-api", region).add_auth(request)
        return dict(request.headers)

    async def _api_request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        """Make a signed API request."""
        await self._auth.ensure_valid()

        base_url = self._auth.app_config.api_base_url
        url = f"{base_url}{path}"
        if params:
            query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
            url = f"{url}?{query}"

        body_str = json.dumps(body) if body else None
        headers = await asyncio.to_thread(self._sign_request, method, url, body_str)

        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, url, headers=headers, data=body_str
            ) as resp:
                if resp.status in (401, 403):
                    await self._auth.authenticate()
                    headers = await asyncio.to_thread(
                        self._sign_request, method, url, body_str
                    )
                    async with session.request(
                        method, url, headers=headers, data=body_str
                    ) as retry_resp:
                        retry_resp.raise_for_status()
                        return await retry_resp.json()
                resp.raise_for_status()
                return await resp.json()

    # ── Account & discovery ──────────────────────────────────────────────

    async def get_baby_profiles(self) -> list[dict[str, Any]]:
        """Get baby profiles for the authenticated user."""
        data = await self._api_request(
            "GET", "/babyProfiles/forEmail", params={"email_id": self._auth.email}
        )
        # API returns {"user_list": [...]} wrapper
        if isinstance(data, dict) and "user_list" in data:
            return data["user_list"]
        if isinstance(data, list):
            return data
        return []

    async def get_cradles_for_baby(self, baby_id: int | str) -> list[dict[str, Any]]:
        """Get cradles paired with a specific baby."""
        data = await self._api_request(
            "GET", f"/babyProfiles/{baby_id}/cradles"
        )
        if isinstance(data, dict) and "cradle_list" in data:
            return data["cradle_list"]
        if isinstance(data, list):
            return data
        return []

    async def discover_cradles(self) -> dict[str, CradlewiseCradle]:
        """Discover all cradles linked to the account."""
        profiles = await self.get_baby_profiles()
        cradles: dict[str, CradlewiseCradle] = {}

        for profile in profiles:
            baby_id = profile.get("baby_id") or profile.get("id")
            baby_name = profile.get("name", "Baby")

            if not baby_id:
                continue

            # Fetch cradles for this baby
            cradle_list = await self.get_cradles_for_baby(baby_id)
            for cradle_data in cradle_list:
                cradle_id = cradle_data.get("cradle_id")
                if cradle_id and cradle_id not in cradles:
                    cradles[cradle_id] = CradlewiseCradle(
                        cradle_id=cradle_id,
                        baby_id=str(baby_id),
                        baby_name=baby_name,
                        timezone=cradle_data.get("timezone"),
                    )

        self._cradles = cradles
        return cradles

    # ── Cradle state ─────────────────────────────────────────────────────

    async def get_cradle_state(self, cradle_id: str) -> dict[str, Any]:
        return await self._api_request("GET", f"/cradles/{cradle_id}/state")

    async def get_cradle_online_status(self, cradle_id: str) -> dict[str, Any]:
        return await self._api_request("GET", f"/cradles/{cradle_id}/onlineStatus/v2")

    async def get_firmware_data(self, cradle_id: str) -> dict[str, Any]:
        return await self._api_request("GET", f"/cradles/{cradle_id}/firmwareData")

    async def get_video_room_details(
        self, cradle_id: str, device_id: str
    ) -> dict[str, Any]:
        """Get the Janus video-room connection details for a cradle's live stream.

        Mirrors the app's ``GET /cradles/{cradleId}/videoRoom?deviceId=...`` call.
        Returns the raw VideoRoomResponse dict with keys:
        ``lb_endpoint`` (Janus WebSocket URL), ``room_id``, ``pin``, ``opaque_id``,
        ``video_room_auth_secret``, ``wait_for_cradle_secs``, ``wait_for_frames_secs``.
        These feed pycradlewise.video.CradlewiseVideoClient.
        """
        return await self._api_request(
            "GET",
            f"/cradles/{cradle_id}/videoRoom",
            params={"deviceId": device_id},
        )

    async def fetch_device_certs(
        self,
        baby_id: int | str,
        device_id: str,
        *,
        fcm_token: str = "",
        device_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Provision/fetch this account's AWS IoT device certificate config.

        Mirrors the app's ``fetchDeviceCertsV3`` → ``POST /cradles/pairedUsers/v3``.
        Returns the ``device_config`` dict: ``s3_bucket``, ``s3_object_keys``
        (``[0]`` = client cert PEM, ``[1]`` = private key PEM), ``group_ca_cert``,
        ``device_id``, ``cradle_id``, etc. The cert objects live in the account's
        ``cradlewise-device-certs`` S3 bucket and authorize the IoT (Janus) mTLS
        connection. Registers ``device_id`` against the account (use a stable id).
        """
        device = device_info or {
            "registration_date": datetime.now(timezone.utc).isoformat(),
            "app_version": "1.0.0",
            "country": "US",
            "os": "android",
            "device_name": device_id,
            "os_version": "13",
            "timezone": "UTC",
            "type": "phone",  # device_type enum: "phone"/"tablet" (NOT "mobile")
            "resolution": "1080x1920",
        }
        body = {
            "email_id": self._auth.email,
            "baby_id": int(baby_id),
            "fcm_token": fcm_token,
            "device": device,
        }
        data = await self._api_request("POST", "/cradles/pairedUsers/v3", body=body)
        if isinstance(data, dict) and "device_config" in data:
            return data["device_config"]
        return data

    async def update_cradle(self, cradle: CradlewiseCradle) -> None:
        """Fetch and apply the latest state for a cradle."""
        try:
            state = await self.get_cradle_state(cradle.cradle_id)
            if isinstance(state, dict):
                cradle.state = state
            cradle.online = True
        except Exception as err:
            _LOGGER.debug("Failed to get state for %s: %s", cradle.cradle_id, err)
            cradle.online = False

        try:
            online = await self.get_cradle_online_status(cradle.cradle_id)
            if isinstance(online, dict):
                cradle.online = online.get("online", cradle.online)
        except Exception:
            pass

        try:
            fw = await self.get_firmware_data(cradle.cradle_id)
            if isinstance(fw, dict):
                cradle.firmware_version = fw.get("version") or fw.get(
                    "firmware_version"
                )
        except Exception:
            pass

    # ── Sleep analytics ──────────────────────────────────────────────────

    async def get_sleep_events(self, baby_id: str) -> list[dict[str, Any]]:
        return await self._api_request("GET", f"/babyProfiles/{baby_id}/eventsV3")

    async def get_analytics(
        self, baby_id: str, start_hour: int = 0
    ) -> dict[str, Any]:
        return await self._api_request(
            "GET",
            f"/babyProfiles/{baby_id}/analyticsV3",
            params={"start_hour": start_hour},
        )

    async def get_status_timeline(
        self, baby_id: str, cradle_id: str
    ) -> dict[str, Any]:
        return await self._api_request(
            "GET", f"/babyProfiles/{baby_id}/status_timeline_v2/{cradle_id}"
        )

    async def fetch_sleep_analytics(
        self, cradle: CradlewiseCradle
    ) -> SleepAnalytics:
        """Fetch and aggregate sleep analytics for a cradle's baby."""
        analytics = SleepAnalytics()
        baby_id = cradle.baby_id
        if not baby_id:
            return analytics

        try:
            events_data = await self.get_sleep_events(baby_id)
            if isinstance(events_data, list):
                analytics.events = events_data
                _process_events(analytics, events_data)
        except Exception as err:
            _LOGGER.debug("Failed to fetch events for %s: %s", baby_id, err)

        try:
            metrics = await self.get_analytics(baby_id)
            if isinstance(metrics, dict):
                _process_analytics_response(analytics, metrics)
        except Exception as err:
            _LOGGER.debug("Failed to fetch analytics for %s: %s", baby_id, err)

        self._analytics[baby_id] = analytics
        return analytics

    def get_cached_analytics(self, baby_id: str) -> SleepAnalytics | None:
        return self._analytics.get(baby_id)


# ── Helpers (module-level) ───────────────────────────────────────────────


def _process_events(
    analytics: SleepAnalytics, events: list[dict[str, Any]]
) -> None:
    """Process sleep event list into analytics."""
    if not events:
        return

    sleep_minutes = 0
    soothe_count = 0
    naps: list[dict[str, str]] = []
    current_nap_start: str | None = None

    for event in events:
        event_time = event.get("event_time", "")
        event_value = str(event.get("event_value", ""))
        phase = (
            SLEEP_PHASE_MAP.get(int(event_value), "unknown")
            if event_value.isdigit()
            else "unknown"
        )

        if phase == "sleep" and current_nap_start is None:
            current_nap_start = event_time
        elif phase in ("awake", "away") and current_nap_start is not None:
            naps.append({"start": current_nap_start, "end": event_time})
            current_nap_start = None

        if phase == "sleep":
            sc = event.get("soothe_count")
            if sc is not None:
                try:
                    soothe_count += int(sc)
                except (ValueError, TypeError):
                    pass

    if current_nap_start is not None:
        naps.append({"start": current_nap_start, "end": ""})

    analytics.nap_count = len(naps)

    for nap in naps:
        try:
            start = datetime.fromisoformat(nap["start"].replace("Z", "+00:00"))
            end = (
                datetime.fromisoformat(nap["end"].replace("Z", "+00:00"))
                if nap["end"]
                else datetime.now(timezone.utc)
            )
            duration = int((end - start).total_seconds() / 60)
            sleep_minutes += duration
            if duration > analytics.longest_nap_minutes:
                analytics.longest_nap_minutes = duration
        except (ValueError, TypeError):
            continue

    analytics.total_sleep_minutes = sleep_minutes
    analytics.total_soothe_count = soothe_count

    if naps:
        analytics.last_nap_start = naps[-1]["start"]
        analytics.last_nap_end = naps[-1]["end"] or None

    if events:
        last = events[-1]
        analytics.last_event_time = last.get("event_time")
        raw_val = last.get("event_value")
        if raw_val is not None and str(raw_val).isdigit():
            analytics.last_event_value = SLEEP_PHASE_MAP.get(
                int(raw_val), str(raw_val)
            )
        else:
            analytics.last_event_value = str(raw_val) if raw_val else None


def _process_analytics_response(
    analytics: SleepAnalytics, data: dict[str, Any]
) -> None:
    """Merge server-side analytics data."""
    for key, attr in (
        ("total_sleep", "total_sleep_minutes"),
        ("total_awake", "total_awake_minutes"),
        ("soothe_count", "total_soothe_count"),
    ):
        if key in data:
            try:
                setattr(analytics, attr, int(data[key]))
            except (ValueError, TypeError):
                pass
