"""AWS IoT device-certificate provisioning for the MQTT (Janus) connection.

Cradlewise's AWS IoT broker authenticates clients with X.509 device certificates
(mutual TLS), not Cognito-signed WebSockets. The app:

1. ``POST /cradles/pairedUsers/v3`` (``fetchDeviceCertsV3``) → ``device_config`` with
   an S3 bucket + object keys for the client cert and private key, plus a root CA.
2. Downloads those objects from the ``cradlewise-device-certs`` S3 bucket (SigV4 with
   the Cognito IAM creds).
3. mTLS-connects to AWS IoT using the cert/key.

This module reproduces steps 1–2 and returns the PEM material.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import boto3

from .client import CradlewiseClient
from .exceptions import CradlewiseApiError

_LOGGER = logging.getLogger(__name__)


def _s3_get(creds, region: str, bucket: str, key: str) -> bytes:
    s3 = boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=creds.access_key,
        aws_secret_access_key=creds.secret_key,
        aws_session_token=creds.session_token,
    )
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


async def provision_device(
    client: CradlewiseClient,
    baby_id: int | str,
    device_name: str = "ha-cradlewise-bridge",
) -> tuple[str, bytes, bytes, bytes | None]:
    """Provision/fetch the IoT device cert and return (device_id, cert, key, ca).

    The server assigns/returns a stable ``device_id`` for ``device_name`` and stores
    the cert/key in S3; we download both. Use the returned ``device_id`` as the MQTT
    client_id when connecting. Registers a device slot on the account (idempotent for
    the same ``device_name``).
    """
    await client.auth.ensure_valid()
    cfg: dict[str, Any] = await client.fetch_device_certs(baby_id, device_name)

    bucket = cfg.get("s3_bucket")
    keys = cfg.get("s3_object_keys") or []
    device_id = cfg.get("device_id")
    if not bucket or len(keys) < 2 or not device_id:
        raise CradlewiseApiError(
            f"device_config missing device_id/s3_bucket/object_keys: {cfg}"
        )

    creds = client.auth.credentials
    region = client.auth.app_config.cognito_region
    _LOGGER.debug("Downloading device certs from s3://%s/%s", bucket, keys[:2])
    cert_pem = await asyncio.to_thread(_s3_get, creds, region, bucket, keys[0])
    key_pem = await asyncio.to_thread(_s3_get, creds, region, bucket, keys[1])

    ca = cfg.get("group_ca_cert")
    ca_pem: bytes | None = None
    if ca:
        # group_ca_cert is either inline PEM or another S3 key. NOTE: this is the
        # *device* CA, not the broker's server CA — do NOT pass it as the TLS
        # ca_bytes when connecting (awscrt's default trust store verifies the
        # Amazon-Root-signed AWS IoT endpoint).
        if "BEGIN CERTIFICATE" in ca:
            ca_pem = ca.encode("utf-8")
        else:
            try:
                ca_pem = await asyncio.to_thread(_s3_get, creds, region, bucket, ca)
            except Exception as err:  # pragma: no cover
                _LOGGER.debug("group_ca_cert download skipped: %s", err)

    return device_id, cert_pem, key_pem, ca_pem
