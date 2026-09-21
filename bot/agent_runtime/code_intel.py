"""Code intelligence after an edit: formatters and language servers (roadmap P5).

Off unless configured - nothing here runs by default, and no language server is bundled:

    native_agent:
      code_intel:
        formatters:                       # run on a file after the agent writes it
          ".py": ["ruff", "format", "{path}"]
          ".rs": ["rustfmt", "{path}"]
        lsp:
          enabled: true
          wait_s: 4                       # how long to wait for a server's verdict after an edit
          max_problems: 10
          servers:
            python:     {command: ["pyright-langserver", "--stdio"], extensions: [".py"]}
            typescript: {command: ["typescript-language-server", "--stdio"], extensions: [".ts", ".tsx", ".js"]}
            rust:       {command: ["rust-analyzer"], extensions: [".rs"]}

**Formatters** run without a shell (`{path}` is replaced by the file), with a scrubbed environment
and a 30 second limit; if the file changes, the agent is told, and its "I read this file" record is
refreshed so the next edit is not refused as stale.

**Language servers** are started on first use, kept running, and talked to over the standard
Language Server Protocol (JSON-RPC with Content-Length framing). After each edit the agent is shown
the errors (and warnings if `include_warnings` is set) the server reports for that file, so it finds
out it broke the build in the same step, not several later. A `lsp` tool answers questions:
diagnostics, symbols, definition, references, hover.

Tested against a small stand-in server, and by hand against **rust-analyzer** (which showed that modern
servers answer diagnostics on request instead of pushing them; both styles are handled). **Not yet run
against pyright or typescript-language-server.** A server still indexing may say nothing at first.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse
from urllib.request import url2pathname

logger = logging.getLogger("bot.code_intel")

FORMAT_TIMEOUT_S = 30.0
SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
_written: contextvars.ContextVar = contextvars.ContextVar("abp_written_files", default=None)


def _cfg() -> dict:
    try:
        from bot.config import config

        return ((config.current.get("native_agent") or {}).get("code_intel")) or {}
    except Exception:  # noqa: BLE001
        return {}


def note_written(path: Path) -> None:
    """Called by the editing tools for every file they write, so the wrapper can follow up."""
    seen = _written.get()
    if seen is not None:
        seen.append(Path(path))


def track() -> contextvars.Token:
    return _written.set([])


def take(token: contextvars.Token) -> list[Path]:
    paths = _written.get() or []
    _written.reset(token)
    seen, out = set(), []
    for p in paths:
        if str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out


def uri_of(path: Path) -> str:
    return "file:///" + quote(str(Path(path).resolve()).replace("\\", "/").lstrip("/"), safe="/:")


def path_of(uri: str) -> Path:
    raw = urlparse(uri).path
    if os.name == "nt" and len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]                                    # /C:/x -> C:/x
    return Path(url2pathname(raw))


def followup(handler):
    """Wrap an editing tool: whatever files it writes are then formatted and checked (see after_writes)."""
    import functools

    @functools.wraps(handler)
    async def wrapped(inp, *, workspace, **kw):
        token = track()
        try:
            out = await handler(inp, workspace=workspace, **kw)
        except BaseException:
            take(token)
            raise
        paths = take(token)
        return out + (await after_writes(paths, workspace) if paths else "")

    return wrapped


# ---- formatters ---------------------------------------------------------------------------
def formatter_for(path: Path) -> Optional[list[str]]:
    table = _cfg().get("formatters") or {}
    command = table.get(path.suffix.lower())
    if isinstance(command, str):
        import shlex

        command = shlex.split(command)
    if not command or not isinstance(command, list):
        return None
    return [str(part).replace("{path}", str(path)) for part in command]


async def format_file(path: Path) -> str:
    """Run the configured formatter on `path`. Returns a note for the agent ('' if nothing happened)."""
    argv = formatter_for(path)
    if not argv:
        return ""
    exe = shutil.which(argv[0]) or (argv[0] if Path(argv[0]).is_file() else None)
    if exe is None:
        return f"(formatter {argv[0]!r} is not installed; {path.name} was not formatted)"
    argv[0] = exe
    from bot.agent_runtime import sandbox

    before = path.read_bytes() if path.exists() else b""
    try:
        proc = await asyncio.create_subprocess_exec(*argv, cwd=str(path.parent), env=sandbox.build_env(),
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=FORMAT_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"(formatter {Path(exe).name} timed out after {int(FORMAT_TIMEOUT_S)}s; {path.name} left as written)"
    except OSError as exc:
        return f"(formatter {Path(exe).name} could not start: {exc})"
    if proc.returncode != 0:
        detail = (err or b"").decode("utf-8", "replace").strip().splitlines()[:3]
        return f"(formatter {Path(exe).name} failed on {path.name}: {' '.join(detail)[:200]}; file left as written)"
    if path.exists() and path.read_bytes() != before:
        try:
            from bot.agent_runtime import coding_tools

            coding_tools.record_read(path)          # the formatter's output is now what the agent knows
        except Exception:  # noqa: BLE001
            pass
        return f"(formatted {path.name} with {Path(exe).name}; re-read it before editing again if you need exact text)"
    return ""


# ---- LSP client -----------------------------------------------------------------------------
class LspError(Exception):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


# -32801 ContentModified and -32802 ServerCancelled: the server is not ready or the text moved on; ask again.
RETRYABLE = (-32801, -32802)


class LspClient:
    """One running language server."""

    def __init__(self, name: str, command: list[str], root: Path, env: Optional[dict] = None):
        self.name, self.command, self.root = name, command, Path(root)
        self.env = env
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._next = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None
        self.diagnostics: dict[str, list[dict]] = {}
        self._published: dict[str, asyncio.Event] = {}
        self._versions: dict[str, int] = {}
        self.capabilities: dict = {}
        self.alive = False
        self.started_at = time.monotonic()
        self.ready = False          # has the server shown it has loaded the project (by reporting something)?

    async def start(self, timeout_s: float = 20.0) -> None:
        exe = shutil.which(self.command[0]) or (self.command[0] if Path(self.command[0]).is_file() else None)
        if exe is None:
            raise LspError(f"{self.command[0]!r} is not installed")
        self.proc = await asyncio.create_subprocess_exec(exe, *self.command[1:], cwd=str(self.root), env=self.env,
                                                         stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.DEVNULL)
        self._reader = asyncio.create_task(self._read_loop())
        root_uri = uri_of(self.root)
        result = await self.request("initialize", {
            "processId": os.getpid(), "rootUri": root_uri, "workspaceFolders": [{"uri": root_uri, "name": self.root.name}],
            "clientInfo": {"name": "abp"},
            "capabilities": {"textDocument": {"synchronization": {"didSave": False}, "publishDiagnostics": {}, "diagnostic": {"dynamicRegistration": False},
                                              "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                                              "definition": {}, "references": {}, "hover": {"contentFormat": ["plaintext", "markdown"]}},
                             "workspace": {"workspaceFolders": True}}}, timeout_s=timeout_s)
        self.capabilities = (result or {}).get("capabilities") or {}
        await self.notify("initialized", {})
        self.alive = True
        self.started_at = time.monotonic()

    async def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if self.alive:
                await asyncio.wait_for(self.request("shutdown", None, timeout_s=3), timeout=3)
                await self.notify("exit", None)
        except Exception:  # noqa: BLE001
            pass
        self.alive = False
        try:
            self.proc.kill()
        except ProcessLookupError:
            pass
        if self._reader:
            self._reader.cancel()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001
            pass

    # ---- wire -------------------------------------------------------------------------
    async def _send(self, message: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise LspError("the server is not running")
        body = json.dumps({"jsonrpc": "2.0", **message}).encode("utf-8")
        self.proc.stdin.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        await self.proc.stdin.drain()

    async def notify(self, method: str, params: Any) -> None:
        await self._send({"method": method, "params": params})

    async def request(self, method: str, params: Any, timeout_s: float = 10.0) -> Any:
        rid = self._next
        self._next += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"id": rid, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            raise LspError(f"{self.name}: {method} timed out after {timeout_s:g}s")
        finally:
            self._pending.pop(rid, None)

    async def _read_message(self) -> Optional[dict]:
        assert self.proc and self.proc.stdout
        length = None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        if length is None:
            return {}
        return json.loads((await self.proc.stdout.readexactly(length)).decode("utf-8"))

    async def _read_loop(self) -> None:
        try:
            while True:
                message = await self._read_message()
                if message is None:
                    break
                if not message:
                    continue
                if "method" in message and "id" in message:               # the server asks us something: say "nothing"
                    await self._send({"id": message["id"], "result": None})
                elif "method" in message:
                    if message["method"] == "textDocument/publishDiagnostics":
                        params = message.get("params") or {}
                        uri = params.get("uri", "")
                        self.diagnostics[uri] = list(params.get("diagnostics") or [])
                        self._published.setdefault(uri, asyncio.Event()).set()
                elif "id" in message:
                    fut = self._pending.get(message["id"])
                    if fut is not None and not fut.done():
                        if "error" in message:
                            fut.set_exception(LspError(str((message["error"] or {}).get("message", "error")), int((message["error"] or {}).get("code", 0) or 0)))
                        else:
                            fut.set_result(message.get("result"))
        except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:  # noqa: BLE001
            logger.debug("lsp reader stopped", exc_info=True)
        finally:
            self.alive = False
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(LspError(f"{self.name} exited"))

    # ---- documents --------------------------------------------------------------------------
    async def sync(self, path: Path, text: str) -> str:
        """Tell the server the file's current text (opening it the first time). Returns its uri."""
        uri = uri_of(path)
        version = self._versions.get(uri, 0) + 1
        self._versions[uri] = version
        self._published.pop(uri, None)
        event = self._published.setdefault(uri, asyncio.Event())
        if version == 1:
            await self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": self.name, "version": 1, "text": text}})
        else:
            await self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]})
        del event
        return uri

    async def wait_for_diagnostics(self, uri: str, wait_s: float, settle_s: float = 0.3) -> Optional[list[dict]]:
        """The diagnostics for `uri` after the last sync, or None if the server said nothing in time.

        Newer servers (rust-analyzer, recent pyright) answer `textDocument/diagnostic` on request and never push,
        so a server that advertises that is asked directly. Others push `publishDiagnostics`, often twice (syntax
        first, then meaning), so after the first message we wait a moment for a second."""
        if self.capabilities.get("diagnosticProvider"):
            deadline = time.monotonic() + wait_s
            while True:
                try:
                    answer = await self.request("textDocument/diagnostic", {"textDocument": {"uri": uri}},
                                                timeout_s=max(0.5, deadline - time.monotonic()))
                except LspError as exc:
                    if exc.code in RETRYABLE and time.monotonic() < deadline:        # still loading the project
                        await asyncio.sleep(0.5)
                        continue
                    answer = None
                if isinstance(answer, dict) and answer.get("kind") == "full":
                    items = list(answer.get("items") or [])
                    # An empty answer just after start may only mean "not indexed yet": ask again for a short while.
                    if not items and not self.ready and time.monotonic() - self.started_at < WARMUP_S and time.monotonic() < deadline:
                        await asyncio.sleep(0.5)
                        continue
                    self.diagnostics[uri] = items
                    self.ready = self.ready or bool(items)
                    return items
                if isinstance(answer, dict) and answer.get("kind") == "unchanged":
                    return self.diagnostics.get(uri, [])
                break
        event = self._published.setdefault(uri, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=wait_s)
        except asyncio.TimeoutError:
            return None
        await asyncio.sleep(settle_s)
        return self.diagnostics.get(uri, [])

    async def call(self, method: str, params: dict, timeout_s: float = 10.0) -> Any:
        return await self.request(method, params, timeout_s=timeout_s)


