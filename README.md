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
        → rtsp://<user>:<password>@<host>:8554/cradlewise_<baby_name>
        → your NVR (record / view)
```

Janus is an SFU, so the crib only ever uploads one feed no matter how many
clients watch — this bridge is just one more subscriber alongside the app.

## Quick start

```bash
cp .env.example .env      # fill in CRADLEWISE_EMAIL / CRADLEWISE_PASSWORD
                          # and RTSP_READ_USER / RTSP_READ_PASSWORD (required, see below)
docker compose -f docker-compose.example.yml up -d
docker compose -f docker-compose.example.yml logs -f
# the logs print each crib's path; then point your NVR at e.g.:
ffprobe rtsp://<RTSP_READ_USER>:<RTSP_READ_PASSWORD>@localhost:8554/cradlewise_<baby_name>
```

Or pull the prebuilt image directly:

```bash
docker run -d --name cradlewise-rtsp-bridge -p 8554:8554 \
  -e CRADLEWISE_EMAIL=you@example.com -e CRADLEWISE_PASSWORD=... \
  -e RTSP_READ_USER=viewer -e RTSP_READ_PASSWORD=choose-a-long-password \
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
| `CRADLEWISE_TIMESTAMP` | *(off)* | Burn a wall-clock timestamp into the video. The value *is* the position — see below. |
| `TZ` | `UTC` | Timezone for the burnt-in timestamp. |
| `RTSP_READ_USER` | — | **Required** (unless `RTSP_ALLOW_ANONYMOUS=true`). Login that viewers/NVRs use to read the streams. |
| `RTSP_READ_PASSWORD` | — | **Required** (same rule). 8+ chars, no `:` or control characters. |
| `RTSP_PUBLISH_USER` | `bridge` | Login the bridge itself uses to publish (localhost only). |
| `RTSP_PUBLISH_PASSWORD` | *(random per start)* | Publish password. Leave unset: a random one is generated at each start and handed to the bridge. |
| `RTSP_ALLOW_ANONYMOUS` | *(off)* | `true` serves the streams to anyone, no password. Logs a loud warning on every start. Mutually exclusive with `RTSP_READ_*`. |
| `RTSP_BASE` | `rtsp://127.0.0.1:8554` | RTSP server base; each crib publishes to `<RTSP_BASE>/cradlewise_<baby_name>`. Don't put credentials in it. |
| `CRADLEWISE_START_TIMEOUT` | `120` | Seconds to wait for the first video track. |
| `CRADLEWISE_STALL_TIMEOUT` | `30` | Reconnect if no frame arrives for this long. |
| `LOG_LEVEL` | `INFO` | Python log level. |

### RTSP authentication

The RTSP server requires a login. Two separate principals are created from the
environment (mediamtx internal auth, passwords stored as SHA-256 hashes in a
private `0600` copy of the config rendered at startup by `rtsp_auth.py`):

| Principal | Env | May | From |
|---|---|---|---|
| **read** | `RTSP_READ_USER` / `RTSP_READ_PASSWORD` | read any path | any address |
| **publish** | `RTSP_PUBLISH_USER` / `RTSP_PUBLISH_PASSWORD` | publish any path | `127.0.0.1` / `::1` only (the bridge, inside the container) |

Nobody is granted the control API, metrics, pprof or playback actions, and those
endpoints are switched off in `mediamtx.yml`. Passwords are never logged (log
lines and tracebacks are scrubbed, and logged RTSP URLs never contain them).

**Consumers must now include the read login in the URL:**

```
rtsp://<RTSP_READ_USER>:<RTSP_READ_PASSWORD>@<host>:8554/cradlewise_<baby_name>
```

Percent-encode special characters in the password (`@` -> `%40`, `/` -> `%2F`,
`#` -> `%23`, `?` -> `%3F`, `%` -> `%25`, space -> `%20`). Easiest is a password with
only letters and digits, e.g. `openssl rand -hex 16`. Also keep `$` and `#` out of
passwords you put in a compose `.env` file, since compose may interpolate them.
`:` is not allowed in passwords (mediamtx rejects such logins).

**Startup rule.** If `RTSP_READ_USER`/`RTSP_READ_PASSWORD` are not set the
container logs an error and exits (code 2) instead of serving an open stream. To
keep serving without a password, say so explicitly with `RTSP_ALLOW_ANONYMOUS=true`;
read is then open to everyone, while publishing stays localhost-only and password
protected. Combining it with `RTSP_READ_*` is rejected as ambiguous.

**Limits.** RTSP here uses HTTP Basic auth (required for hashed passwords) over
plain TCP, so a passive sniffer on the path can read both the password and the
video. Keep port 8554 on a trusted LAN/VPN; this stops casual and accidental
viewing, not an on-path attacker. Overriding the container command so mediamtx
starts directly on `mediamtx.yml` is safe but useless: that file has no users, so
everything (including the bridge's publish) is denied.

### Burnt-in timestamp

`CRADLEWISE_TIMESTAMP` both enables the overlay and places it — one variable, no
separate on/off switch. Leave it unset (or `0`/`false`/`no`/`off`) and no
timestamp is drawn at all:

| Value | Result |
|---|---|
| unset, `0`, `false`, `no`, `off` | No overlay (default). |
| `top-left`, `top-center`, `top-right`, `bottom-left`, `bottom-center`, `bottom-right` | Overlay in that corner. `tl`/`tc`/`tr`/`bl`/`bc`/`br` and `_` separators also work. |
| `1`, `true`, `yes`, `on` | Overlay at `bottom-left`. |

The clock is the container's **local** time, so set `TZ` as well or it stamps
UTC. Format is `YYYY-MM-DD HH:MM:SS`; the font auto-scales to the source height
(~1/30th of it) and is drawn white with a dark outline so it reads on both a lit
room and an IR night frame.

Cost is small: the text is rasterised once per second, then blitted straight into
the frame's luma plane (no RGB round-trip). Measured at **~4 ms/frame at
1080p** on one core — under 6% of a core at 15 fps. If drawing ever fails it logs
once and passes frames through unstamped rather than dropping the stream.

## How it works

`bridge.py` runs the full Cradlewise control-plane handshake (Cognito auth → REST
→ device provisioning), discovers every crib on the account, and for each holds a
signed Janus WebSocket subscription via [aiortc](https://github.com/aiortc/aiortc),
piping its tracks into mediamtx as a separate RTSP path. The container starts
`rtsp_auth.py`, which renders the auth config and `exec`s mediamtx; mediamtx then
launches and supervises the publisher process (`runOnInit`, inheriting the
publish credentials from the environment) which publishes over `127.0.0.1`, and
each per-crib session self-reconnects with backoff. The Cradlewise client lives in the vendored
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
