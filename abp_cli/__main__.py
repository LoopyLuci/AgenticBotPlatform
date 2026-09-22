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
    print(f"unknown command {cmd!r}", file=sys.stderr)
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

    return ap


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
