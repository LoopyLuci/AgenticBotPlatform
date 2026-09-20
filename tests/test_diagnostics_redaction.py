"""Crash reports and support bundles are meant to be pasted into public bug
reports, so credentials in tracebacks and log lines must never survive into
them. Every sample below is assembled at runtime from harmless pieces — no
token-shaped literal lives in the source (GitHub push protection flags them).
"""
from __future__ import annotations

import json
import logging
import zipfile

import pytest

from bot import diagnostics
from bot.diagnostics import redact


def _samples() -> dict[str, str]:
    a20 = "A1b2C3d4E5f6G7h8I9j0"
    return {
        "openai-style key": "key " + "sk-" + a20 + a20,
        "telegram token": "url https://api.telegram.org/" + "bot" + "1234567890" + ":" + a20 + "abcde12345",
        "discord token": "t " + a20 + "." + "AbCdEf" + "." + a20 + "xyz",
        "slack token": "x " + "xox" + "b-" + "1234567890-abcdefghij",
        "github token": "g " + "ghp" + "_" + a20 + "abcdef",
        "aws key id": "k " + "AKIA" + "ABCDEFGHIJKLMNOP",
        "bearer header": "Authorization: " + "Bearer " + a20 + "abcdefgh",
    }


@pytest.mark.parametrize("name", list(_samples()))
def test_known_credential_shapes_are_redacted(name):
    line = _samples()[name]
    out = redact(line)
    secret = line.split(" ", 1)[1] if name != "bearer header" else line.split("Bearer ", 1)[1]
    core = secret.split("/")[-1] if name == "telegram token" else secret
    assert core not in out, f"{name} survived: {out!r}"
    assert "[REDACTED]" in out


def test_secret_named_key_value_pairs_are_redacted_but_the_name_stays():
    out = redact('GET /ws?token=abcdef123456&x=1 and {"api_key": "supersecretvalue99"} password=hunter2hunter2')
    assert "abcdef123456" not in out
    assert "supersecretvalue99" not in out
    assert "hunter2hunter2" not in out
    assert "token=[REDACTED]" in out
    assert '"api_key": "[REDACTED]' in out


def test_this_processs_own_secret_env_values_are_redacted_whatever_their_shape(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_API_KEY", "plainlooking-value-123")
    assert "plainlooking-value-123" not in redact("failed calling service with plainlooking-value-123 in header")


def test_short_env_values_are_not_mangled_into_ordinary_text(monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", "abc")  # too short to be worth replacing
    assert redact("the abc of things") == "the abc of things"


def test_ordinary_log_lines_are_left_alone():
    line = "12:00:01 WARNING bot.models: live_custom_models: fetch failed for provider 'my_ollama': All connection attempts failed"
    assert redact(line) == line


def test_a_crash_report_on_disk_holds_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "CRASH_DIR", tmp_path / "crash_reports")
    secret = "sk-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2"
    record = logging.LogRecord(
        name="test", level=logging.CRITICAL, pathname=__file__, lineno=1,
        msg="provider said: bad key " + secret, args=(), exc_info=None,
    )

    report_id = diagnostics.write_crash_report(record)

    raw = (diagnostics.CRASH_DIR / f"{report_id}.json").read_text(encoding="utf-8")
    assert secret not in raw
    assert json.loads(raw)["message"].endswith("[REDACTED]")


def test_the_support_bundle_redacts_logs_and_older_unredacted_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "CRASH_DIR", tmp_path / "crash_reports")
    monkeypatch.setattr(diagnostics, "BUNDLE_DIR", tmp_path / "bundles")
    monkeypatch.setattr(diagnostics, "LOG_DIR", tmp_path)
    secret = "sk-" + "Qq1Ww2Ee3Rr4Tt5Yy6Uu7Ii8Oo9"
    (tmp_path / "bot.log").write_text(f"12:00 ERROR x: request failed api_key={secret}\n", encoding="utf-8")
    diagnostics.CRASH_DIR.mkdir(parents=True)
    (diagnostics.CRASH_DIR / "20200101-000000-000001.json").write_text(
        json.dumps({"id": "20200101-000000-000001", "level": "CRITICAL", "message": "old report " + secret}),
        encoding="utf-8",
    )

    bundle = diagnostics.build_support_bundle()

    with zipfile.ZipFile(bundle) as zf:
        for name in zf.namelist():
            assert secret not in zf.read(name).decode("utf-8", "replace"), f"{name} leaked the key"
