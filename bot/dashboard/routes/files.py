"""Dashboard routes: files.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

import asyncio
import html
import json
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from bot import db, desktop, envfile


def register(app: FastAPI) -> None:
    from bot.dashboard.server import (
        LOG_FILE,
        _require_tier,
        _require_token,
        _require_token_or_api_key,
        _require_token_or_bootstrap,
    )

    # Read-only, allowlisted access to specific directories on this
    # machine (see bot/file_share.py) — how a file that lives outside
    # AgenticBotPlatform's own data (e.g. a freshly built Android APK) gets reached
    # "from anywhere" over the same Funnel/Tailscale/LAN paths + auth
    # everything else already uses. Browsing/downloading within an
    # already-configured root is token-or-api-key (works from a paired
    # phone); adding/removing a root is desktop-token-only — a mobile
    # device key must never be able to mount a new directory on this
    # machine as browsable.

    @app.get("/api/files", dependencies=[Depends(_require_token_or_api_key)])
    async def api_files_roots():
        from bot import file_share

        return await asyncio.get_running_loop().run_in_executor(None, file_share.list_roots)

    @app.post("/api/files", dependencies=[Depends(_require_token)])
    async def api_files_add_root(payload: dict = Body(...)):
        from bot import file_share

        name = payload.get("name") or ""
        path = payload.get("path") or ""
        try:
            await asyncio.get_running_loop().run_in_executor(None, file_share.add_root, name, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="file_share_add_root", detail=f"added root {name!r} -> {path!r}")
        return {"ok": True}

    @app.delete("/api/files/{root}", dependencies=[Depends(_require_token)])
    async def api_files_remove_root(root: str):
        from bot import file_share

        removed = await asyncio.get_running_loop().run_in_executor(None, file_share.remove_root, root)
        if not removed:
            raise HTTPException(status_code=404, detail=f"no root named {root!r}")
        db.log_audit(actor="dashboard", action="file_share_remove_root", detail=f"removed root {root!r}")
        return {"ok": True}

    @app.get("/api/files/{root}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_files_list(root: str, path: str = ""):
        from bot import file_share

        try:
            entries = await asyncio.get_running_loop().run_in_executor(None, file_share.list_dir, root, path)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no root named {root!r}") from exc
        except file_share.PathEscapeError as exc:
            raise HTTPException(status_code=400, detail="that path escapes the root") from exc
        except (NotADirectoryError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="no such directory") from exc
        return {"entries": entries}

    @app.get("/api/files/{root}/download", dependencies=[Depends(_require_token_or_api_key)])
    async def api_files_download(root: str, path: str):
        from bot import file_share

        try:
            target = await asyncio.get_running_loop().run_in_executor(None, file_share.resolve_safe_path, root, path)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no root named {root!r}") from exc
        except file_share.PathEscapeError as exc:
            raise HTTPException(status_code=400, detail="that path escapes the root") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="no such file")
        db.log_audit(actor="dashboard", action="file_share_download", detail=f"{root}/{path}")
        return FileResponse(target, filename=target.name)

    # SSH Toolkit (https://github.com/LoopyLuci/SSH_Toolkit) - a separately maintained
    # PowerShell tool, vendored as a git submodule at vendor/ssh_toolkit and reached
    # only through bot/ssh_toolkit.py, which shells out to its own CLI - see that
    # module's docstring. _require_token (not _require_token_or_api_key): this manages
    # real SSH connection setup, not something a lower-trust paired device should touch.
    @app.get("/api/ssh-toolkit/status", dependencies=[Depends(_require_token)])
    async def api_ssh_toolkit_status():
        from bot import ssh_toolkit

        available, reason = ssh_toolkit.is_available()
        return {"available": available, "reason": reason}

    @app.get("/api/ssh-toolkit/connections", dependencies=[Depends(_require_token)])
    async def api_ssh_toolkit_connections():
        from bot import ssh_toolkit

        try:
            return {"connections": await ssh_toolkit.list_connections()}
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/ssh-toolkit/connections/{name}", dependencies=[Depends(_require_token)])
    async def api_ssh_toolkit_connection_get(name: str):
        from bot import ssh_toolkit

        try:
            return await ssh_toolkit.get_connection(name)
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/ssh-toolkit/connections", dependencies=[Depends(_require_token)])
    async def api_ssh_toolkit_connection_add(payload: dict = Body(...)):
        from bot import ssh_toolkit

        name = (payload.get("name") or "").strip()
        host_name = (payload.get("host_name") or "").strip()
        if not name or not host_name:
            raise HTTPException(status_code=400, detail="name and host_name are both required")
        try:
            await ssh_toolkit.add_connection(
                name, host_name, port=int(payload.get("port") or 22), user=payload.get("user"),
                identity_file=payload.get("identity_file"), generate_key=bool(payload.get("generate_key")),
                proxy_jump=payload.get("proxy_jump"), tags=payload.get("tags"),
                multiplex=bool(payload.get("multiplex")), force=bool(payload.get("force")),
            )
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="ssh_toolkit_add", detail=name)
        return {"ok": True}

    @app.delete("/api/ssh-toolkit/connections/{name}", dependencies=[Depends(_require_token)])
    async def api_ssh_toolkit_connection_remove(name: str):
        from bot import ssh_toolkit

        try:
            await ssh_toolkit.remove_connection(name)
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="ssh_toolkit_remove", detail=name)
        return {"ok": True}

    @app.post("/api/ssh-toolkit/connections/{name}/test", dependencies=[Depends(_require_token)])
    async def api_ssh_toolkit_connection_test(name: str):
        from bot import ssh_toolkit

        try:
            reachable = await ssh_toolkit.test_connection(name)
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"name": name, "reachable": reachable}

    @app.post("/api/ssh-toolkit/connections/{name}/run", dependencies=[Depends(_require_token)])
    async def api_ssh_toolkit_connection_run(name: str, payload: dict = Body(...)):
        from bot import ssh_toolkit

        command = (payload.get("command") or "").strip()
        if not command:
            raise HTTPException(status_code=400, detail="command is required")
        try:
            output = await ssh_toolkit.run_command(name, command)
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="ssh_toolkit_run", detail=f"{name}: {command[:200]}")
        return {"output": output}

    @app.get("/api/ssh-toolkit/status-all", dependencies=[Depends(_require_token)])
    async def api_ssh_toolkit_status_all():
        from bot import ssh_toolkit

        try:
            return {"connections": await ssh_toolkit.status_all()}
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/ssh-toolkit/graph", dependencies=[Depends(_require_token)])
    async def api_ssh_toolkit_graph():
        from bot import ssh_toolkit

        try:
            return {"nodes": await ssh_toolkit.graph()}
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/ssh-toolkit/update/check", dependencies=[Depends(_require_token)])
    async def api_ssh_toolkit_update_check():
        from bot import ssh_toolkit

        try:
            return await ssh_toolkit.check_update()
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/ssh-toolkit/update/apply", dependencies=[Depends(_require_token)])
    async def api_ssh_toolkit_update_apply():
        from bot import ssh_toolkit

        try:
            result = await ssh_toolkit.apply_update()
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="ssh_toolkit_update", detail=json.dumps(result))
        return result

    @app.get("/api/ssh-toolkit/auto-update", dependencies=[Depends(_require_token)])
    async def api_ssh_toolkit_auto_update_get():
        from bot import ssh_toolkit

        return {"mode": ssh_toolkit.get_auto_update_mode(), "options": list(ssh_toolkit.AUTO_UPDATE_MODES)}

    @app.post("/api/ssh-toolkit/auto-update", dependencies=[Depends(_require_token)])
    async def api_ssh_toolkit_auto_update_set(payload: dict = Body(...)):
        from bot import ssh_toolkit

        mode = payload.get("mode")
        try:
            ssh_toolkit.set_auto_update_mode(mode, actor="dashboard")
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"mode": ssh_toolkit.get_auto_update_mode(), "options": list(ssh_toolkit.AUTO_UPDATE_MODES)}

    # SSH Toolkit session monitor + recorder - structured live events (never
    # video/screen-share) over the existing /api/ws live-events socket
    # ("ssh_session_event", "ssh_session_started", "ssh_session_stopped",
    # "ssh_recording_state" message types), so a GUI can show every action an
    # agent or user takes over an SSH connection as it happens, plus an
    # optional durable recording. See bot/ssh_session_monitor.py.
    @app.post("/api/ssh-toolkit/session/start", dependencies=[Depends(_require_token)])
    async def api_ssh_session_start(payload: dict = Body(...)):
        from bot import ssh_session_monitor

        name = (payload.get("name") or "").strip()
        command = (payload.get("command") or "").strip()
        if not name or not command:
            raise HTTPException(status_code=400, detail="name and command are both required")
        session_id = await ssh_session_monitor.start_session(name, command)
        return {"session_id": session_id}

    @app.get("/api/ssh-toolkit/session", dependencies=[Depends(_require_token)])
    async def api_ssh_session_list():
        from bot import ssh_session_monitor

        return {"sessions": ssh_session_monitor.list_sessions()}

    @app.post("/api/ssh-toolkit/session/{session_id}/stop", dependencies=[Depends(_require_token)])
    async def api_ssh_session_stop(session_id: str):
        from bot import ssh_session_monitor, ssh_toolkit

        try:
            await ssh_session_monitor.stop_session(session_id)
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/ssh-toolkit/session/{session_id}/record/start", dependencies=[Depends(_require_token)])
    async def api_ssh_session_record_start(session_id: str):
        from bot import ssh_session_monitor, ssh_toolkit

        try:
            recording_id = ssh_session_monitor.start_recording(session_id)
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"recording_id": recording_id}

    @app.post("/api/ssh-toolkit/session/{session_id}/record/pause", dependencies=[Depends(_require_token)])
    async def api_ssh_session_record_pause(session_id: str):
        from bot import ssh_session_monitor, ssh_toolkit

        try:
            ssh_session_monitor.pause_recording(session_id)
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/ssh-toolkit/session/{session_id}/record/resume", dependencies=[Depends(_require_token)])
    async def api_ssh_session_record_resume(session_id: str):
        from bot import ssh_session_monitor, ssh_toolkit

        try:
            ssh_session_monitor.resume_recording(session_id)
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/ssh-toolkit/session/{session_id}/record/stop", dependencies=[Depends(_require_token)])
    async def api_ssh_session_record_stop(session_id: str):
        from bot import ssh_session_monitor, ssh_toolkit

        try:
            recording_id = ssh_session_monitor.stop_recording(session_id)
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"recording_id": recording_id}

    @app.get("/api/ssh-toolkit/recordings", dependencies=[Depends(_require_token)])
    async def api_ssh_recordings_list():
        from bot import ssh_session_monitor

        return {"recordings": ssh_session_monitor.list_recordings()}

    @app.get("/api/ssh-toolkit/recordings/{recording_id}", dependencies=[Depends(_require_token)])
    async def api_ssh_recording_get(recording_id: int):
        from bot import ssh_session_monitor, ssh_toolkit

        try:
            return ssh_session_monitor.get_recording(recording_id)
        except ssh_toolkit.SshToolkitError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/ssh-toolkit/recordings/{recording_id}", dependencies=[Depends(_require_token)])
    def api_ssh_recording_delete(recording_id: int):
        from bot import ssh_session_monitor

        ssh_session_monitor.delete_recording(recording_id)
        db.log_audit(actor="dashboard", action="ssh_recording_delete", detail=str(recording_id))
        return {"ok": True}

    @app.get("/api/plugins", dependencies=[Depends(_require_token)])
    async def api_plugins_list():
        from bot import plugins as plugin_registry

        return {"plugins": plugin_registry.list_plugins()}

    @app.post("/api/plugins", dependencies=[Depends(_require_token)])
    def api_plugins_install(payload: dict = Body(...)):
        from bot import plugins as plugin_registry

        try:
            info = plugin_registry.install(payload.get("path", ""))
        except plugin_registry.PluginError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="plugin_install", detail=info["name"])
        return info

    @app.post("/api/plugins/create", dependencies=[Depends(_require_token)])
    def api_plugins_create(payload: dict = Body(...)):
        from bot import plugins as plugin_registry
        from bot.envfile import PROJECT_ROOT

        name = (payload.get("name") or "").strip()
        code = payload.get("code") or ""
        if not name or not code.strip():
            raise HTTPException(status_code=400, detail="name and code are required")
        # plugins.install() derives its registered name from the FILE's
        # own stem, not its parent directory — see the matching comment
        # in bot/agent_runtime/tools.py's create_plugin tool.
        plugin_dir = PROJECT_ROOT / "data" / "plugins" / name
        plugin_dir.mkdir(parents=True, exist_ok=True)
        plugin_path = plugin_dir / f"{name}.py"
        plugin_path.write_text(code, encoding="utf-8")
        try:
            info = plugin_registry.install(str(plugin_path))
        except plugin_registry.PluginError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="plugin_create", detail=info["name"])
        return info

    @app.post("/api/plugins/{name}/enable", dependencies=[Depends(_require_token)])
    def api_plugins_enable(name: str):
        from bot import plugins as plugin_registry

        try:
            info = plugin_registry.enable(name)
        except plugin_registry.PluginError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="plugin_enable", detail=name)
        return info

    @app.post("/api/plugins/{name}/disable", dependencies=[Depends(_require_token)])
    def api_plugins_disable(name: str):
        from bot import plugins as plugin_registry

        try:
            info = plugin_registry.disable(name)
        except plugin_registry.PluginError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="plugin_disable", detail=name)
        return info

    @app.delete("/api/plugins/{name}", dependencies=[Depends(_require_token)])
    def api_plugins_delete(name: str):
        from bot import plugins as plugin_registry

        if not plugin_registry.remove(name):
            raise HTTPException(status_code=404, detail=f"no plugin named {name!r}")
        db.log_audit(actor="dashboard", action="plugin_remove", detail=name)
        return {"ok": True}

    @app.get("/api/mcp-external", dependencies=[Depends(_require_token)])
    def api_mcp_external_list(instance_id: Optional[int] = None):
        from bot.agent_runtime import mcp_client

        rows = db.list_external_mcp_servers(instance_id)
        connected = set(mcp_client.connected_servers())
        return {
            "servers": [
                {
                    "name": r["name"], "transport": r["transport"], "command": r["command"],
                    "args": json.loads(r["args_json"] or "[]"), "url": r["url"],
                    "has_auth_token": bool(r["auth_token"]), "oauth_enabled": bool(r["oauth_enabled"]),
                    "enabled": bool(r["enabled"]), "instance_id": r["instance_id"],
                    "connected": r["name"] in connected,
                    # Non-None only while an OAuth authorization is
                    # actually outstanding for this server — the
                    # dashboard/Telegram surface this as a clickable link
                    # rather than leaving it as a log-only detail.
                    "authorization_url": mcp_client.oauth_authorization_url(r["name"]),
                }
                for r in rows
            ]
        }

    @app.post("/api/mcp-external", dependencies=[Depends(_require_token)])
    async def api_mcp_external_add(payload: dict = Body(...)):
        from bot.agent_runtime import mcp_client

        name = (payload.get("name") or "").strip()
        transport = (payload.get("transport") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        if transport not in ("stdio", "remote"):
            raise HTTPException(status_code=400, detail="transport must be 'stdio' or 'remote'")
        if db.get_external_mcp_server(name) is not None:
            raise HTTPException(status_code=400, detail=f"a server named {name!r} already exists")
        db.add_external_mcp_server(
            name, transport,
            command=payload.get("command") or None, args_json=json.dumps(payload.get("args") or []),
            env_json=json.dumps(payload.get("env") or {}), url=payload.get("url") or None,
            auth_token=payload.get("auth_token") or None, oauth_enabled=bool(payload.get("oauth_enabled")),
            instance_id=payload.get("instance_id"),
        )
        db.log_audit(actor="dashboard", action="external_mcp_add", detail=name)
        ok = await mcp_client.connect(name)
        return {"ok": True, "connected": ok, "authorization_url": mcp_client.oauth_authorization_url(name)}

    @app.get("/api/mcp-external/oauth/callback")
    async def api_mcp_external_oauth_callback(code: str = "", state: str = "", error: str = ""):
        """Where the operator's browser lands after granting (or denying)
        consent for an OAuth-enabled external MCP server — see
        bot/agent_runtime/mcp_client.py's OAuthClientProvider wiring.
        Deliberately no auth dependency: the redirecting OAuth provider
        can't carry AgenticBotPlatform's own dashboard token, and this endpoint's
        real security boundary is the unguessable, single-use `state`
        value this process itself minted for the one pending flow it
        correlates against (deliver_oauth_callback), not a bearer token —
        the same security model every OAuth redirect endpoint uses."""
        from bot.agent_runtime import mcp_client

        if error:
            return HTMLResponse(f"<h3>Authorization failed: {html.escape(error)}</h3><p>You can close this tab.</p>")
        if mcp_client.deliver_oauth_callback(state, code):
            return HTMLResponse("<h3>Authorized.</h3><p>You can close this tab and return to AgenticBotPlatform.</p>")
        return HTMLResponse(
            "<h3>No matching pending authorization found.</h3>"
            "<p>It may have already expired — try connecting the server again from the dashboard.</p>"
        )

    @app.post("/api/mcp-external/{name}/enable", dependencies=[Depends(_require_token)])
    async def api_mcp_external_enable(name: str):
        from bot.agent_runtime import mcp_client

        if db.get_external_mcp_server(name) is None:
            raise HTTPException(status_code=404, detail=f"no external MCP server named {name!r}")
        db.set_external_mcp_server_enabled(name, True)
        db.log_audit(actor="dashboard", action="external_mcp_enable", detail=name)
        ok = await mcp_client.connect(name)
        return {"ok": True, "connected": ok}

    @app.post("/api/mcp-external/{name}/disable", dependencies=[Depends(_require_token)])
    async def api_mcp_external_disable(name: str):
        from bot.agent_runtime import mcp_client

        if db.get_external_mcp_server(name) is None:
            raise HTTPException(status_code=404, detail=f"no external MCP server named {name!r}")
        db.set_external_mcp_server_enabled(name, False)
        await mcp_client.disconnect(name)
        db.log_audit(actor="dashboard", action="external_mcp_disable", detail=name)
        return {"ok": True}

    @app.delete("/api/mcp-external/{name}", dependencies=[Depends(_require_token)])
    async def api_mcp_external_delete(name: str):
        from bot.agent_runtime import mcp_client

        await mcp_client.disconnect(name)
        if not db.delete_external_mcp_server(name):
            raise HTTPException(status_code=404, detail=f"no external MCP server named {name!r}")
        db.log_audit(actor="dashboard", action="external_mcp_remove", detail=name)
        return {"ok": True}

    # Hooks/agent-settings/auto-manage (through the end of api_auto_manage_set
    # below) are reachable by a paired mobile device key, not just the
    # desktop token — same tier as /api/bots and /api/config/set (see
    # _identify_caller's own docstring): a lost/unlocked phone that could
    # already rewrite bot credentials or config is no more exposed by also
    # being able to add a hook or flip an agent setting. Widened from the
    # original desktop-only _require_token when the Android app's own
    # Automation screen was built, mirroring exactly the same audit-logging
    # tradeoff api_config_set already makes (actor="dashboard" regardless
    # of which caller kind actually authenticated).
    @app.get("/api/hooks", dependencies=[Depends(_require_token_or_api_key)])
    def api_hooks_list(event: Optional[str] = None):
        rows = db.list_agent_hooks(event=event)
        return {
            "hooks": [
                {
                    "id": r["id"], "event": r["event"], "matcher": r["matcher"], "command": r["command"],
                    "instance_id": r["instance_id"], "enabled": bool(r["enabled"]),
                }
                for r in rows
            ]
        }

    @app.post("/api/hooks", dependencies=[Depends(_require_tier("unrestricted"))])
    def api_hooks_add(payload: dict = Body(...)):
        from bot.agent_runtime import hooks as agent_hooks

        event = (payload.get("event") or "").strip()
        command = (payload.get("command") or "").strip()
        if event not in agent_hooks.VALID_EVENTS:
            raise HTTPException(status_code=400, detail=f"event must be one of {sorted(agent_hooks.VALID_EVENTS)}")
        if not command:
            raise HTTPException(status_code=400, detail="command is required")
        hook_id = db.add_agent_hook(
            event, command, matcher=payload.get("matcher") or None, instance_id=payload.get("instance_id"),
        )
        db.log_audit(actor="dashboard", action="agent_hook_add", detail=f"#{hook_id} {event}")
        return {"ok": True, "id": hook_id}

    @app.post("/api/hooks/{hook_id}/enable", dependencies=[Depends(_require_tier("unrestricted"))])
    def api_hooks_enable(hook_id: int):
        if db.get_agent_hook(hook_id) is None:
            raise HTTPException(status_code=404, detail=f"no hook #{hook_id}")
        db.set_agent_hook_enabled(hook_id, True)
        db.log_audit(actor="dashboard", action="agent_hook_enable", detail=str(hook_id))
        return {"ok": True}

    @app.post("/api/hooks/{hook_id}/disable", dependencies=[Depends(_require_token_or_api_key)])
    def api_hooks_disable(hook_id: int):
        if db.get_agent_hook(hook_id) is None:
            raise HTTPException(status_code=404, detail=f"no hook #{hook_id}")
        db.set_agent_hook_enabled(hook_id, False)
        db.log_audit(actor="dashboard", action="agent_hook_disable", detail=str(hook_id))
        return {"ok": True}

    @app.delete("/api/hooks/{hook_id}", dependencies=[Depends(_require_token_or_api_key)])
    def api_hooks_delete(hook_id: int):
        if not db.delete_agent_hook(hook_id):
            raise HTTPException(status_code=404, detail=f"no hook #{hook_id}")
        db.log_audit(actor="dashboard", action="agent_hook_remove", detail=str(hook_id))
        return {"ok": True}

    @app.get("/api/skills", dependencies=[Depends(_require_token)])
    def api_skills_list(instance_id: Optional[int] = None):
        from bot import skills as bot_skills

        return {"skills": bot_skills.list_for_instance(instance_id)}

    @app.post("/api/skills", dependencies=[Depends(_require_token)])
    def api_skills_create(payload: dict = Body(...)):
        from bot import skills as bot_skills

        try:
            info = bot_skills.create(
                payload.get("instance_id"),
                payload.get("name", ""),
                payload.get("description", ""),
                payload.get("content", ""),
                global_=bool(payload.get("global_")),
            )
        except bot_skills.SkillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="skill_create", detail=info["name"])
        return info

    @app.delete("/api/skills/{name}", dependencies=[Depends(_require_token)])
    def api_skills_delete(name: str, instance_id: Optional[int] = None):
        from bot import skills as bot_skills

        if not bot_skills.remove(instance_id, name):
            raise HTTPException(status_code=404, detail=f"no skill named {name!r}")
        db.log_audit(actor="dashboard", action="skill_remove", detail=name)
        return {"ok": True}

    @app.get("/api/agent-settings", dependencies=[Depends(_require_token_or_api_key)])
    async def api_agent_settings_get(instance_id: Optional[int] = None, own: bool = False):
        from bot import agent_settings

        # own=true returns only what this instance has set itself (None where it inherits), not the resolved values.
        return agent_settings.get_own(instance_id) if own else agent_settings.get(instance_id)

    @app.post("/api/agent-settings", dependencies=[Depends(_require_token_or_api_key)])
    def api_agent_settings_set(payload: dict = Body(...)):
        from bot import agent_settings

        instance_id = payload.get("instance_id")
        fields = {k: v for k, v in payload.items() if k in agent_settings.FIELDS}
        try:
            result = agent_settings.set_settings(instance_id, **fields)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="agent_settings_update", detail=f"instance {instance_id}: {fields}")
        return result

    @app.get("/api/auto-manage/{instance_id}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_auto_manage_get(instance_id: int):
        from bot import auto_manage

        try:
            return auto_manage.get_config(instance_id)
        except auto_manage.AutoManageError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/auto-manage/{instance_id}", dependencies=[Depends(_require_token_or_api_key)])
    def api_auto_manage_set(instance_id: int, payload: dict = Body(...)):
        from bot import auto_manage

        try:
            if payload.get("enabled") is False:
                result = auto_manage.disable(instance_id, actor="dashboard")
            elif payload.get("enabled") is True:
                result = auto_manage.enable(
                    instance_id,
                    chat_id=payload.get("chat_id"),
                    thread_id=payload.get("thread_id"),
                    trigger=payload.get("trigger", "scheduled"),
                    interval=payload.get("interval", "30m"),
                    goal_template=payload.get("goal_template"),
                    actor="dashboard",
                )
            else:
                fields = {k: v for k, v in payload.items() if k in ("trigger", "interval", "goal_template", "chat_id", "thread_id")}
                result = auto_manage.set_config(instance_id, actor="dashboard", **fields)
        except auto_manage.AutoManageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="auto_manage_update", detail=f"instance {instance_id}: {payload}")
        return result

    @app.get("/api/estop", dependencies=[Depends(_require_token_or_api_key)])
    async def api_estop_get():
        from bot.agent_runtime import estop

        return estop.status()

    @app.post("/api/estop", dependencies=[Depends(_require_token)])
    async def api_estop_set(payload: dict = Body(...)):
        from bot.agent_runtime import estop

        if payload.get("engaged"):
            return estop.engage(payload.get("reason"), actor="dashboard")
        return estop.disengage(actor="dashboard")

    @app.get("/api/personas", dependencies=[Depends(_require_token_or_api_key)])
    async def api_personas():
        from bot.personas import list_personas

        return list_personas()

    @app.get("/api/mcp", dependencies=[Depends(_require_token_or_api_key)])
    async def api_mcp():
        return desktop.list_mcp_servers()

    @app.get("/api/mcp/{name}/logs", dependencies=[Depends(_require_token_or_api_key)])
    async def api_mcp_logs(name: str, lines: int = 50):
        return {"lines": desktop.tail_mcp_log(name, lines=lines)}

    @app.get("/api/logs", dependencies=[Depends(_require_token_or_api_key)])
    def api_logs(lines: int = 100, level: Optional[str] = None):
        if not LOG_FILE.exists():
            return {"lines": []}
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()[-2000:]
        if level and level != "all":
            all_lines = [ln for ln in all_lines if f" {level.upper()} " in ln or f" {level.upper()}   " in ln]
        return {"lines": [ln.rstrip("\n") for ln in all_lines[-lines:]]}

    @app.get("/api/env", dependencies=[Depends(_require_token_or_api_key)])
    def api_env():
        return envfile.status()

    # Contents/backups expose secret values, unlike every other GET in this
    # API — token-gated even though they're reads. _require_token_or_bootstrap
    # rather than _require_token: this is also the only path that can set
    # the first DASHBOARD_TOKEN, so it can't itself demand one already exist.
    @app.get("/api/env/content", dependencies=[Depends(_require_token_or_bootstrap)])
    def api_env_content():
        return {"content": envfile.read_content(), "path": str(envfile.resolve())}

    @app.post("/api/env/content", dependencies=[Depends(_require_token_or_bootstrap)])
    def api_env_content_save(payload: dict = Body(...)):
        content = payload.get("content")
        if content is None:
            raise HTTPException(status_code=400, detail="payload must be {content: str}")
        backup = envfile.write_content(content, actor="dashboard")
        return {"ok": True, "backup": backup.name if backup else None}

    # Agent-safe write path: sets one key without ever returning file
    # content, unlike /api/env/content above — an agent (or any caller)
    # can add/update a setting but can never read DASHBOARD_TOKEN or any
    # other existing value through this route.
    @app.post("/api/env/set", dependencies=[Depends(_require_token_or_bootstrap)])
    def api_env_set(payload: dict = Body(...)):
        key = payload.get("key")
        value = payload.get("value")
        if not key or value is None:
            raise HTTPException(status_code=400, detail="payload must be {key: str, value: str}")
        try:
            envfile.set_var(key, str(value), actor="dashboard")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/api/env/backups", dependencies=[Depends(_require_token_or_bootstrap)])
    def api_env_backups():
        return envfile.list_backups()

    @app.post("/api/env/backups/{name}/restore", dependencies=[Depends(_require_token_or_bootstrap)])
    def api_env_restore(name: str):
        try:
            envfile.restore_backup(name, actor="dashboard")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}
