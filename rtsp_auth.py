#!/usr/bin/env python3
"""RTSP authentication for the bundled mediamtx (stdlib only).

Container entrypoint: reads the ``RTSP_*`` env vars, renders mediamtx's
``authInternalUsers`` into a private copy of ``mediamtx.yml``, then ``exec``s
mediamtx on it. The same module is imported by ``bridge.py`` for the publish URL
and for log redaction, so both ends always agree on the credentials.

Two principals are created (mediamtx internal auth, passwords stored as sha256):

* **read**    -- ``RTSP_READ_USER`` / ``RTSP_READ_PASSWORD``. May ``read`` any
  path from any address. This is what NVRs (Frigate, ...) use.
* **publish** -- ``RTSP_PUBLISH_USER`` / ``RTSP_PUBLISH_PASSWORD``. May ``publish``
  any path, but ONLY from 127.0.0.1 / ::1. This is the bridge itself. If the
  password is unset a random one is generated for each container start and handed
  to the bridge through the environment (mediamtx runs it as a child process), so
  nothing needs configuring.

No other permission is granted to anyone: the API, metrics, pprof and playback
actions are not given to any user (and are disabled in ``mediamtx.yml``).

Without read credentials the server refuses to start, unless
``RTSP_ALLOW_ANONYMOUS=true`` is set (then anyone may read; publishing stays
locked down; a loud warning is logged).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

ENV_READ_USER = "RTSP_READ_USER"
ENV_READ_PASSWORD = "RTSP_READ_PASSWORD"
ENV_PUBLISH_USER = "RTSP_PUBLISH_USER"
ENV_PUBLISH_PASSWORD = "RTSP_PUBLISH_PASSWORD"
ENV_ALLOW_ANONYMOUS = "RTSP_ALLOW_ANONYMOUS"

DEFAULT_PUBLISH_USER = "bridge"
LOCALHOST = ("127.0.0.1", "::1")
MIN_PASSWORD_LEN = 8

_USER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_USERINFO_RE = re.compile(r"(?i)(rtsps?://)[^/@\s]+@")
_AUTH_LINE_RE = re.compile(r"(?m)^authInternalUsers:.*$")

_LOG = logging.getLogger("rtsp-auth")


class AuthConfigError(ValueError):
    """The RTSP_* environment is missing, inconsistent or invalid."""


@dataclass(frozen=True)
class AuthConfig:
    read: tuple[str, str] | None  # (user, password); None => anonymous read
    publish: tuple[str, str]  # (user, password); localhost only
    publish_generated: bool  # True when the password was randomly generated

    @property
    def anonymous(self) -> bool:
        return self.read is None


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _check_user(var: str, user: str) -> str:
    if not _USER_RE.fullmatch(user):
        raise AuthConfigError(
            f"{var} must be 1-64 characters of A-Z a-z 0-9 . _ - (got an invalid value)"
        )
    return user


def _check_password(var: str, password: str) -> str:
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthConfigError(f"{var} must be at least {MIN_PASSWORD_LEN} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise AuthConfigError(f"{var} must not contain control characters or newlines")
    if ":" in password:
        # mediamtx 1.9.3 rejects HTTP-Basic-style RTSP logins whose password
        # contains ':' (verified), so such a password could never log in.
        raise AuthConfigError(f"{var} must not contain ':'")
    return password


def load_config(env: Mapping[str, str]) -> AuthConfig:
    """Validate the RTSP_* env vars into an :class:`AuthConfig`.

    Raises :class:`AuthConfigError` (never includes a password in the message).
    """
    anonymous = _truthy(env.get(ENV_ALLOW_ANONYMOUS))
    r_user = env.get(ENV_READ_USER, "")
    r_pass = env.get(ENV_READ_PASSWORD, "")

    read: tuple[str, str] | None = None
    if r_user or r_pass:
        if not (r_user and r_pass):
            raise AuthConfigError(
                f"{ENV_READ_USER} and {ENV_READ_PASSWORD} must be set together"
            )
        if anonymous:
            raise AuthConfigError(
                f"{ENV_ALLOW_ANONYMOUS}=true conflicts with {ENV_READ_USER}/"
                f"{ENV_READ_PASSWORD}; unset one of them (refusing to guess)"
            )
        read = (
            _check_user(ENV_READ_USER, r_user),
            _check_password(ENV_READ_PASSWORD, r_pass),
        )
    elif not anonymous:
        raise AuthConfigError(
            f"RTSP read credentials are required: set {ENV_READ_USER} and "
            f"{ENV_READ_PASSWORD}. To deliberately serve the streams to anyone "
            f"on the network without a password, set {ENV_ALLOW_ANONYMOUS}=true."
        )

    p_user = _check_user(
        ENV_PUBLISH_USER, env.get(ENV_PUBLISH_USER) or DEFAULT_PUBLISH_USER
    )
    p_pass = env.get(ENV_PUBLISH_PASSWORD, "")
    generated = not p_pass
    p_pass = secrets.token_urlsafe(32) if generated else _check_password(
        ENV_PUBLISH_PASSWORD, p_pass
    )
    if read is not None and read[0] == p_user:
        raise AuthConfigError(
            f"{ENV_READ_USER} and {ENV_PUBLISH_USER} must differ "
            "(a viewer must not be able to publish)"
        )
    return AuthConfig(read=read, publish=(p_user, p_pass), publish_generated=generated)


def hash_password(password: str) -> str:
    """mediamtx's ``sha256:<base64(sha256(password))>`` credential format."""
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return "sha256:" + base64.b64encode(digest).decode("ascii")