# ---- managing servers ---------------------------------------------------------------------------
_clients: dict[tuple[str, str], LspClient] = {}
_failed: dict[tuple[str, str], float] = {}
COOL_OFF_S = 300.0
WARMUP_S = 20.0     # a fresh server's first "no problems" is not trusted until it has been up this long (or says something)


def lsp_config() -> dict:
    return _cfg().get("lsp") or {}


def server_for(path: Path) -> Optional[tuple[str, list[str]]]:
    cfg = lsp_config()
    if not cfg.get("enabled"):
        return None
    for name, spec in (cfg.get("servers") or {}).items():
        if isinstance(spec, dict) and path.suffix.lower() in [str(e).lower() for e in (spec.get("extensions") or [])]:
            command = spec.get("command")
            if isinstance(command, list) and command:
                return str(name), [str(c) for c in command]
    return None


async def client_for(path: Path, workspace: Path) -> Optional[LspClient]:
    spec = server_for(path)
    if spec is None:
        return None
    name, command = spec
    key = (str(Path(workspace).resolve()), name)
    client = _clients.get(key)
    if client is not None and client.alive:
        return client
    if time.monotonic() - _failed.get(key, -COOL_OFF_S) < COOL_OFF_S:
        return None
    from bot.agent_runtime import sandbox

    client = LspClient(name, command, Path(workspace).resolve(), env=sandbox.build_env())
    try:
        await client.start(timeout_s=float(lsp_config().get("start_timeout_s", 20)))
    except Exception as exc:  # noqa: BLE001 - a missing or crashing server must never break an edit
        logger.info("language server %s unavailable: %s", name, exc)
        _failed[key] = time.monotonic()
        try:
            await client.stop()
        except Exception:  # noqa: BLE001
            pass
        return None
    _clients[key] = client
    return client


