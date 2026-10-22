"""Unit tests for rtsp_auth (stdlib only; run with `pytest tests`)."""

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rtsp_auth as ra  # noqa: E402

TEMPLATE = (Path(__file__).resolve().parents[1] / "mediamtx.yml").read_text()
OK = {"RTSP_READ_USER": "frigate", "RTSP_READ_PASSWORD": "s3cret-pass!"}


def test_refuses_without_read_credentials():
    with pytest.raises(ra.AuthConfigError, match="RTSP_ALLOW_ANONYMOUS"):
        ra.load_config({})


@pytest.mark.parametrize(
    "env",
    [
        {"RTSP_READ_USER": "frigate"},
        {"RTSP_READ_PASSWORD": "longenough1"},
        {**OK, "RTSP_READ_PASSWORD": "short"},
        {**OK, "RTSP_READ_USER": "bad user"},
        {**OK, "RTSP_READ_PASSWORD": "line\nbreak-pass"},
        {**OK, "RTSP_READ_PASSWORD": "has:colon-1234"},
        {**OK, "RTSP_PUBLISH_USER": "frigate"},
        {**OK, "RTSP_ALLOW_ANONYMOUS": "true"},
    ],
)
def test_rejects_bad_config(env):
    with pytest.raises(ra.AuthConfigError) as exc:
        ra.load_config(env)
    assert "s3cret-pass" not in str(exc.value)


def test_anonymous_opt_out():
    cfg = ra.load_config({"RTSP_ALLOW_ANONYMOUS": "TRUE"})
    assert cfg.anonymous
    users = ra.internal_users(cfg)
    assert {"user": "any", "pass": "", "ips": [], "permissions": [{"action": "read"}]} in users
    pub = [u for u in users if u["user"] != "any"][0]
    assert pub["ips"] == ["127.0.0.1", "::1"]


def test_users_are_separated_and_hashed():
    cfg = ra.load_config({**OK, "RTSP_PUBLISH_PASSWORD": "publish-pass-1"})
    assert not cfg.publish_generated
    pub, read = ra.internal_users(cfg)
    assert pub["permissions"] == [{"action": "publish"}] and pub["ips"] == ["127.0.0.1", "::1"]
    assert read["permissions"] == [{"action": "read"}] and read["ips"] == []
    expect = "sha256:" + base64.b64encode(hashlib.sha256(b"s3cret-pass!").digest()).decode()
    assert read["pass"] == expect
    blob = json.dumps([pub, read])
    assert "s3cret-pass" not in blob and "publish-pass-1" not in blob


def test_generated_publish_password_is_random():
    a, b = ra.load_config(OK), ra.load_config(OK)
    assert a.publish_generated and a.publish[1] != b.publish[1] and len(a.publish[1]) >= 32


def test_render_replaces_exactly_the_users_line():
    cfg = ra.load_config(OK)
    out = ra.render_config(TEMPLATE, cfg)
    assert "authInternalUsers: []" not in out and "s3cret-pass" not in out
    assert out.count("authInternalUsers:") == 1
    assert "frigate" in out
    with pytest.raises(ra.AuthConfigError):
        ra.render_config("logLevel: info\n", cfg)


def test_template_is_fail_closed_and_has_no_open_endpoints():
    assert "authInternalUsers: []" in TEMPLATE
    for key in ("api", "metrics", "pprof", "playback"):
        assert f"\n{key}: no\n" in TEMPLATE


def test_with_credentials_percent_encodes():
    url = ra.with_credentials("rtsp://127.0.0.1:8554/cradlewise_a", "bridge", "p@ss/w:rd#?%")
    assert url == "rtsp://bridge:p%40ss%2Fw%3Ard%23%3F%25@127.0.0.1:8554/cradlewise_a"
    assert ra.with_credentials("rtsp://u:p@h/x", "a", "b") == "rtsp://u:p@h/x"
    assert ra.with_credentials("rtsp://h/x", "a", None) == "rtsp://h/x"


def test_publish_url_without_password_is_unchanged():
    assert ra.publish_url("rtsp://127.0.0.1:8554/x", {}) == "rtsp://127.0.0.1:8554/x"


def test_redact():
    pw = "p@ss/w:rd#?%"
    text = f"fail {pw} and {ra.with_credentials('rtsp://h/x', 'u', pw)} and rtsp://a:b@h/y"
    out = ra.redact(text, [pw])
    assert pw not in out and "p%40ss" not in out and ":b@" not in out
    assert "rtsp://***@h/x" in out