def internal_users(cfg: AuthConfig) -> list[dict]:
    """The mediamtx ``authInternalUsers`` list for ``cfg``."""
    users: list[dict] = [
        {
            "user": cfg.publish[0],
            "pass": hash_password(cfg.publish[1]),
            "ips": list(LOCALHOST),
            "permissions": [{"action": "publish"}],
        }
    ]
    if cfg.read is None:
        # RTSP_ALLOW_ANONYMOUS: 'any' = every user incl. anonymous, any address.
        users.append({"user": "any", "pass": "", "ips": [], "permissions": [{"action": "read"}]})
    else:
        users.append(
            {
                "user": cfg.read[0],
                "pass": hash_password(cfg.read[1]),
                "ips": [],
                "permissions": [{"action": "read"}],
            }
        )
    return users


def render_config(template: str, cfg: AuthConfig) -> str:
    """Replace the ``authInternalUsers:`` line of ``template`` with ``cfg``'s users."""
    if len(_AUTH_LINE_RE.findall(template)) != 1:
        raise AuthConfigError("mediamtx.yml must contain exactly one 'authInternalUsers:' line")
    # JSON is valid YAML flow syntax, so json.dumps handles all quoting.
    line = "authInternalUsers: " + json.dumps(internal_users(cfg))
    return _AUTH_LINE_RE.sub(lambda _m: line, template)


# ── publisher side (used by bridge.py) ────────────────────────────────────────
def with_credentials(url: str, user: str | None, password: str | None) -> str:
    """Return ``url`` with percent-encoded ``user:password@`` userinfo.

    A URL that already carries userinfo, or a missing user/password, is returned
    unchanged.
    """
    if not user or not password:
        return url
    parts = urlsplit(url)
    if "@" in parts.netloc:
        return url
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{parts.netloc}"
    return urlunsplit(parts._replace(netloc=netloc))


def publish_url(url: str, env: Mapping[str, str] | None = None) -> str:
    """Add the publisher credentials from the environment (if any) to ``url``."""
    env = os.environ if env is None else env
    return with_credentials(url, env.get(ENV_PUBLISH_USER) or DEFAULT_PUBLISH_USER,
                            env.get(ENV_PUBLISH_PASSWORD))


def secret_values(env: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    return [env[k] for k in (ENV_READ_PASSWORD, ENV_PUBLISH_PASSWORD) if env.get(k)]


def redact(text: str, secrets_: list[str] | None = None) -> str:
    """Mask known passwords (raw and percent-encoded) and any ``rtsp://u:p@``."""
    for secret in sorted(secrets_ if secrets_ is not None else secret_values(), key=len, reverse=True):
        for variant in {secret, quote(secret, safe="")}:
            text = text.replace(variant, "***")
    return _USERINFO_RE.sub(r"\1***@", text)


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs credentials from the final line, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


# ── container entrypoint ──────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--template", default="/app/mediamtx.yml")
    ap.add_argument("--out", default="/tmp/mediamtx.rendered.yml")
    ap.add_argument("--mediamtx", default="mediamtx", help="mediamtx binary")
    ap.add_argument("--no-exec", action="store_true", help="render only; don't start mediamtx")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(os.environ)
        with open(args.template, encoding="utf-8") as fh:
            rendered = render_config(fh.read(), cfg)
    except (AuthConfigError, OSError) as err:
        _LOG.error("Refusing to start: %s", err)
        return 2

    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(rendered)

    # Hand the publisher creds to the bridge (mediamtx runs it as a child).
    os.environ[ENV_PUBLISH_USER], os.environ[ENV_PUBLISH_PASSWORD] = cfg.publish

    _LOG.info(
        "RTSP publish: user %r, accepted only from %s%s",
        cfg.publish[0], ", ".join(LOCALHOST),
        " (random password generated for this start)" if cfg.publish_generated else "",
    )
    if cfg.anonymous:
        _LOG.warning("=" * 72)
        _LOG.warning(
            "%s=true: RTSP READ IS OPEN, ANYONE WHO CAN REACH THIS PORT CAN WATCH "
            "THE CRIB STREAMS WITHOUT A PASSWORD.", ENV_ALLOW_ANONYMOUS,
        )
        _LOG.warning("Set %s and %s instead.", ENV_READ_USER, ENV_READ_PASSWORD)
        _LOG.warning("=" * 72)
    else:
        _LOG.info("RTSP read: password required, user %r", cfg.read[0])

    if args.no_exec:
        return 0
    sys.stdout.flush()
    os.execvp(args.mediamtx, [args.mediamtx, args.out])
    return 1  # pragma: no cover -- execvp only returns on failure (raises)


if __name__ == "__main__":
    sys.exit(main())