async def shutdown_all() -> None:
    for client in list(_clients.values()):
        await client.stop()
    _clients.clear()


def render_problems(path: Path, workspace: Path, diags: list[dict], *, include_warnings: bool, limit: int) -> str:
    try:
        rel = str(Path(path).resolve().relative_to(Path(workspace).resolve())).replace("\\", "/")
    except ValueError:
        rel = str(path)
    keep = [d for d in diags if int(d.get("severity") or 1) <= (2 if include_warnings else 1)]
    keep.sort(key=lambda d: (int(d.get("severity") or 1), (d.get("range") or {}).get("start", {}).get("line", 0)))
    lines = []
    for d in keep[:limit]:
        start = (d.get("range") or {}).get("start") or {}
        source = f" [{d['source']}]" if d.get("source") else ""
        lines.append(f"  {rel}:{int(start.get('line', 0)) + 1}:{int(start.get('character', 0)) + 1} "
                     f"{SEVERITY.get(int(d.get('severity') or 1), 'error')}: {' '.join(str(d.get('message', '')).split())[:300]}{source}")
    if len(keep) > limit:
        lines.append(f"  ... and {len(keep) - limit} more")
    return "\n".join(lines)


async def after_writes(paths: list[Path], workspace: Path) -> str:
    """Format the files the agent just wrote, then report what the language server thinks of them."""
    notes = []
    for path in paths:
        if not path.is_file():
            continue
        note = await format_file(path)
        if note:
            notes.append(note)
    cfg = lsp_config()
    if cfg.get("enabled"):
        wait_s = float(cfg.get("wait_s", 4))
        limit = int(cfg.get("max_problems", 10))
        for path in paths:
            if not path.is_file():
                continue
            try:
                client = await client_for(path, workspace)
                if client is None:
                    continue
                uri = await client.sync(path, path.read_text(encoding="utf-8", errors="replace"))
                diags = await client.wait_for_diagnostics(uri, wait_s)
            except Exception as exc:  # noqa: BLE001 - never let a broken server fail the edit
                logger.info("diagnostics unavailable for %s: %s", path, exc)
                continue
            if diags is None:
                notes.append(f"(the language server did not report on {path.name} within {wait_s:g}s)")
                continue
            text = render_problems(path, workspace, diags, include_warnings=bool(cfg.get("include_warnings")), limit=limit)
            if text:
                notes.append("Problems the language server found after this edit:\n" + text)
    return ("\n" + "\n".join(notes)) if notes else ""


