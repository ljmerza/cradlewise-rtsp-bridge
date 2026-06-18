# cradlewise-rtsp-bridge

Turn every [Cradlewise Smart Crib](https://cradlewise.com) on your account into a
plain **RTSP stream** any NVR (Frigate, Blue Iris, Scrypted, go2rtc, …) can record.

The crib has **no local video stream** — its live feed is a [Janus](https://janus.conf.meetecho.com/)
*videoroom* published to Cradlewise's cloud SFU and consumed over WebRTC. This
service discovers every crib on the account, holds one always-on WebRTC
subscriber session per crib, and re-publishes each received **H.264 video
(+ audio)** as its own local RTSP path via a co-located
[mediamtx](https://github.com/bluenviron/mediamtx):

```
crib(s) → Cradlewise cloud Janus (SFU)
        → cradlewise-rtsp-bridge  (one WebRTC subscriber per crib → mediamtx)
        → rtsp://<host>:8554/cradlewise_<baby_name>
        → your NVR (record / view)
```

Janus is an SFU, so the crib only ever uploads one feed no matter how many
clients watch — this bridge is just one more subscriber alongside the app.

## Quick start

```bash
cp .env.example .env      # fill in CRADLEWISE_EMAIL / CRADLEWISE_PASSWORD
docker compose -f docker-compose.example.yml up -d
docker compose -f docker-compose.example.yml logs -f
# the logs print each crib's path; then point your NVR at e.g.:
ffprobe rtsp://localhost:8554/cradlewise_<baby_name>
```

Or pull the prebuilt image directly:

```bash
docker run -d --name cradlewise-rtsp-bridge -p 8554:8554 \
  -e CRADLEWISE_EMAIL=you@example.com -e CRADLEWISE_PASSWORD=... \
  -v "$PWD/cache:/cache" \
  ghcr.io/ljmerza/cradlewise-rtsp-bridge:latest
```

Images are published to `ghcr.io/ljmerza/cradlewise-rtsp-bridge`
(`linux/amd64` + `linux/arm64`).

## Configuration

| Variable | Default | Description |
|---|---|---|
| `CRADLEWISE_EMAIL` | — | **Required.** Cradlewise account email. |
| `CRADLEWISE_PASSWORD` | — | **Required.** Cradlewise account password. |
| `CRADLEWISE_DEVICE_NAME` | `cradlewise-rtsp-bridge` | Device name used to provision a registered device id (see note). |
| `CRADLEWISE_AUDIO` | `1` | Include each crib's audio track (`1`/`0`). |
| `RTSP_BASE` | `rtsp://127.0.0.1:8554` | RTSP server base; each crib publishes to `<RTSP_BASE>/cradlewise_<baby_name>`. |
| `CRADLEWISE_START_TIMEOUT` | `120` | Seconds to wait for the first video track. |
| `CRADLEWISE_STALL_TIMEOUT` | `30` | Reconnect if no frame arrives for this long. |
| `LOG_LEVEL` | `INFO` | Python log level. |

## How it works

`bridge.py` runs the full Cradlewise control-plane handshake (Cognito auth → REST
→ device provisioning), discovers every crib on the account, and for each holds a
signed Janus WebSocket subscription via [aiortc](https://github.com/aiortc/aiortc),
piping its tracks into mediamtx as a separate RTSP path. mediamtx launches and
supervises the publisher process (`runOnInit`), and each per-crib session
self-reconnects with backoff. The Cradlewise client lives in the vendored
[`cradlewise/`](cradlewise) package.

## Notes & caveats

- **Device-slot limit / provisioning.** The `videoRoom` API only accepts a
  *registered* device id, so the bridge provisions one from `CRADLEWISE_DEVICE_NAME`
  at startup. A Cradlewise account has a device-slot cap; if provisioning a
  brand-new name returns `422 DEVICE_ASSIGNMENT_FAILED`, set
  `CRADLEWISE_DEVICE_NAME` to a name already registered on the account. Don't run
  two clients against the same device name's videoRoom session at once.
- **Continuous cloud bandwidth.** Media flows crib → Cradlewise cloud → bridge
  (there is no LAN path), so a 24/7 recording streams continuously over the
  internet. The vendor's rate limits on a persistent subscriber are unknown.
- **Publishing isn't always 24/7.** If the crib only publishes to the videoroom
  on demand, the handshake raises "No publishers in the video room yet"; the
  bridge retries and recovers when it resumes — expect gaps in that case.
- **Resolution.** The bridge republishes exactly what the crib's videoroom sends.
  If you get a lower resolution than the app, the crib is sending a low simulcast
  layer; selecting a higher layer would need a change in the videoroom subscribe.
- **Re-encode.** aiortc hands decoded frames, so video is re-encoded (CPU
  H.264 + AAC); a 640×480/720p crib feed is cheap.

## CI / releases

GitHub Actions (reusable workflows from
[`ljmerza/misc-actions`](https://github.com/ljmerza/misc-actions)):

- **Docker CI** — builds a `pr-<n>` image on each pull request.
- **Docker Release** — publishes `:main`/`:sha-…` on pushes to `main`, and
  `:vX.Y.Z`/`:latest` (with a provenance attestation) on a published GitHub
  Release.
- **Cleanup PR Image** — removes the `pr-<n>` image when a PR closes.

## Credits & license

MIT. The [`cradlewise/`](cradlewise) package is vendored from
[pycradlewise](https://github.com/jlamendo/pycradlewise) (MIT, Jon Lamendola)
because its live-video support isn't in a published release yet — it will be
replaced with a normal dependency once that ships. See [LICENSE](LICENSE).
