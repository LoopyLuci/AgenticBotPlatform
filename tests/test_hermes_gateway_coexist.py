"""AgenticBotPlatform and a Hermes Telegram gateway side by side.

The user's own Hermes gateway (`hermes gateway run`) keeps serving their Telegram
bot directly; ABP runs its own channels beside it and must never poll that same
token (a Telegram bot token's `getUpdates` has exactly one long-poller — a second
one gets 409 and the two knock each other off).

Everything here is real: real files under a throwaway HERMES_HOME, a real live
OS process recorded as the gateway (and a real exited one, for the "not
running" half), a real local HTTP server answering the Telegram Bot API
python-telegram-bot actually talks to, and the real dashboard app over an
in-process ASGI transport for the CLI. Only Hermes *itself* is stood in for by
a script on PATH - which is the thing ABP shells out to, not the thing under
test.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from abp_cli.__main__ import _dispatch, _parser
from bot import bot_instances, hermes_gateway, platform_supervisor
from bot.config import config
from bot.dashboard.server import build_app
from bot.dashboard_client import ApiError, DashboardClient

# PTB's own token regex is ^\d+:[A-Za-z0-9_-]{35}$; ABP's validator wants 6+
# digits and 30+ trailing chars. 35 satisfies both.
TOKEN = "1234567890:" + "A" * 35
OTHER_TOKEN = "9876543210:" + "B" * 35
FAKE_PASSWORD = "unused"


# --------------------------------------------------------------- fixtures --

@pytest.fixture
def fake_home(tmp_path, monkeypatch) -> Path:
    """A throwaway Hermes home: an .env with a Telegram token (plus a
    commented-out second one, the way users actually disable a platform) and a
    config.yaml with Telegram enabled. HERMES_HOME points at it, so
    bot/hermes_gateway.default_home() - the whole point of reusing
    bot/hermes_config's resolution - resolves here and the developer's real
    %LOCALAPPDATA%\\hermes is never read or written."""
    home = tmp_path / "hermes"
    (home / "logs").mkdir(parents=True)
    (home / ".env").write_text(
        "# Hermes Agent\n"
        f"TELEGRAM_BOT_TOKEN={TOKEN}\n"
        f"#TELEGRAM_BOT_TOKEN={OTHER_TOKEN}\n"
        "TELEGRAM_ALLOWED_USERS=111\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        "platforms:\n  telegram:\n    enabled: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def live_gateway_process():
    """A real, separate OS process standing in for a running Hermes gateway.

    It is genuine in every way that matters to the code under test: a live pid
    in the process table, started independently of this test process, with a
    real command line that names the Hermes gateway entrypoint - which is
    exactly what bot/hermes_gateway._looks_like_gateway (and Hermes's own
    gateway/status.py matcher) inspect. What is stood in for is Hermes's own
    agent loop, not the detection. The `# ...` comment is what puts those words
    in the process's real argv."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)  # hermes_cli.main gateway run"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    time.sleep(0.4)  # let the interpreter actually be running before it is inspected
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture
def dead_pid() -> int:
    """The pid of a process that has really exited - the stale gateway.pid case."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    proc.wait(timeout=30)
    return proc.pid


def record_gateway(home: Path, pid: int) -> None:
    """Write the pid/state files Hermes's own gateway writes, in its own shape
    (gateway/status.py's _build_pid_record / _build_runtime_status_record)."""
    (home / "gateway.pid").write_text(json.dumps({
        "pid": pid,
        "kind": "hermes-gateway",
        "argv": ["hermes", "gateway", "run"],
        "hermes_home": str(home),
    }), encoding="utf-8")
    (home / "gateway_state.json").write_text(json.dumps({
        "pid": pid,
        "kind": "hermes-gateway",
        "gateway_state": "running",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "active_agents": 0,
        "platforms": {"telegram": {"enabled": True}},
    }), encoding="utf-8")


def _make_row(temp_db, **overrides) -> dict:
    """A real bot_instances row, created through the real create_instance()."""
    kwargs = {
        "name": "telegram bot",
        "platform": "telegram",
        "backend": "native_agent",
        "credentials": {"bot_token": TOKEN},
        "allowed_user_ids": [111],
        "enabled": False,
    }
    kwargs.update(overrides)
    iid = bot_instances.create_instance(**kwargs)
    return bot_instances.get_instance(iid)


class _BotApi:
    """A real local HTTP server answering the Telegram Bot API surface
    python-telegram-bot actually calls, counting what it was asked for.

    `conflict=True` makes getUpdates answer exactly what a real second long-
    poller gets: HTTP 409 with Telegram's own error body.
    """

    def __init__(self, conflict: bool = False) -> None:
        self.conflict = conflict
        self.calls: list[str] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep pytest output clean
                pass

            def _respond(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self) -> None:
                # Drain the request body first: HTTP/1.1 keep-alive means the
                # leftover body bytes would otherwise be parsed as the next
                # request line and the server would answer 501 to garbage.
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                method = self.path.split("?")[0]
                name = method.rsplit("/", 1)[-1]
                with outer._lock:
                    outer.calls.append(name)
                if name == "getUpdates" and outer.conflict:
                    self._respond(409, {
                        "ok": False,
                        "error_code": 409,
                        "description": "Conflict: terminated by other getUpdates request; "
                                       "make sure that only one bot instance is running",
                    })
                elif name == "getMe":
                    self._respond(200, {"ok": True, "result": {
                        "id": 1, "is_bot": True, "first_name": "fake", "username": "fake_bot",
                        "can_join_groups": True, "can_read_all_group_messages": False,
                        "supports_inline_queries": False,
                    }})
                elif name == "getUpdates":
                    time.sleep(0.15)  # stand in for a long poll, so PTB doesn't spin
                    self._respond(200, {"ok": True, "result": []})
                else:
                    self._respond(200, {"ok": True, "result": True})

            do_GET = _handle
            do_POST = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_BotApi":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        # python-telegram-bot appends "<token>/<method>" to base_url verbatim
        # (telegram/_bot.py: self._base_url = base_url + token), so it has to
        # carry the same trailing "/bot" its documented default does.
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/bot"

    def count(self, name: str) -> int:
        with self._lock:
            return sum(1 for c in self.calls if c == name)


@pytest.fixture
def clean_supervisor():
    """Each test starts from an empty supervisor so one test's live tasks can
    never be mistaken for another's."""
    platform_supervisor._handles.clear()
    platform_supervisor._conflict_events.clear()
    platform_supervisor._restart_state.clear()
    yield platform_supervisor
    for instance_id in list(platform_supervisor._handles):
        handle = platform_supervisor._handles[instance_id]
        handle.task.cancel()
    platform_supervisor._handles.clear()
    platform_supervisor._conflict_events.clear()
    platform_supervisor._restart_state.clear()


# --------------------------------------------------------------- ownership --

def test_a_live_gateway_in_a_fake_home_owns_the_token(fake_home, live_gateway_process):
    record_gateway(fake_home, live_gateway_process.pid)

    owner = hermes_gateway.telegram_owner(TOKEN)

    assert owner is not None
    assert Path(owner.home) == fake_home
    assert owner.running is True
    assert owner.status_text == f"served by the Hermes gateway ({fake_home})"
    assert hermes_gateway.gateway_pids(fake_home) == [live_gateway_process.pid]


def test_tokens_are_compared_by_sha256_and_never_leave_the_module_as_text(fake_home, live_gateway_process):
    record_gateway(fake_home, live_gateway_process.pid)

    owner = hermes_gateway.telegram_owner(TOKEN)

    import hashlib

    assert owner.token_sha256 == hashlib.sha256(TOKEN.encode()).hexdigest()
    assert TOKEN not in json.dumps(owner.to_dict())
    assert owner.to_dict()["home"] == str(fake_home)


def test_a_different_token_is_not_claimed(fake_home, live_gateway_process):
    record_gateway(fake_home, live_gateway_process.pid)

    assert hermes_gateway.telegram_owner(OTHER_TOKEN) is None


def test_a_dead_pid_is_not_a_running_gateway(fake_home, dead_pid):
    record_gateway(fake_home, dead_pid)

    assert hermes_gateway.gateway_running(fake_home) is False
    owner = hermes_gateway.telegram_owner(TOKEN)
    assert owner is not None and owner.running is False


def test_a_dead_pid_leaves_the_instance_to_abp(fake_home, dead_pid, temp_db):
    record_gateway(fake_home, dead_pid)
    row = _make_row(temp_db, name="stale-gateway-bot", credentials={"bot_token": TOKEN})

    assert hermes_gateway.instance_owner(row).running is False
    assert hermes_gateway.instance_status(row) == ""


def test_a_commented_out_token_is_not_an_owner(fake_home, live_gateway_process):
    record_gateway(fake_home, live_gateway_process.pid)

    # OTHER_TOKEN is present in the .env only behind a '#'.
    assert hermes_gateway.telegram_owner(OTHER_TOKEN) is None


def test_telegram_explicitly_disabled_in_config_yaml_is_not_an_owner(fake_home, live_gateway_process):
    record_gateway(fake_home, live_gateway_process.pid)
    (fake_home / "config.yaml").write_text(
        "platforms:\n  telegram:\n    enabled: false\n", encoding="utf-8"
    )

    assert hermes_gateway.telegram_enabled(fake_home) is False
    assert hermes_gateway.telegram_owner(TOKEN) is None


def test_an_absent_config_yaml_leaves_hermes_own_creds_enable_the_platform(fake_home, live_gateway_process):
    """Hermes's real rule (gateway/config_env.py's _enable_from_env): credentials
    in .env enable Telegram unless config.yaml explicitly disables it. Treating a
    missing config.yaml as "off" would wrongly hand every token back to ABP."""
    record_gateway(fake_home, live_gateway_process.pid)
    (fake_home / "config.yaml").unlink()

    assert hermes_gateway.telegram_enabled(fake_home) is None
    assert hermes_gateway.telegram_owner(TOKEN) is not None


def test_a_hermes_gateway_backed_instance_is_not_a_conflict_with_its_own_home(fake_home, live_gateway_process, temp_db):
    record_gateway(fake_home, live_gateway_process.pid)
    row = _make_row(temp_db, name="hermes gateway bot", backend="hermes_gateway", hermes_home=str(fake_home))

    assert hermes_gateway.is_self_managed(row) is True
    assert hermes_gateway.instance_owner(row) is None
    assert hermes_gateway.instance_status(row) == ""


def test_every_instance_hermes_home_is_checked_not_just_the_default(fake_home, live_gateway_process, temp_db, tmp_path):
    """A per-instance hermes_home is a second place a token can be owned from."""
    record_gateway(fake_home, live_gateway_process.pid)
    other = tmp_path / "hermes-other"
    other.mkdir()
    (other / ".env").write_text(f"TELEGRAM_BOT_TOKEN={OTHER_TOKEN}\n", encoding="utf-8")
    (other / "config.yaml").write_text("platforms:\n  telegram:\n    enabled: true\n", encoding="utf-8")
    record_gateway(other, live_gateway_process.pid)
    row = _make_row(temp_db, name="per-home bot", credentials={"bot_token": OTHER_TOKEN},
                    hermes_home=str(other))

    owner = hermes_gateway.instance_owner(row)
    assert owner is not None and Path(owner.home) == other
    assert str(other) in hermes_gateway.instance_status(row)


# ------------------------------------------------------- the poller itself --

def test_a_served_instance_is_never_polled(fake_home, live_gateway_process, temp_db, clean_supervisor, monkeypatch):
    """The real proof that ABP does not fight the gateway for its token: point
    every Telegram instance at a real local Bot API server that counts
    getUpdates, and assert it is never asked for one."""
    record_gateway(fake_home, live_gateway_process.pid)
    row = _make_row(temp_db, name="served bot")
    with _BotApi() as api:
        monkeypatch.setenv(platform_supervisor.API_BASE_URL_ENV, api.base_url)

        async def _go():
            await platform_supervisor.start_instance(row)
            for _ in range(60):
                await asyncio.sleep(0.05)
                if platform_supervisor.served_by(row["id"]):
                    break
            # asyncio.run() cancels every pending task on the way out, so the
            # live state has to be read while the loop is still running.
            live = platform_supervisor.status()[row["id"]]
            return live, api.count("getUpdates"), api.count("getMe")

        live, updates, get_me = asyncio.run(_go())

    assert updates == 0
    assert get_me == 0        # the PTB Application is never even built
    assert live["running"] is True
    assert live["served_by"] == f"served by the Hermes gateway ({fake_home})"


def test_a_parked_instance_says_why_once_the_gateway_stops(fake_home, live_gateway_process, temp_db,
                                                           clean_supervisor, monkeypatch):
    """Default behaviour: stopping the gateway is NOT permission to take the
    token over. The instance stays parked, and says so instead of looking like a
    bot that merely needs pressing Start."""
    record_gateway(fake_home, live_gateway_process.pid)
    row = _make_row(temp_db, name="patient bot")
    with _BotApi() as api:
        monkeypatch.setenv(platform_supervisor.API_BASE_URL_ENV, api.base_url)
        monkeypatch.setattr(platform_supervisor, "GATEWAY_RECHECK_S", 0.2)

        async def _go():
            await platform_supervisor.start_instance(row)
            await asyncio.sleep(0.6)
            (fake_home / "gateway.pid").unlink()   # the gateway exits: no pid file
            (fake_home / "gateway_state.json").unlink()
            await asyncio.sleep(1.0)
            return api.count("getUpdates"), platform_supervisor.status()[row["id"]]

        updates, live = asyncio.run(_go())

    assert updates == 0
    assert live["running"] is True                     # still alive, still idle
    assert live["served_by"] == ""                     # nothing is serving it now
    assert "not polling" in live["status"] and str(fake_home) in live["status"]
    assert "takeover_when_gateway_down" in live["status"]
    assert platform_supervisor.served_by(row["id"]) == ""


def test_takeover_when_gateway_down_is_an_explicit_opt_in(fake_home, live_gateway_process, temp_db,
                                                          clean_supervisor, monkeypatch):
    record_gateway(fake_home, live_gateway_process.pid)
    row = _make_row(temp_db, name="eager bot", takeover_when_gateway_down=True)
    assert row["takeover_when_gateway_down"] is True
    with _BotApi() as api:
        monkeypatch.setenv(platform_supervisor.API_BASE_URL_ENV, api.base_url)
        monkeypatch.setattr(platform_supervisor, "GATEWAY_RECHECK_S", 0.2)

        async def _go():
            await platform_supervisor.start_instance(row)
            await asyncio.sleep(0.6)
            assert api.count("getUpdates") == 0
            (fake_home / "gateway.pid").unlink()
            (fake_home / "gateway_state.json").unlink()
            for _ in range(80):
                await asyncio.sleep(0.1)
                if api.count("getUpdates"):
                    break
            return api.count("getUpdates")

        assert asyncio.run(_go()) >= 1


def test_takeover_defaults_to_off_and_round_trips_through_the_real_update(temp_db):
    row = _make_row(temp_db, name="opt-in toggle")

    assert row["takeover_when_gateway_down"] is False          # the default
    bot_instances.update_instance(row["id"], takeover_when_gateway_down=True, actor="test")
    assert bot_instances.get_instance(row["id"])["takeover_when_gateway_down"] is True
    bot_instances.update_instance(row["id"], takeover_when_gateway_down=False, actor="test")
    assert bot_instances.get_instance(row["id"])["takeover_when_gateway_down"] is False


def test_409_conflict_stops_the_instance_and_is_recorded(temp_db, clean_supervisor, monkeypatch):
    """A real 409 off a real local Bot API server, raised by real
    python-telegram-bot, ends the instance instead of retrying forever."""
    row = _make_row(temp_db, name="conflicted bot")
    with _BotApi(conflict=True) as api:
        monkeypatch.setenv(platform_supervisor.API_BASE_URL_ENV, api.base_url)

        async def _go():
            await platform_supervisor.start_instance(row)
            for _ in range(120):
                await asyncio.sleep(0.1)
                if not platform_supervisor.is_running(row["id"]) and row["id"] not in platform_supervisor._handles:
                    break

        asyncio.run(_go())

        assert api.count("getUpdates") >= 1
        stored = bot_instances.get_instance(row["id"])
        assert stored["last_error"] == hermes_gateway.CONFLICT_ERROR
        assert platform_supervisor.is_running(row["id"]) is False
        # No retry storm: the poller is gone, so the request count stops.
        first = api.count("getUpdates")
        time.sleep(1.5)
        assert api.count("getUpdates") == first
        # ...and nothing quietly rescheduled a restart behind our back.
        assert row["id"] not in platform_supervisor._restart_state


def test_a_409_leaves_the_apps_shutdown_prompt(temp_db, clean_supervisor, monkeypatch):
    """The conflicted instance's PTB application is really shut down, and fast:
    waiting out PTB's 30s error backoff would make a restart look hung."""
    row = _make_row(temp_db, name="conflicted fast stop")
    with _BotApi(conflict=True) as api:
        monkeypatch.setenv(platform_supervisor.API_BASE_URL_ENV, api.base_url)

        async def _go():
            t0 = time.monotonic()
            await platform_supervisor.start_instance(row)
            for _ in range(120):
                await asyncio.sleep(0.05)
                if not platform_supervisor.is_running(row["id"]) and row["id"] not in platform_supervisor._handles:
                    return time.monotonic() - t0
            return time.monotonic() - t0

        elapsed = asyncio.run(_go())

    assert elapsed < 20, f"the conflicted instance took {elapsed:.1f}s to unwind"


# ------------------------------------------------------------ the API/CLI --

@pytest.fixture
def fake_hermes_cli(tmp_path, monkeypatch) -> Path:
    """A real executable named `hermes` on PATH that echoes the arguments and
    the HERMES_HOME it was handed, so the windowless spawn itself is provable."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if os.name == "nt":
        script = bindir / "hermes.cmd"
        body = (
            "@echo off\r\n"
            "echo ARGS: %*\r\n"
            "echo HERMES_HOME=%HERMES_HOME%\r\n"
        )
    else:
        script = bindir / "hermes"
        body = (
            "#!/bin/sh\n"
            "echo \"ARGS: $*\"\n"
            "echo \"HERMES_HOME=$HERMES_HOME\"\n"
        )
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD;.EXE;.BAT;.COM;" + os.environ.get("PATHEXT", ""))
    return bindir


def test_every_hermes_cli_call_is_windowless_and_passes_the_home(fake_hermes_cli, fake_home, capsys):
    """No console may ever appear on the user's desktop, and `hermes` must be
    told which home to act on rather than inheriting whatever ABP happens to
    have."""
    assert hermes_gateway._windowless() & getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert not hermes_gateway._windowless() & getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

    out = hermes_gateway.run_cli(["gateway", "status"], home=fake_home)

    assert "gateway status" in out
    assert str(fake_home) in out


@pytest.fixture
def _hermes_scripted_output(tmp_path, monkeypatch) -> Path:
    """A real executable named `hermes` on PATH that prints exactly the text the
    real Hermes CLI prints for `gateway status` / `gateway list` (confirmed by
    running both against a real Hermes install). ABP shells out to this; Hermes's
    own agent is not what is under test - the parsing is."""
    bindir = tmp_path / "hermesbin"
    bindir.mkdir()
    if os.name == "nt":
        script = bindir / "hermes.cmd"
        body = (
            "@echo off\r\n"
            "echo Windows login item installed: C:\\Users\\test\\AppData\\Roaming\\Startup\\Hermes_Gateway.vbs\r\n"
            "echo Gateway process running (PID: 4242)\r\n"
            "echo.\r\n"
            "echo Gateways:\r\n"
            "echo   \u2713 default (current)  \u2014 PID 4242\r\n"
            "echo   \u2717 ops                  \u2014 not running\r\n"
        )
    else:
        script = bindir / "hermes"
        body = (
            "#!/bin/sh\n"
            "echo 'Windows login item installed: /home/test/.config/autostart/Hermes_Gateway.vbs'\n"
            "echo 'Gateway process running (PID: 4242)'\n"
            "echo\n"
            "echo 'Gateways:'\n"
            "echo '  \u2713 default (current)  \u2014 PID 4242'\n"
            "echo '  \u2717 ops                  \u2014 not running'\n"
        )
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD;.EXE;.BAT;.COM;" + os.environ.get("PATHEXT", ""))
    return bindir


@pytest.fixture
def dashboard_client(temp_db, monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    shipped = config.path
    temp_path = tmp_path / "backends.yaml"
    shutil.copy(shipped, temp_path)
    monkeypatch.setattr(config, "path", temp_path)
    monkeypatch.setattr(config, "_data", dict(config._data))
    config.reload(actor="test")
    transport = httpx.ASGITransport(app=build_app())
    c = DashboardClient("http://testserver", "test-token", transport=transport)
    yield c
    asyncio.run(c.aclose())


def run_cli(args_list, client):
    args = _parser().parse_args(args_list)

    async def _wrapped():
        try:
            return await _dispatch(args, client)
        except ApiError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return asyncio.run(_wrapped())


def test_cli_status_json_parses_hermes_own_output(fake_home, _hermes_scripted_output, dashboard_client, capsys):
    code = run_cli(["--json", "hermes", "status", "--home", str(fake_home)], dashboard_client)

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["home"] == str(fake_home)
    # ABP's own `running` comes from this home's own state files (the same
    # answer its ownership check uses); Hermes's own claim is reported
    # separately rather than one silently overriding the other.
    assert out["running"] is False
    assert out["pids"] == []
    assert out["reported_running"] is True
    assert out["reported_pids"] == [4242]
    assert out["service"] == "C:\\Users\\test\\AppData\\Roaming\\Startup\\Hermes_Gateway.vbs"
    assert "Gateway process running" in out["raw"]


def test_cli_status_text_explains_a_disagreement_instead_of_picking_a_side(fake_home, _hermes_scripted_output,
                                                                            dashboard_client, capsys):
    code = run_cli(["hermes", "status", "--home", str(fake_home)], dashboard_client)

    assert code == 0
    out = capsys.readouterr().out
    assert "not running" in out
    assert "could not confirm alive" in out
    assert "| Gateway process running (PID: 4242)" in out


def test_cli_status_json_reports_the_homes_own_live_pid(fake_home, _hermes_scripted_output,
                                                         live_gateway_process, dashboard_client, capsys):
    record_gateway(fake_home, live_gateway_process.pid)

    code = run_cli(["--json", "hermes", "status", "--home", str(fake_home)], dashboard_client)

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["running"] is True
    assert out["pids"] == [live_gateway_process.pid]


def test_cli_list_json_parses_every_profile_row(_hermes_scripted_output, dashboard_client, capsys):
    code = run_cli(["--json", "hermes", "list"], dashboard_client)

    assert code == 0
    rows = json.loads(capsys.readouterr().out)["profiles"]
    assert [r["profile"] for r in rows] == ["default", "ops"]
    assert rows[0] == {**rows[0], "running": True, "current": True, "pid": 4242}
    assert rows[1]["running"] is False and rows[1]["current"] is False


def test_cli_logs_json_tails_the_real_log_file(fake_home, dashboard_client, capsys):
    (fake_home / "logs" / "gateway.log").write_text(
        "\n".join(f"line {i}" for i in range(500)), encoding="utf-8"
    )

    code = run_cli(["--json", "hermes", "logs", "--home", str(fake_home), "--lines", "5"], dashboard_client)

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["files"] == [str(fake_home / "logs" / "gateway.log")]
    assert out["tail"].splitlines()[-1] == "line 499"
    assert "line 495" in out["tail"] and "line 494" not in out["tail"]


def test_cli_instances_json_lists_the_served_bots(fake_home, live_gateway_process, temp_db,
                                                  dashboard_client, capsys):
    record_gateway(fake_home, live_gateway_process.pid)
    row = _make_row(temp_db, name="served by hermes")

    code = run_cli(["--json", "hermes", "instances"], dashboard_client)

    assert code == 0
    out = json.loads(capsys.readouterr().out)["served"]
    assert [s["id"] for s in out] == [row["id"]]
    assert out[0]["served_by"] == f"served by the Hermes gateway ({fake_home})"
    assert TOKEN not in json.dumps(out)


def test_cli_start_runs_hermes_own_login_item_launcher(fake_home, live_gateway_process, tmp_path, monkeypatch):
    """ABP must start the gateway the way Hermes's own Startup-folder login item
    does - hidden, detached, outside ABP's process tree - because a gateway
    spawned as a plain child gets killed by any Job Object around ABP (the very
    thing `hermes gateway status` warns about). Proven by a real cscript on PATH
    that records the arguments it was handed, and stands in for the launcher by
    writing the pid file a real `gateway run` would."""
    launcher = fake_home / "gateway-service"
    launcher.mkdir()
    (launcher / "Hermes_Gateway.vbs").write_text(
        "' Hermes Agent Gateway\r\nsh.Run \"python -m hermes_cli.main gateway run\", 0, False\r\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        pytest.skip("the Windows login-item launcher is Windows-only")
    bindir = tmp_path / "cscriptbin"
    bindir.mkdir()
    args_file = tmp_path / "cscript-args.txt"
    stub = bindir / "cscript.cmd"
    stub.write_text(
        "@echo off\r\n"
        f"echo %* > \"{args_file}\"\r\n"
        f"echo {{\"pid\": {live_gateway_process.pid}, \"kind\": \"hermes-gateway\", "
        f"\"argv\": [\"hermes\", \"gateway\", \"run\"]}} > \"{fake_home / 'gateway.pid'}\"\r\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PATHEXT", ".CMD;.EXE;.BAT;.COM;" + os.environ.get("PATHEXT", ""))

    result = hermes_gateway.start(fake_home)

    recorded = args_file.read_text(encoding="utf-8")
    assert "//nologo" in recorded
    assert str(launcher / "Hermes_Gateway.vbs") in recorded
    assert result["via"] == str(launcher / "Hermes_Gateway.vbs")
    assert result["ok"] is True
    assert result["pids"] == [live_gateway_process.pid]


def test_start_is_a_noop_when_the_gateway_is_already_running(fake_home, live_gateway_process):
    record_gateway(fake_home, live_gateway_process.pid)

    result = hermes_gateway.start(fake_home)

    assert result["already_running"] is True
    assert result["pids"] == [live_gateway_process.pid]


def test_overview_exposes_the_digest_and_never_the_token(fake_home, live_gateway_process):
    record_gateway(fake_home, live_gateway_process.pid)

    overview = hermes_gateway.overview(fake_home)

    assert overview["telegram"]["configured"] is True
    assert overview["telegram"]["enabled"] is True
    assert overview["telegram"]["token_sha256"] == hermes_gateway.token_fingerprint(TOKEN)
    assert TOKEN not in json.dumps(overview)
    assert overview["state"]["pids"] == [live_gateway_process.pid]
    assert str(fake_home) in overview["homes"]


def test_ask_runs_a_one_shot_hermes_dash_z_while_the_gateway_serves_telegram(fake_home, tmp_path, monkeypatch):
    """ABP's own channels reach Hermes with `hermes -z`, a per-call process that
    never touches getUpdates - so there is no token contention with the running
    gateway at all. Proven against a real `hermes` stand-in that records its own
    argv and the environment it was handed."""
    bindir = tmp_path / "askbin"
    bindir.mkdir()
    recorded = tmp_path / "ask-args.txt"
    other_home = tmp_path / "hermes-other"
    other_home.mkdir()
    if os.name == "nt":
        script = bindir / "hermes.cmd"
        script.write_text(
            "@echo off\r\n"
            f'echo %* > "{recorded}"\r\n'
            f'echo %HERMES_HOME% > "{recorded}.home"\r\n'
            "echo the answer\r\n",
            encoding="utf-8",
        )
    else:
        script = bindir / "hermes"
        script.write_text(
            f'#!/bin/sh\necho "$@" > "{recorded}"\necho "$HERMES_HOME" > "{recorded}.home"\necho "the answer"\n',
            encoding="utf-8",
        )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD;.EXE;.BAT;.COM;" + os.environ.get("PATHEXT", ""))

    answer = asyncio.run(hermes_gateway.ask("what is 2+2", home=other_home))

    assert answer["text"] == "the answer"
    argv = recorded.read_text(encoding="utf-8")
    assert "-z" in argv and "what is 2+2" in argv
    # The home override reaches the CHILD, not this process: two `ask` calls
    # scoped to different homes must not swap homes under each other.
    assert Path((tmp_path / "ask-args.txt.home").read_text(encoding="utf-8").strip()) == other_home
    assert os.environ["HERMES_HOME"] == str(fake_home)


def test_a_stale_conflict_event_does_not_spin_a_re_parked_instance(fake_home, live_gateway_process, temp_db,
                                                                   clean_supervisor, monkeypatch):
    """A 409 sets this instance's conflict event, and only stop_instance() pops
    it. So a later run of the same instance can find it already set - and since
    the park loop waits on that same event, it must not treat it as "stop now".
    Otherwise the loop re-checks ownership as fast as the event loop turns.

    Proven behaviourally: with a long re-check interval the status must still be
    the served-by one a second later, i.e. no early re-check happened."""
    record_gateway(fake_home, live_gateway_process.pid)
    row = _make_row(temp_db, name="re-parked bot")
    with _BotApi() as api:
        monkeypatch.setenv(platform_supervisor.API_BASE_URL_ENV, api.base_url)
        monkeypatch.setattr(platform_supervisor, "GATEWAY_RECHECK_S", 30.0)

        async def _go():
            # exactly what a previous 409 leaves behind
            platform_supervisor._conflict_events[row["id"]] = asyncio.Event()
            platform_supervisor._conflict_events[row["id"]].set()
            await platform_supervisor.start_instance(row)
            for _ in range(60):
                await asyncio.sleep(0.05)
                if platform_supervisor.served_by(row["id"]):
                    break
            (fake_home / "gateway.pid").unlink()      # the gateway exits
            (fake_home / "gateway_state.json").unlink()
            await asyncio.sleep(1.0)                  # far less than the 30 s interval
            return platform_supervisor.status()[row["id"]], api.count("getUpdates")

        live, updates = asyncio.run(_go())

    assert updates == 0
    assert live["running"] is True
    assert live["served_by"] == f"served by the Hermes gateway ({fake_home})"
    assert platform_supervisor._conflict_events[row["id"]].is_set() is False


def test_the_dashboard_gateway_overview_route_never_leaks_the_token(fake_home, live_gateway_process,
                                                                    _hermes_scripted_output, dashboard_client):
    record_gateway(fake_home, live_gateway_process.pid)

    payload = asyncio.run(dashboard_client._request("GET", "/api/hermes/gateway", params={"home": str(fake_home)}))

    assert payload["status"]["home"] == str(fake_home)
    assert payload["overview"]["telegram"]["token_sha256"] == hermes_gateway.token_fingerprint(TOKEN)
    assert [p["profile"] for p in payload["profiles"]] == ["default", "ops"]
    assert TOKEN not in json.dumps(payload)


def test_the_served_instance_is_reported_by_the_bots_api(fake_home, live_gateway_process, temp_db,
                                                          dashboard_client):
    record_gateway(fake_home, live_gateway_process.pid)
    row = _make_row(temp_db, name="api served bot")

    listed = asyncio.run(dashboard_client.list_bots())
    shown = asyncio.run(dashboard_client.get_bot(row["id"]))

    assert listed[0]["served_by"] == f"served by the Hermes gateway ({fake_home})"
    assert shown["served_by"] == f"served by the Hermes gateway ({fake_home})"
    # served_by itself is a home path + prose; the token only ever appears as
    # the digest the overview exposes.
    assert TOKEN not in listed[0]["served_by"]


def test_a_409_survives_a_restart_of_the_api_read(fake_home, temp_db, clean_supervisor, monkeypatch, dashboard_client):
    """The 409 reason is on the row itself, so it is still there after an ABP
    restart - not only in the live supervisor's memory. This fake home serves a
    different token and has no gateway, so ABP really does poll this one."""
    row = _make_row(temp_db, name="conflict remembered", credentials={"bot_token": OTHER_TOKEN})
    with _BotApi(conflict=True) as api:
        monkeypatch.setenv(platform_supervisor.API_BASE_URL_ENV, api.base_url)

        async def _go():
            await platform_supervisor.start_instance(row)
            for _ in range(150):
                await asyncio.sleep(0.1)
                if bot_instances.get_instance(row["id"])["last_error"] == hermes_gateway.CONFLICT_ERROR:
                    break

        asyncio.run(_go())

    assert api.count("getUpdates") >= 1
    assert bot_instances.get_instance(row["id"])["last_error"] == hermes_gateway.CONFLICT_ERROR
    shown = asyncio.run(dashboard_client.get_bot(row["id"]))
    assert shown["last_error"] == hermes_gateway.CONFLICT_ERROR
