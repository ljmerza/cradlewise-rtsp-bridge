"""Data models for the Cradlewise API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .const import SLEEP_PHASE_MAP


def shadow_get(state: dict[str, Any], path: str) -> Any:
    """Read a dotted-path value out of a shadow ``state`` dict.

    ``shadow_get(state, "actuator.amplitude")`` returns ``state["actuator"]["amplitude"]``
    or ``None`` if any segment is missing or not a dict.
    """
    cur: Any = state
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def shadow_desired(path: str, value: Any) -> dict[str, Any]:
    """Build a nested desired-update dict from a dotted path and value.

    ``shadow_desired("actuator.amplitude", 5)`` → ``{"actuator": {"amplitude": 5}}``.
    Mirrors the per-field payloads the official app publishes (e.g. it wraps an
    ``amplitude`` change as ``{"actuator": {"amplitude": v}}``).
    """
    parts = path.split(".")
    out: dict[str, Any] = {}
    cur = out
    for part in parts[:-1]:
        nxt: dict[str, Any] = {}
        cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value
    return out


@dataclass
class CradlewiseCradle:
    """Represents a Cradlewise crib."""

    cradle_id: str
    baby_id: str | None = None
    baby_name: str | None = None
    firmware_version: str | None = None
    timezone: str | None = None
    serial_number: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    online: bool = False

    @property
    def baby_present(self) -> bool | None:
        return self.state.get("babyPresent")

    @property
    def baby_sleep_state(self) -> str | None:
        return self.state.get("babySleepState")

    @property
    def baby_sleep_phase(self) -> str | None:
        return self.state.get("babySleepPhase")

    @property
    def cradle_mode(self) -> str | None:
        return self.state.get("detectedCradleMode") or self.state.get(
            "userSetCradleMode"
        )

    @property
    def bounce_mode(self) -> str | None:
        return self.state.get("bounceMode")

    @property
    def bounce_setting(self) -> str | None:
        return self.state.get("bounceSetting")

    @property
    def responsivity_setting(self) -> str | None:
        return self.state.get("responsivitySetting")

    @property
    def music_mode(self) -> str | None:
        return self.state.get("musicMode")

    @property
    def actuator(self) -> dict[str, Any]:
        return self.state.get("actuator") or {}

    @property
    def bouncing(self) -> bool | None:
        return self.actuator.get("on")

    @property
    def bounce_amplitude(self) -> int | None:
        val = self.actuator.get("amplitude")
        return int(val) if val is not None else None

    @property
    def music(self) -> dict[str, Any]:
        # The crib's shadow exposes audio under "soundSynth" (play/volume/trackName/…),
        # not a "music" object.
        return self.state.get("soundSynth") or {}

    @property
    def music_playing(self) -> bool | None:
        return self.music.get("play")

    @property
    def music_volume(self) -> int | None:
        val = self.music.get("volume")
        return int(val) if val is not None else None

    @property
    def light(self) -> dict[str, Any]:
        return self.state.get("light") or {}

    @property
    def light_intensity(self) -> int | None:
        # The crib's shadow exposes the indicator-LED brightness, not a nightlight level.
        val = self.light.get("indicatorBrightness")
        return int(val) if val is not None else None

    @property
    def sleep_phase_raw(self) -> int | None:
        """Raw sleep phase integer from shadow."""
        phase_v2 = self.state.get("babySleepPhaseV2")
        if isinstance(phase_v2, dict):
            return phase_v2.get("eventValue")
        val = self.state.get("babySleepPhase")
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
        return None

    @property
    def sleep_phase_name(self) -> str | None:
        """Human-readable sleep phase from the phase map."""
        raw = self.sleep_phase_raw
        if raw is not None:
            return SLEEP_PHASE_MAP.get(raw, f"unknown ({raw})")
        return self.state.get("babySleepPhase")

    def get_value(self, path: str) -> Any:
        """Read a dotted-path value from this cradle's shadow state."""
        return shadow_get(self.state, path)

    def update_state(self, new_state: dict[str, Any]) -> None:
        """Merge partial state update (from MQTT delta)."""
        for key, value in new_state.items():
            if isinstance(value, dict) and isinstance(self.state.get(key), dict):
                self.state[key].update(value)
            else:
                self.state[key] = value


@dataclass
class SleepAnalytics:
    """Aggregated sleep analytics for a baby."""

    total_sleep_minutes: int = 0
    total_awake_minutes: int = 0
    total_soothe_count: int = 0
    nap_count: int = 0
    longest_nap_minutes: int = 0
    last_nap_start: str | None = None
    last_nap_end: str | None = None
    last_event_time: str | None = None
    last_event_value: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
