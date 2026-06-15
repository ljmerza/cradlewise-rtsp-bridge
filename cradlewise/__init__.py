"""Vendored, minimal Cradlewise client for the RTSP bridge.

Extracted from **pycradlewise** (MIT, Jon Lamendola —
https://github.com/jlamendo/pycradlewise) because the live-video
(videoRoom/WSS) support is not yet in a published release. Only the modules the
bridge needs are vendored — auth, REST client, device-cert provisioning,
app-config bootstrap, data models, and the WebRTC video client. The MQTT/shadow
telemetry path (`mqtt.py`) is intentionally omitted.

Re-sync from upstream when the video support lands in a release, or replace this
package with a normal pip dependency on pycradlewise.
"""

from .auth import CradlewiseAuth, CradlewiseCredentials
from .bootstrap import AppConfig, get_app_config, refresh_app_config
from .certs import provision_device
from .client import CradlewiseClient
from .exceptions import CradlewiseApiError, CradlewiseAuthError, CradlewiseError
from .models import CradlewiseCradle, SleepAnalytics
from .video import CradlewiseVideoClient, build_ws_auth_headers

__all__ = [
    "AppConfig",
    "CradlewiseApiError",
    "CradlewiseAuth",
    "CradlewiseAuthError",
    "CradlewiseClient",
    "CradlewiseCradle",
    "CradlewiseCredentials",
    "CradlewiseError",
    "CradlewiseVideoClient",
    "SleepAnalytics",
    "build_ws_auth_headers",
    "get_app_config",
    "provision_device",
    "refresh_app_config",
]
