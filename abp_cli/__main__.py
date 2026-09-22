"""python -m abp_cli <command> [options] - a scriptable command-line client for a running
AgenticBotPlatform dashboard.

Talks to the same REST API the web dashboard, the desktop app, and bot/tui/ use
(bot/dashboard_client.py's DashboardClient) - nothing here reads bot.* business-logic
modules directly, so it works against a remote/federated instance exactly like the
desktop app already does, not just a local one.

Commands:
  bots list|show|create|edit|delete|start|stop|restart|enable|disable
  chat <instance-id> <text...>            a real turn, same route the Android app uses
  agent-settings get|set                  a bot's own permission mode, sub-agent limits, ...
  agent-config schema|get|set             the ~65 native_agent.* settings (ABP Agents page)
  providers list|add|remove|catalog|models|toggle|restore

Connection: --host (default 127.0.0.1:8787) and --token (default from .env's
DASHBOARD_TOKEN, same as bot/tui/'s ConnectScreen). --json prints machine-readable output
instead of plain text/tables, on every command.

Exit status: 0 done | 1 the request failed | 2 bad usage.
See docs/agents/cli-tui.md for what this covers today and what's still dashboard-only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from typing import Any, Optional

from bot.dashboard_client import ApiError, DashboardClient, default_connection


def _client(args) -> DashboardClient:
    default_host, default_token = default_connection()
    host = args.host or default_host
    if not host.startswith("http"):
        host = f"http://{host}"
    token = args.token or default_token
    if not token:
        print("no dashboard token — pass --token or set DASHBOARD_TOKEN in .env", file=sys.stderr)
        raise SystemExit(2)
    return DashboardClient(host, token)


def _print(args, data: Any, *, table: Optional[list[str]] = None) -> None:
    if args.json:
        print(json.dumps(data, indent=1))
        return
    if table and isinstance(data, list):
        rows = [[str(row.get(col, "")) for col in table] for row in data]
        widths = [max(len(col), *(len(r[i]) for r in rows)) if rows else len(col) for i, col in enumerate(table)]
        print("  ".join(col.ljust(w) for col, w in zip(table, widths)))
        for r in rows:
            print("  ".join(v.ljust(w) for v, w in zip(r, widths)))
        return
    print(json.dumps(data, indent=1) if isinstance(data, (dict, list)) else data)


def _parse_kv_pairs(pairs: list[str]) -> dict:
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            print(f"expected key=value, got {pair!r}", file=sys.stderr)
            raise SystemExit(2)
        key, _, raw = pair.partition("=")
        if raw.lower() in ("true", "false"):
            value: Any = raw.lower() == "true"
        elif raw.lower() in ("null", "none", ""):
            value = None
        else:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
        out[key] = value
    return out


def _ids(raw: Optional[str]) -> list:
    if not raw:
        return []
    out = []
    for s in raw.split(","):
        s = s.strip()
        if not s:
            continue
        try:
            out.append(int(s))
        except ValueError:
            out.append(s)
    return out


async def _run(args) -> int:
    client = _client(args)
    try:
        return await _dispatch(args, client)
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — a network/connection failure, not an API error
        print(f"couldn't reach the dashboard: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()


async def _dispatch(args, client: DashboardClient) -> int:
    cmd = args.cmd
    if cmd == "bots":
        return await _bots(args, client)
    if cmd == "chat":
        result = await client.send_to_bot(args.instance_id, " ".join(args.text))
        _print(args, result if args.json else result["reply"])
        return 0
    if cmd == "agent-settings":
        return await _agent_settings(args, client)
    if cmd == "agent-config":
        return await _agent_config(args, client)
    if cmd == "providers":
        return await _providers(args, client)
    if cmd == "swarms":
        return await _swarms(args, client)
    if cmd == "sessions":
        return await _sessions(args, client)
    if cmd == "terminal":
        out = await client.terminal_exec(" ".join(args.text), instance_id=args.instance)
        _print(args, out)
        return 0
    if cmd == "hooks":
        return await _hooks(args, client)
    if cmd == "plugins":
        return await _plugins(args, client)
    if cmd == "skills":
        return await _skills(args, client)
    if cmd == "mcp":
        return await _mcp(args, client)
    if cmd == "security":
        return await _security(args, client)
    if cmd == "snapshots":
        return await _snapshots(args, client)
    if cmd == "env":
        _print(args, await client.env_status())
        return 0
    if cmd == "config":
        return await _config(args, client)
    if cmd == "diagnostics":
        return await _diagnostics(args, client)
    if cmd == "peers":
        return await _peers(args, client)
    if cmd == "kanban":
        return await _kanban(args, client)
    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


async def _swarms(args, client: DashboardClient) -> int:
    sub = args.swarms_cmd
    if sub == "list":
        _print(args, await client.list_swarms(), table=["id", "name", "strategy", "enabled"])
        return 0
    if sub == "show":
        _print(args, await client.get_swarm(args.swarm_id))
        return 0
    if sub == "create":
        config = json.loads(args.config) if args.config else {}
        _print(args, await client.create_swarm(args.name, args.strategy, config, enabled=not args.disabled))
        return 0
    if sub == "delete":
        _print(args, await client.delete_swarm(args.swarm_id))
        return 0
    if sub in ("enable", "disable"):
        method = client.enable_swarm if sub == "enable" else client.disable_swarm
        _print(args, await method(args.swarm_id))
        return 0
    if sub == "run":
        _print(args, await client.run_swarm(args.swarm_id, " ".join(args.prompt), source_instance=args.source))
        return 0
    if sub == "runs":
        _print(args, await client.list_swarm_runs(swarm_id=args.swarm_id, limit=args.limit),
              table=["id", "swarm_id", "swarm_run_id", "status"])
        return 0
    if sub == "run-show":
        _print(args, await client.get_swarm_run(args.swarm_run_id))
        return 0
    if sub == "run-cancel":
        _print(args, await client.cancel_swarm_run(args.swarm_run_id))
        return 0
    print(f"unknown swarms subcommand {sub!r}", file=sys.stderr)
    return 2


async def _sessions(args, client: DashboardClient) -> int:
    sub = args.sessions_cmd
    if sub == "list":
        _print(args, await client.list_sessions(instance_id=args.instance, q=args.query, limit=args.limit),
              table=["id", "instance_id", "title", "item_count"])
        return 0
    if sub == "show":
        _print(args, await client.get_session(args.session_id))
        return 0
    if sub == "delete":
        _print(args, await client.delete_session(args.session_id))
        return 0
    if sub == "new":
        _print(args, await client.new_bot_session(args.instance_id))
        return 0
    print(f"unknown sessions subcommand {sub!r}", file=sys.stderr)
    return 2


async def _hooks(args, client: DashboardClient) -> int:
    sub = args.hooks_cmd
    if sub == "list":
        _print(args, await client.list_hooks(event=args.event), table=["id", "event", "command", "enabled"])
        return 0
    if sub == "add":
        _print(args, await client.add_hook(args.event, args.command, matcher=args.matcher, instance_id=args.instance))
        return 0
    if sub in ("enable", "disable"):
        method = client.enable_hook if sub == "enable" else client.disable_hook
        _print(args, await method(args.hook_id))
        return 0
    if sub == "remove":
        _print(args, await client.delete_hook(args.hook_id))
        return 0
    print(f"unknown hooks subcommand {sub!r}", file=sys.stderr)
    return 2


async def _plugins(args, client: DashboardClient) -> int:
    sub = args.plugins_cmd
    if sub == "list":
        _print(args, await client.list_plugins(), table=["name", "version", "enabled"])
        return 0
    if sub == "install":
        _print(args, await client.install_plugin(args.path))
        return 0
    if sub == "create":
        code = pathlib.Path(args.code_file).read_text(encoding="utf-8") if args.code_file else args.code
        _print(args, await client.create_plugin(args.name, code or ""))
        return 0
    if sub in ("enable", "disable"):
        method = client.enable_plugin if sub == "enable" else client.disable_plugin
        _print(args, await method(args.name))
        return 0
    if sub == "remove":
        _print(args, await client.delete_plugin(args.name))
        return 0
    print(f"unknown plugins subcommand {sub!r}", file=sys.stderr)
    return 2


async def _skills(args, client: DashboardClient) -> int:
    sub = args.skills_cmd
    if sub == "list":
        _print(args, await client.list_skills(instance_id=args.instance), table=["name", "description"])
        return 0
    if sub == "create":
        content = pathlib.Path(args.content_file).read_text(encoding="utf-8") if args.content_file else (args.content or "")
        _print(args, await client.create_skill(args.instance, args.name, args.description or "", content, is_global=args.global_))
        return 0
    if sub == "remove":
        _print(args, await client.delete_skill(args.name, instance_id=args.instance))
        return 0
    if sub == "packs":
        _print(args, await client.skill_packs(), table=["name", "description", "source"])
        return 0
    if sub == "fetch":
        _print(args, await client.fetch_skill_pack(args.url, ref=args.ref, subdir=args.subdir))
        return 0
    if sub == "quarantine":
        _print(args, await client.skill_quarantine())
        return 0
    if sub == "approve-quarantine":
        _print(args, await client.review_skill_quarantine(args.name, "approve"))
        return 0
    if sub == "reject-quarantine":
        _print(args, await client.review_skill_quarantine(args.name, "reject"))
        return 0
    if sub == "drafts":
        _print(args, await client.skill_drafts())
        return 0
    if sub == "approve-draft":
        _print(args, await client.review_skill_draft(args.name, "approve"))
        return 0
    if sub == "reject-draft":
        _print(args, await client.review_skill_draft(args.name, "reject"))
        return 0
    print(f"unknown skills subcommand {sub!r}", file=sys.stderr)
    return 2


async def _mcp(args, client: DashboardClient) -> int:
    sub = args.mcp_cmd
    if sub == "list":
        _print(args, await client.list_mcp_servers(), table=["name", "command", "enabled"])
        return 0
    if sub == "logs":
        for line in await client.mcp_server_logs(args.name, lines=args.lines):
            print(line)
        return 0
    if sub in ("enable", "disable"):
        method = client.enable_mcp_server if sub == "enable" else client.disable_mcp_server
        _print(args, await method(args.name))
        return 0
    if sub == "pins":
        _print(args, await client.mcp_pins())
        return 0
    if sub == "approve-pin":
        _print(args, await client.approve_mcp_pin(args.server, args.tool))
        return 0
    if sub == "external-list":
        _print(args, await client.list_external_mcp_servers(instance_id=args.instance),
              table=["name", "transport", "url", "enabled", "connected"])
        return 0
    if sub == "external-add":
        server_args = json.loads(args.args) if args.args else None
        _print(args, await client.add_external_mcp_server(
            args.name, args.transport, command=args.command, args=server_args, url=args.url,
            auth_token=args.auth_token, oauth_enabled=args.oauth, instance_id=args.instance,
        ))
        return 0
    if sub in ("external-enable", "external-disable"):
        method = client.enable_external_mcp_server if sub == "external-enable" else client.disable_external_mcp_server
        _print(args, await method(args.name))
        return 0
    if sub == "external-remove":
        _print(args, await client.remove_external_mcp_server(args.name))
        return 0
    print(f"unknown mcp subcommand {sub!r}", file=sys.stderr)
    return 2


async def _security(args, client: DashboardClient) -> int:
    sub = args.security_cmd
    if sub == "allowed-users":
        _print(args, await client.list_allowed_users())
        return 0
    if sub == "allow-user":
        _print(args, await client.add_allowed_user(args.telegram_id, name=args.name))
        return 0
    if sub == "disallow-user":
        _print(args, await client.remove_allowed_user(args.telegram_id))
        return 0
    if sub == "permissions":
        _print(args, await client.get_permissions())
        return 0
    if sub == "instance-permissions":
        _print(args, await client.get_instance_permissions(args.instance_id))
        return 0
    if sub == "set-instance-permissions":
        _print(args, await client.set_instance_permissions(args.instance_id, mode=args.mode))
        return 0
    if sub == "devices":
        _print(args, await client.list_devices(), table=["label", "tier", "last_used_at"])
        return 0
    if sub == "mobile-keys":
        _print(args, await client.list_mobile_keys(), table=["id", "label", "permission_tier"])
        return 0
    if sub == "create-mobile-key":
        _print(args, await client.create_mobile_key(args.label, args.tier, host=args.host))
        return 0
    if sub == "revoke-mobile-key":
        _print(args, await client.delete_mobile_key(args.key_id))
        return 0
    print(f"unknown security subcommand {sub!r}", file=sys.stderr)
    return 2


async def _snapshots(args, client: DashboardClient) -> int:
    sub = args.snapshots_cmd
    if sub == "list":
        _print(args, await client.list_snapshots(), table=["name", "label", "created_at"])
        return 0
    if sub == "create":
        _print(args, await client.create_snapshot(label=args.label))
        return 0
    if sub == "restore":
        _print(args, await client.restore_snapshot(args.name))
        return 0
    if sub == "remove":
        _print(args, await client.delete_snapshot(args.name))
        return 0
    print(f"unknown snapshots subcommand {sub!r}", file=sys.stderr)
    return 2


async def _config(args, client: DashboardClient) -> int:
    sub = args.config_cmd
    if sub == "get":
        _print(args, await client.get_config())
        return 0
    if sub == "reload":
        _print(args, await client.reload_config())
        return 0
    if sub == "set":
        value = _parse_kv_pairs([f"v={args.value}"])["v"]
        _print(args, await client.set_config_path(args.path.split("."), value))
        return 0
    print(f"unknown config subcommand {sub!r}", file=sys.stderr)
    return 2


async def _diagnostics(args, client: DashboardClient) -> int:
    sub = args.diagnostics_cmd
    if sub == "summary":
        _print(args, await client.diagnostics_summary())
        return 0
    if sub == "crash-reports":
        _print(args, await client.crash_reports(limit=args.limit))
        return 0
    print(f"unknown diagnostics subcommand {sub!r}", file=sys.stderr)
    return 2


async def _peers(args, client: DashboardClient) -> int:
    sub = args.peers_cmd
    if sub == "list":
        _print(args, await client.list_peers(), table=["id", "name", "base_url", "last_seen_at"])
        return 0
    if sub == "self-address":
        _print(args, await client.peer_self_address())
        return 0
    if sub == "pairing-token":
        _print(args, await client.create_peer_pairing_token(base_url=args.base_url))
        return 0
    if sub == "link":
        _print(args, await client.link_peer(args.name, args.pairing_token, my_base_url=args.my_base_url))
        return 0
    if sub == "remove":
        _print(args, await client.remove_peer(args.peer_id))
        return 0
    if sub == "overview":
        _print(args, await client.peer_overview(args.peer_id))
        return 0
    if sub == "bots":
        _print(args, await client.peer_bots(args.peer_id))
        return 0
    print(f"unknown peers subcommand {sub!r}", file=sys.stderr)
    return 2


async def _kanban(args, client: DashboardClient) -> int:
    sub = args.kanban_cmd
    if sub == "boards":
        _print(args, await client.kanban_boards(instance_id=args.instance))
        return 0
    if sub == "cards":
        _print(args, await client.kanban_cards(instance_id=args.instance, board=args.board),
              table=["id", "column", "text"])
        return 0
    if sub == "add":
        _print(args, await client.add_kanban_card(args.instance, args.text, board=args.board, column=args.column))
        return 0
    if sub == "move":
        _print(args, await client.move_kanban_card(args.card_id, args.instance, args.column))
        return 0
    if sub == "remove":
        _print(args, await client.delete_kanban_card(args.card_id, args.instance))
        return 0
    print(f"unknown kanban subcommand {sub!r}", file=sys.stderr)
    return 2


async def _bots(args, client: DashboardClient) -> int:
    sub = args.bots_cmd
    if sub == "list":
        bots = await client.list_bots()
        _print(args, bots, table=["id", "name", "platform", "backend", "enabled", "live_running"])
        return 0
    if sub == "show":
        _print(args, await client.get_bot(args.instance_id))
        return 0
    if sub == "create":
        try:
            credentials = json.loads(args.credentials) if args.credentials else {}
        except json.JSONDecodeError as exc:
            print(f"--credentials must be valid JSON: {exc}", file=sys.stderr)
            raise SystemExit(2)
        payload = {
            "name": args.name, "platform": args.platform, "backend": args.backend,
            "credentials": credentials, "allowed_user_ids": _ids(args.allowed), "admin_user_ids": _ids(args.admins),
        }
        if args.model:
            payload["model"] = args.model
        result = await client.create_bot(payload)
        _print(args, result)
        return 0
    if sub == "edit":
        payload = {}
        if args.name is not None:
            payload["name"] = args.name
        if args.backend is not None:
            payload["backend"] = args.backend
        if args.model is not None:
            payload["model"] = args.model
        if args.allowed is not None:
            payload["allowed_user_ids"] = _ids(args.allowed)
        if args.admins is not None:
            payload["admin_user_ids"] = _ids(args.admins)
        if not payload:
            print("nothing to change — pass at least one of --name/--backend/--model/--allowed/--admins", file=sys.stderr)
            return 2
        _print(args, await client.update_bot(args.instance_id, payload))
        return 0
    if sub == "delete":
        _print(args, await client.delete_bot(args.instance_id))
        return 0
    if sub in ("start", "stop", "restart", "enable", "disable"):
        method = {"start": client.start_bot, "stop": client.stop_bot, "restart": client.restart_bot,
                  "enable": client.enable_bot, "disable": client.disable_bot}[sub]
        _print(args, await method(args.instance_id))
        return 0
    print(f"unknown bots subcommand {sub!r}", file=sys.stderr)
    return 2


async def _agent_settings(args, client: DashboardClient) -> int:
    if args.settings_cmd == "get":
        _print(args, await client.get_agent_settings(args.instance, own=args.own))
        return 0
    if args.settings_cmd == "set":
        fields = _parse_kv_pairs(args.fields)
        _print(args, await client.set_agent_settings(args.instance, **fields))
        return 0
    print(f"unknown agent-settings subcommand {args.settings_cmd!r}", file=sys.stderr)
    return 2


async def _agent_config(args, client: DashboardClient) -> int:
    if args.config_cmd == "schema":
        _print(args, await client.get_agent_config_schema())
        return 0
    if args.config_cmd == "get":
        _print(args, await client.get_agent_config())
        return 0
    if args.config_cmd == "set":
        changes = _parse_kv_pairs(args.changes)
        _print(args, await client.set_agent_config(changes))
        return 0
    print(f"unknown agent-config subcommand {args.config_cmd!r}", file=sys.stderr)
    return 2


async def _providers(args, client: DashboardClient) -> int:
    sub = args.providers_cmd
    if sub == "list":
        _print(args, await client.list_providers(), table=["name", "base_url", "protocol"])
        return 0
    if sub == "add":
        _print(args, await client.set_provider(
            args.name, args.base_url, protocol=args.protocol, api_key_env=args.api_key_env,
            api_key=args.api_key, catalog_id=args.catalog_id,
        ))
        return 0
    if sub == "remove":
        _print(args, await client.delete_provider(args.name))
        return 0
    if sub == "catalog":
        _print(args, await client.provider_catalog())
        return 0
    if sub == "models":
        _print(args, await client.provider_models(args.name, refresh=args.refresh), table=["id", "free", "context"])
        return 0
    if sub == "toggle":
        _print(args, await client.toggle_provider_model(args.name, args.model_id, enabled=not args.disable))
        return 0
    if sub == "restore":
        _print(args, await client.restore_provider(args.name, api_key=args.api_key))
        return 0
    print(f"unknown providers subcommand {sub!r}", file=sys.stderr)
    return 2


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="abp_cli", description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=None, help="host:port of the dashboard (default: 127.0.0.1:8787)")
    ap.add_argument("--token", default=None, help="dashboard token (default: .env's DASHBOARD_TOKEN)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    bots = sub.add_parser("bots", help="manage bot instances")
    bsub = bots.add_subparsers(dest="bots_cmd", required=True)
    bsub.add_parser("list")
    p = bsub.add_parser("show"); p.add_argument("instance_id", type=int)
    p = bsub.add_parser("create")
    p.add_argument("--name", required=True)
    p.add_argument("--platform", required=True)
    p.add_argument("--backend", required=True)
    p.add_argument("--credentials", help="JSON object, e.g. '{\"bot_token\": \"...\"}' (default: {})")
    p.add_argument("--allowed", help="comma-separated allowed user ids")
    p.add_argument("--admins", help="comma-separated admin user ids")
    p.add_argument("--model", default=None)
    p = bsub.add_parser("edit")
    p.add_argument("instance_id", type=int)
    p.add_argument("--name", default=None)
    p.add_argument("--backend", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--allowed", default=None, help="comma-separated, replaces the current list")
    p.add_argument("--admins", default=None, help="comma-separated, replaces the current list")
    for name in ("delete", "start", "stop", "restart", "enable", "disable"):
        p = bsub.add_parser(name); p.add_argument("instance_id", type=int)

    p = sub.add_parser("chat", help="send a real message to a bot and print its reply")
    p.add_argument("instance_id", type=int)
    p.add_argument("text", nargs="+", help="the message; joined with spaces")

    ags = sub.add_parser("agent-settings", help="a bot's own agent settings (permission mode, sub-agent limits, ...)")
    asub = ags.add_subparsers(dest="settings_cmd", required=True)
    p = asub.add_parser("get")
    p.add_argument("--instance", type=int, default=None, help="omit for the global defaults")
    p.add_argument("--own", action="store_true", help="only what this instance set itself, not the resolved values")
    p = asub.add_parser("set")
    p.add_argument("--instance", type=int, default=None, help="omit to set the global defaults")
    p.add_argument("fields", nargs="+", help="field=value pairs, e.g. worker_effort=high fallback_model=openrouter/x:free")

    acfg = sub.add_parser("agent-config", help="the ~65 native_agent.* settings the ABP Agents page edits")
    csub = acfg.add_subparsers(dest="config_cmd", required=True)
    csub.add_parser("schema")
    csub.add_parser("get")
    p = csub.add_parser("set")
    p.add_argument("changes", nargs="+", help="id=value pairs, e.g. sandbox.backend=docker web.enabled=true")

    prov = sub.add_parser("providers", help="named model providers (config/providers.yaml)")
    psub = prov.add_subparsers(dest="providers_cmd", required=True)
    psub.add_parser("list")
    p = psub.add_parser("add")
    p.add_argument("--name", required=True)
    p.add_argument("--base-url", required=True, dest="base_url")
    p.add_argument("--protocol", default="openai")
    p.add_argument("--api-key-env", default=None, dest="api_key_env")
    p.add_argument("--api-key", default=None, dest="api_key")
    p.add_argument("--catalog-id", default=None, dest="catalog_id")
    p = psub.add_parser("remove"); p.add_argument("name")
    psub.add_parser("catalog")
    p = psub.add_parser("models")
    p.add_argument("name")
    p.add_argument("--refresh", action="store_true")
    p = psub.add_parser("toggle")
    p.add_argument("name")
    p.add_argument("model_id")
    p.add_argument("--disable", action="store_true", help="turn the model off instead of on")
    p = psub.add_parser("restore")
    p.add_argument("name")
    p.add_argument("--api-key", default=None, dest="api_key")

    swarms = sub.add_parser("swarms", help="fan-out/leader-vote/etc. multi-bot swarms")
    ssub = swarms.add_subparsers(dest="swarms_cmd", required=True)
    ssub.add_parser("list")
    p = ssub.add_parser("show"); p.add_argument("swarm_id", type=int)
    p = ssub.add_parser("create")
    p.add_argument("--name", required=True)
    p.add_argument("--strategy", required=True, help="fanout_synthesize | leader_vote | sequential_relay | decompose_delegate | custom")
    p.add_argument("--config", help="JSON object, strategy-specific (default: {})")
    p.add_argument("--disabled", action="store_true")
    p = ssub.add_parser("delete"); p.add_argument("swarm_id", type=int)
    for name in ("enable", "disable"):
        p = ssub.add_parser(name); p.add_argument("swarm_id", type=int)
    p = ssub.add_parser("run")
    p.add_argument("swarm_id", type=int)
    p.add_argument("prompt", nargs="+")
    p.add_argument("--source", type=int, default=None, dest="source", help="the requesting bot instance id, for agent_control.can_target enforcement")
    p = ssub.add_parser("runs")
    p.add_argument("--swarm-id", type=int, default=None, dest="swarm_id")
    p.add_argument("--limit", type=int, default=50)
    p = ssub.add_parser("run-show"); p.add_argument("swarm_run_id")
    p = ssub.add_parser("run-cancel"); p.add_argument("swarm_run_id")

    sessions = sub.add_parser("sessions", help="a bot's conversation sessions")
    sesub = sessions.add_subparsers(dest="sessions_cmd", required=True)
    p = sesub.add_parser("list")
    p.add_argument("--instance", type=int, default=None)
    p.add_argument("--query", default=None, dest="query")
    p.add_argument("--limit", type=int, default=50)
    p = sesub.add_parser("show"); p.add_argument("session_id")
    p = sesub.add_parser("delete"); p.add_argument("session_id")
    p = sesub.add_parser("new"); p.add_argument("instance_id", type=int)

    term = sub.add_parser("terminal", help="run one ABP slash command (not a raw shell)")
    term.add_argument("text", nargs="+")
    term.add_argument("--instance", type=int, default=None)

    hooks = sub.add_parser("hooks", help="PreToolUse/PostToolUse/... hooks")
    hsub = hooks.add_subparsers(dest="hooks_cmd", required=True)
    p = hsub.add_parser("list"); p.add_argument("--event", default=None)
    p = hsub.add_parser("add")
    p.add_argument("--event", required=True)
    p.add_argument("--command", required=True)
    p.add_argument("--matcher", default=None)
    p.add_argument("--instance", type=int, default=None)
    for name in ("enable", "disable", "remove"):
        p = hsub.add_parser(name); p.add_argument("hook_id", type=int)

    plugins = sub.add_parser("plugins", help="installed plugins")
    plsub = plugins.add_subparsers(dest="plugins_cmd", required=True)
    plsub.add_parser("list")
    p = plsub.add_parser("install"); p.add_argument("path")
    p = plsub.add_parser("create")
    p.add_argument("--name", required=True)
    p.add_argument("--code", default=None, help="the plugin's Python source, inline")
    p.add_argument("--code-file", default=None, dest="code_file", help="read the source from this file instead")
    for name in ("enable", "disable", "remove"):
        p = plsub.add_parser(name); p.add_argument("name")

    skills = sub.add_parser("skills", help="per-bot skills, packs, quarantine and drafts")
    sksub = skills.add_subparsers(dest="skills_cmd", required=True)
    p = sksub.add_parser("list"); p.add_argument("--instance", type=int, default=None)
    p = sksub.add_parser("create")
    p.add_argument("--instance", type=int, default=None)
    p.add_argument("--name", required=True)
    p.add_argument("--description", default=None)
    p.add_argument("--content", default=None)
    p.add_argument("--content-file", default=None, dest="content_file")
    p.add_argument("--global", action="store_true", dest="global_")
    p = sksub.add_parser("remove"); p.add_argument("name"); p.add_argument("--instance", type=int, default=None)
    sksub.add_parser("packs")
    p = sksub.add_parser("fetch")
    p.add_argument("url")
    p.add_argument("--ref", default=None)
    p.add_argument("--subdir", default=None)
    sksub.add_parser("quarantine")
    for name in ("approve-quarantine", "reject-quarantine", "approve-draft", "reject-draft"):
        p = sksub.add_parser(name); p.add_argument("name")
    sksub.add_parser("drafts")

    mcp = sub.add_parser("mcp", help="internal (Claude Desktop) and external MCP servers")
    mcsub = mcp.add_subparsers(dest="mcp_cmd", required=True)
    mcsub.add_parser("list")
    p = mcsub.add_parser("logs"); p.add_argument("name"); p.add_argument("--lines", type=int, default=50)
    for name in ("enable", "disable"):
        p = mcsub.add_parser(name); p.add_argument("name")
    mcsub.add_parser("pins")
    p = mcsub.add_parser("approve-pin"); p.add_argument("server"); p.add_argument("tool")
    p = mcsub.add_parser("external-list"); p.add_argument("--instance", type=int, default=None)
    p = mcsub.add_parser("external-add")
    p.add_argument("--name", required=True)
    p.add_argument("--transport", required=True, choices=["stdio", "remote"])
    p.add_argument("--command", default=None)
    p.add_argument("--args", default=None, help='a JSON list, e.g. \'["-m", "some_server"]\' — not space-separated, so an arg starting with "-" is never mistaken for another flag')
    p.add_argument("--url", default=None)
    p.add_argument("--auth-token", default=None, dest="auth_token")
    p.add_argument("--oauth", action="store_true")
    p.add_argument("--instance", type=int, default=None)
    for name in ("external-enable", "external-disable", "external-remove"):
        p = mcsub.add_parser(name); p.add_argument("name")

    sec = sub.add_parser("security", help="allowed users, permission rules, devices/mobile keys")
    secsub = sec.add_subparsers(dest="security_cmd", required=True)
    secsub.add_parser("allowed-users")
    p = secsub.add_parser("allow-user"); p.add_argument("telegram_id"); p.add_argument("--name", default=None)
    p = secsub.add_parser("disallow-user"); p.add_argument("telegram_id")
    secsub.add_parser("permissions")
    p = secsub.add_parser("instance-permissions"); p.add_argument("instance_id", type=int)
    p = secsub.add_parser("set-instance-permissions")
    p.add_argument("instance_id", type=int)
    p.add_argument("--mode", default=None, help="default | plan | accept_edits | bypass | '' (follow the global default)")
    secsub.add_parser("devices")
    secsub.add_parser("mobile-keys")
    p = secsub.add_parser("create-mobile-key")
    p.add_argument("--label", required=True)
    p.add_argument("--tier", required=True)
    p.add_argument("--host", default=None)
    p = secsub.add_parser("revoke-mobile-key"); p.add_argument("key_id", type=int)

    snaps = sub.add_parser("snapshots", help="config/db snapshots")
    snsub = snaps.add_subparsers(dest="snapshots_cmd", required=True)
    snsub.add_parser("list")
    p = snsub.add_parser("create"); p.add_argument("--label", default=None)
    for name in ("restore", "remove"):
        p = snsub.add_parser(name); p.add_argument("name")

    sub.add_parser("env", help="env-file status (redacted) - see .env directly for raw content")

    cfg = sub.add_parser("config", help="config/backends.yaml - version, history, and the generic path setter")
    cfsub = cfg.add_subparsers(dest="config_cmd", required=True)
    cfsub.add_parser("get")
    cfsub.add_parser("reload")
    p = cfsub.add_parser("set")
    p.add_argument("path", help="dot-separated, e.g. backends.api.model")
    p.add_argument("value")

    diag = sub.add_parser("diagnostics", help="crash reports and a one-line health summary")
    diagsub = diag.add_subparsers(dest="diagnostics_cmd", required=True)
    diagsub.add_parser("summary")
    p = diagsub.add_parser("crash-reports"); p.add_argument("--limit", type=int, default=50)

    peers = sub.add_parser("peers", help="linked/federated AgenticBotPlatform servers")
    pesub = peers.add_subparsers(dest="peers_cmd", required=True)
    pesub.add_parser("list")
    pesub.add_parser("self-address")
    p = pesub.add_parser("pairing-token"); p.add_argument("--base-url", default=None, dest="base_url")
    p = pesub.add_parser("link")
    p.add_argument("--name", required=True)
    p.add_argument("--pairing-token", required=True, dest="pairing_token")
    p.add_argument("--my-base-url", default=None, dest="my_base_url")
    p = pesub.add_parser("remove"); p.add_argument("peer_id", type=int)
    for name in ("overview", "bots"):
        p = pesub.add_parser(name); p.add_argument("peer_id", type=int)

    kanban = sub.add_parser("kanban", help="per-bot kanban boards")
    kbsub = kanban.add_subparsers(dest="kanban_cmd", required=True)
    p = kbsub.add_parser("boards"); p.add_argument("--instance", type=int, required=True)
    p = kbsub.add_parser("cards")
    p.add_argument("--instance", type=int, required=True)
    p.add_argument("--board", default="default")
    p = kbsub.add_parser("add")
    p.add_argument("--instance", type=int, required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--board", default="default")
    p.add_argument("--column", default=None)
    p = kbsub.add_parser("move")
    p.add_argument("card_id", type=int)
    p.add_argument("--instance", type=int, required=True)
    p.add_argument("--column", required=True)
    p = kbsub.add_parser("remove")
    p.add_argument("card_id", type=int)
    p.add_argument("--instance", type=int, required=True)

    return ap


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
