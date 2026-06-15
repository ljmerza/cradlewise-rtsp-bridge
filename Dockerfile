# Cradlewise → RTSP bridge: mediamtx (RTSP server) + a Python publisher that
# subscribes to the crib's cloud Janus videoroom (vendored cradlewise client)
# and re-publishes it as local RTSP for any NVR.
FROM python:3.13-slim

# ffmpeg: PyAV/aiortc media stack + the MediaRecorder RTSP muxer.
# curl/ca-certificates: fetch the mediamtx release.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates \
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
COPY bridge.py mediamtx.yml /app/

CMD ["mediamtx", "/app/mediamtx.yml"]
