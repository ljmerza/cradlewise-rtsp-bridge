# Cradlewise → RTSP bridge: mediamtx (RTSP server) + a Python publisher that
# subscribes to the crib's cloud Janus videoroom (vendored cradlewise client)
# and re-publishes it as local RTSP for any NVR.
FROM python:3.13-slim

# ffmpeg: PyAV/aiortc media stack + the MediaRecorder RTSP muxer.
# curl/ca-certificates: fetch the mediamtx release.
# fonts-dejavu-core: glyphs for the optional CRADLEWISE_TIMESTAMP overlay.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# mediamtx static binary, arch-matched to the build platform (buildx sets
# TARGETARCH). Supports the multi-arch (amd64 + arm64) image the CI publishes.
ARG MEDIAMTX_VERSION=1.9.3
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) MTX_ARCH=amd64 ;; \
      arm64) MTX_ARCH=arm64v8 ;; \
      arm)   MTX_ARCH=armv7 ;; \
      *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_${MTX_ARCH}.tar.gz" \
      | tar -xz -C /usr/local/bin mediamtx; \
    chmod +x /usr/local/bin/mediamtx

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY cradlewise/ /app/cradlewise/
COPY bridge.py mediamtx.yml rtsp_auth.py /app/

# rtsp_auth.py validates the RTSP_* auth env vars (refusing to start without read
# credentials unless RTSP_ALLOW_ANONYMOUS=true), renders them into mediamtx's
# internal-auth config and exec()s mediamtx on it.
CMD ["python3", "-u", "/app/rtsp_auth.py"]