# ---- the lsp tool ---------------------------------------------------------------------------------
def _flatten_symbols(symbols: list, depth: int = 0) -> list[str]:
    out = []
    for s in symbols or []:
        rng = s.get("range") or (s.get("location") or {}).get("range") or {}
        line = int((rng.get("start") or {}).get("line", 0)) + 1
        out.append(f"{'  ' * depth}{s.get('name')} (kind {s.get('kind')}) line {line}")
        out.extend(_flatten_symbols(s.get("children") or [], depth + 1))
    return out


def _locations(result: Any, workspace: Path) -> str:
    if not result:
        return "(nothing found)"
    items = result if isinstance(result, list) else [result]
    out = []
    for loc in items[:50]:
        uri = loc.get("uri") or loc.get("targetUri") or ""
        rng = loc.get("range") or loc.get("targetSelectionRange") or {}
        start = rng.get("start") or {}
        try:
            shown = str(path_of(uri).resolve().relative_to(Path(workspace).resolve())).replace("\\", "/")
        except (ValueError, OSError):
            shown = uri
        out.append(f"{shown}:{int(start.get('line', 0)) + 1}:{int(start.get('character', 0)) + 1}")
    return "\n".join(out)


async def lsp_tool(inp: dict, *, workspace: Path, instance_id=None, device_tier=None) -> str:
    from bot.agent_runtime.errors import ToolError, safe_path

    action = str(inp.get("action") or "")
    if action not in ("diagnostics", "symbols", "definition", "references", "hover"):
        raise ToolError("action must be one of: diagnostics, symbols, definition, references, hover")
    if not lsp_config().get("enabled"):
        raise ToolError("language servers are not enabled (native_agent.code_intel.lsp.enabled)")
    path = safe_path(workspace, inp.get("path") or "")
    if not path.is_file():
        raise ToolError(f"{inp.get('path')!r} is not a file")
    client = await client_for(path, workspace)
    if client is None:
        raise ToolError("no language server is configured for this file type, or it is not installed or failed to start")
    uri = await client.sync(path, path.read_text(encoding="utf-8", errors="replace"))
    position = {"line": max(0, int(inp.get("line") or 1) - 1), "character": max(0, int(inp.get("column") or 1) - 1)}
    doc = {"uri": uri}
    try:
        if action == "diagnostics":
            diags = await client.wait_for_diagnostics(uri, float(lsp_config().get("wait_s", 4)))
            if diags is None:
                return "The language server did not report in time."
            text = render_problems(path, workspace, diags, include_warnings=True, limit=int(lsp_config().get("max_problems", 10)) * 3)
            return text or "No problems reported."
        if action == "symbols":
            return "\n".join(_flatten_symbols(await client.call("textDocument/documentSymbol", {"textDocument": doc}))) or "(no symbols)"
        if action == "definition":
            return _locations(await client.call("textDocument/definition", {"textDocument": doc, "position": position}), workspace)
        if action == "references":
            return _locations(await client.call("textDocument/references", {
                "textDocument": doc, "position": position, "context": {"includeDeclaration": True}}), workspace)
        hover = await client.call("textDocument/hover", {"textDocument": doc, "position": position})
        contents = (hover or {}).get("contents")
        if isinstance(contents, dict):
            contents = contents.get("value")
        elif isinstance(contents, list):
            contents = "\n".join(c if isinstance(c, str) else c.get("value", "") for c in contents)
        return str(contents or "(nothing to show)")
    except LspError as exc:
        raise ToolError(str(exc))


def register_all() -> None:
    from bot.agent_runtime import toolspec

    toolspec.register(
        {"name": "lsp",
         "description": "Ask the project's language server: diagnostics (errors in a file), symbols (outline), definition, references, "
                        "hover (type / docs) at a line and column. Needs a language server configured for the file type.",
         "input_schema": {"type": "object", "properties": {
             "action": {"type": "string", "enum": ["diagnostics", "symbols", "definition", "references", "hover"]},
             "path": {"type": "string"}, "line": {"type": "integer", "description": "1-based"}, "column": {"type": "integer", "description": "1-based"}},
             "required": ["action", "path"]}},
        toolspec.ToolSpec("lsp", "read", read_only=True, concurrency_safe=False, origin="registered"), lsp_tool,
        enabled=lambda: bool(lsp_config().get("enabled")))


register_all()
