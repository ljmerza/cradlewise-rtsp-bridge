"""Constants for the Cradlewise API."""

# Sleep phase mapping (from APK Constants.java babySleepPhaseMap)
SLEEP_PHASE_MAP: dict[int, str] = {
    0: "away",
    1: "awake",
    2: "stirring",
    3: "stirring",
    4: "sleep",
    5: "awake",
    6: "stirring",
}
