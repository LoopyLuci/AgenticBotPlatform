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
  agent run <goal...>                    a real native-agent run: through a bot instance's API turn
                                           (streaming its progress), or headlessly on this machine via
                                           abp_run with --backend/--model/--workspace
  agent runs|show|cancel                  the run ledger, one run's detail, cancel a fan-out
  tools list                             what the native agent can call, with its permission class
  approvals list|show|approve|deny        tool approvals, so a headless run is never stuck on a GUI click
  memory search|add|list|delete|context|threads|thread|post-turn|tree|tree-ingest|tree-stats
        |sources|source-add|source-sync|source-rm|diff|checkpoint|vault|vault-sync|rules|rule-add
        |rule-remove|goals|goal-add|goal-done|settings|settings-set|approve|reject
                                           the memory fabric: memories, threads, the knowledge tree,
                                           sources, the vault, tool rules and goals
  swarms list|show|create|delete|enable|disable|run|runs|run-show|run-cancel
      |dispatch|goal|status              fan-out through one instance (native or Hermes), the swarm
                                           budget, and what has been dispatched recently
  route explain|rules|set|simulate|overview|models|decisions|decision|feedback|examples|example-add
        |example-rm|events|rest|release|forget|reset
                                           the model router: which model a message would use, and why
  models list|free|usage|info|refresh     the model catalog, its free models, and this week's usage
  privacy get|set                        privacy mode
  dns resolve|status                     resolve a name here; Tailscale DNS status
  doctor                                 one screen: can it reach ABP, does the token work, what answers
  providers list|add|remove|catalog|models|toggle|restore
  modules list|show|ops|run-op|start|stop|status|logs|call|setup|adopt|add|candidates|new|publish|forget
                                          every module; `modules adopt <folder>` makes any project one
  vision status|analyze|find|compare|edit|fetch
                                          computer vision here (OpenCV + its model zoo): objects, faces, text,
                                          codes; find text or an image on the screen; compare two images
  host status|network|account|site|plan|live|publish|check|server|dns|tunnel|router|cert|vps
                                          ABP Web Hosting: sites on your domains from here, a VPS, your own
                                          server or a provider (see abp_cli/host.py)
  nas status|disks|array|share|user|search|transfer|backup|app|...
                                          ABP File Server: parity array, shares, users, search, transfers,
                                          backups, apps (see abp_cli/nas.py)
  ai status|serve-status|models|list|ps|pull|rm|run|cp|show|import|create|discover|train|server|engine
                                          ABP's local AI (Ollama and Unsloth in ABP): models, the server,
                                          a real inference run, fine-tuning on the GPU (see abp_cli/ai.py)
  hermes status|list|start|stop|restart|logs|instances|ask
                                          Hermes Agent's own messaging gateway, driven from here while it
                                          keeps serving Telegram (see abp_cli/hermes.py)
  lab status|runs|designs|validate|train|import|projects|systune|advice|bench|retrain|telemetry|hw
                                          the Neural Lab: designs trained on the GPU, BrainBuilder / KotMoE /
                                          Amethyst / Kestrion, the system models (see abp_cli/ai.py)

Connection: --host (default 127.0.0.1:8787) and --token (default from .env's
DASHBOARD_TOKEN, same as bot/tui/'s ConnectScreen). --json prints machine-readable output
instead of plain text/tables, on every command.

Exit status: 0 done | 1 the request failed | 2 bad usage.
Every command takes --json for machine-readable output, and errors go to stderr.
See docs/agents/cli-tui.md for what this covers today and what's still dashboard-only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import shutil
import sys
import tempfile
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
    if cmd == "modules":
        return await _modules(args, client)
    if cmd == "vision":
        return await _vision(args, client)
    if cmd == "hermes":
        from abp_cli import hermes as _hermes
        return await _hermes.run(args, client)
    if cmd == "host":
        from abp_cli import host as _host
        return await _host.run(args, client)
    if cmd == "nas":
        from abp_cli import nas as _nas
        return await _nas.run(args, client)
    if cmd in ("ai", "lab"):
        from abp_cli import ai as _ai
        return await _ai.run(args, client)
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
    if cmd in ("tailscale", "docker", "vm", "rules", "browser"):
        try:
            body = json.loads(args.data) if args.data else None
        except ValueError as exc:
            print(f"--data is not valid JSON: {exc}", file=sys.stderr)
            return 2
        _print(args, await client.infra(cmd, args.verb, args.path, body))
        return 0
    if cmd == "env":
        if getattr(args, "env_cmd", "status") == "set":
            _print(args, await client.env_set(args.key, args.value))
        else:
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
    if cmd == "editors":
        return await _editors(args, client)
    if cmd == "ssh":
        return await _ssh(args, client)
    if cmd == "memory":
        return await _memory(args, client)
    if cmd == "agent":
        return await _agent(args, client)
    if cmd == "tools":
        return await _tools(args, client)
    if cmd == "approvals":
        return await _approvals(args, client)
    if cmd == "route":
        return await _route(args, client)
    if cmd == "models":
        return await _models(args, client)
    if cmd == "privacy":
        return await _privacy(args, client)
    if cmd == "dns":
        return await _dns(args, client)
    if cmd == "doctor":
        return await _doctor(args, client)
    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


async def _ssh(args, client: DashboardClient) -> int:
    sub = args.ssh_cmd
    if sub == "status":
        _print(args, await client.ssh_toolkit_status())
        return 0
    if sub == "list":
        _print(args, await client.ssh_toolkit_connections(), table=["Name", "HostName", "Port", "User", "Tags"])
        return 0
    if sub == "show":
        _print(args, await client.ssh_toolkit_get_connection(args.name))
        return 0
    if sub == "add":
        _print(args, await client.ssh_toolkit_add_connection(
            args.name, args.host_name, port=args.port, user=args.user, identity_file=args.identity_file,
            generate_key=args.generate_key, proxy_jump=args.proxy_jump, tags=args.tags,
            multiplex=args.multiplex, force=args.force,
        ))
        return 0
    if sub == "remove":
        _print(args, await client.ssh_toolkit_remove_connection(args.name))
        return 0
    if sub == "test":
        _print(args, await client.ssh_toolkit_test_connection(args.name))
        return 0
    if sub == "run":
        _print(args, await client.ssh_toolkit_run(args.name, " ".join(args.command)))
        return 0
    if sub == "status-all":
        _print(args, await client.ssh_toolkit_status_all(), table=["Name", "Target", "Reachable"])
        return 0
    if sub == "visualize":
        _print(args, await client.ssh_toolkit_graph())
        return 0
    if sub == "check-update":
        _print(args, await client.ssh_toolkit_check_update())
        return 0
    if sub == "update":
        _print(args, await client.ssh_toolkit_apply_update())
        return 0
    if sub == "auto-update":
        if args.mode:
            _print(args, await client.ssh_toolkit_set_auto_update(args.mode))
        else:
            _print(args, await client.ssh_toolkit_get_auto_update())
        return 0
    print(f"unknown ssh subcommand {sub!r}", file=sys.stderr)
    return 2


async def _memory(args, client: DashboardClient) -> int:
    sub = args.memory_cmd
    if sub == "search":
        _print(args, await client.memory_search(args.q, instance_id=args.instance, limit=args.limit),
               table=["id", "content", "kind", "shared"])
        return 0
    if sub == "add":
        _print(args, await client.memory_entry_add(args.content, kind=args.kind, shared=args.shared,
                                                   instance_id=args.instance, source=args.source))
        return 0
    if sub == "list":
        _print(args, await client.memory_entries_list(scope=args.scope, status=args.status),
               table=["id", "content", "kind", "status"])
        return 0
    if sub == "delete":
        _print(args, await client.memory_entry_delete(args.entry_id, scope=args.scope))
        return 0
    if sub == "approve":
        _print(args, await client.memory_entry_review(args.entry_id, "approve"))
        return 0
    if sub == "reject":
        _print(args, await client.memory_entry_review(args.entry_id, "reject"))
        return 0
    if sub == "context":
        _print(args, await client.memory_context(args.q, instance_id=args.instance, thread=args.thread,
                                                 backend=args.backend))
        return 0
    if sub == "threads":
        _print(args, await client.memory_threads(instance_id=args.instance, limit=args.limit),
               table=["thread", "instance_id", "turns"])
        return 0
    if sub == "thread":
        _print(args, await client.memory_thread_get(args.thread, after=args.after, limit=args.limit),
               table=["id", "role", "text", "created"])
        return 0
    if sub == "post-turn":
        _print(args, await client.memory_thread_add(args.thread, args.role, args.text, backend=args.backend,
                                                    model=args.model, instance_id=args.instance))
        return 0
    if sub == "tree":
        data = await _memory_tree(args, client)
        _print(args, data)
        return 0
    if sub == "tree-ingest":
        _print(args, await client.memory_tree_ingest(args.title, args.text, source_id=args.source_id))
        return 0
    if sub == "tree-stats":
        _print(args, await client.memory_tree_stats())
        return 0
    if sub == "sources":
        _print(args, await client.memory_sources(), table=["id", "kind", "label", "enabled", "last_sync"])
        return 0
    if sub == "source-add":
        _print(args, await client.memory_source_add(args.kind, args.label, path=args.path, repo=args.repo,
                                                    url=args.url, glob=args.glob))
        return 0
    if sub == "source-sync":
        _print(args, await client.memory_source_sync(args.source_id))
        return 0
    if sub == "source-rm":
        _print(args, await client.memory_source_remove(args.source_id))
        return 0
    if sub == "diff":
        _print(args, await client.memory_diff(source_id=args.source_id, checkpoint=args.checkpoint,
                                              since_read=not args.all, commit=not args.no_commit, text=args.text))
        return 0
    if sub == "checkpoint":
        _print(args, await client.memory_checkpoint(args.name))
        return 0
    if sub == "vault":
        _print(args, await client.memory_vault())
        return 0
    if sub == "vault-sync":
        _print(args, await client.memory_vault_sync())
        return 0
    if sub == "rules":
        _print(args, await client.memory_tool_rules(tool=args.tool), table=["id", "tool", "rule", "priority"])
        return 0
    if sub == "rule-add":
        _print(args, await client.memory_tool_rule_put(args.tool, args.rule, priority=args.priority,
                                                       tags=args.tags, rule_id=args.rule_id))
        return 0
    if sub == "rule-remove":
        _print(args, await client.memory_tool_rule_delete(args.rule_id))
        return 0
    if sub == "goals":
        _print(args, await client.memory_goals(all=args.all), table=["id", "text", "status"])
        return 0
    if sub == "goal-add":
        _print(args, await client.memory_goal_put(args.text, status=args.status, goal_id=args.goal_id))
        return 0
    if sub == "goal-done":
        _print(args, await client.memory_goal_delete(args.goal_id))
        return 0
    if sub == "settings":
        if args.json:
            _print(args, await client.memory_settings_get())
        else:
            s = await client.memory_settings_get()
            for k, v in s.items():
                print(f"{k}: {v}")
        return 0
    if sub == "settings-set":
        _print(args, await client.memory_settings_set(_parse_kv_pairs(args.changes)))
        return 0
    print(f"unknown memory subcommand {sub!r}", file=sys.stderr)
    return 2


async def _agent_target(args, client: DashboardClient) -> tuple[Optional[int], Optional[dict], list[dict]]:
    """(instance id, its row, every agent-backed bot) for `agent run` and the swarm fan-outs. The
    candidates come from GET /api/agent/overview's own `bots` list - the same filtered set the Agents
    page shows - so this never needs its own idea of which backends are agent backends."""
    candidates = (await client.agent_overview()).get("bots") or []
    if args.instance is not None:
        row = next((b for b in candidates if int(b["id"]) == args.instance), None)
        if row is None:
            print(f"no agent-backed bot instance {args.instance} here"
                  + (f" (agent-backed: {', '.join(str(b['id']) for b in candidates)})" if candidates else
                     " - create one: abp_cli bots create ... --backend native_agent"), file=sys.stderr)
            return None, None, candidates
        return int(row["id"]), row, candidates
    row = candidates[0] if candidates else None
    return (int(row["id"]) if row else None), row, candidates


async def _memory_tree(args, client: DashboardClient) -> Any:
    """The knowledge tree's own query modes (bot/memoryfabric/knowledge.py's query(), the same entry
    point the agents' memory_tree tool and the MCP server use), one CLI subcommand each."""
    t = args.tree_cmd
    if t == "walk":
        return await client.memory_tree("walk", query=args.query, limit=args.limit, max_hops=args.max_hops,
                                        time_window_days=_number(args.window))
    if t == "search-entities":
        return await client.memory_tree("search_entities", name=args.name, limit=args.limit)
    if t == "neighbors":
        return await client.memory_tree("neighbors", entity=args.entity, limit=args.limit)
    if t == "source":
        return await client.memory_tree("query_source", source_id=args.source_id, query=args.query,
                                        since=_number(args.since), until=_number(args.until), limit=args.limit)
    if t == "drill-down":
        return await client.memory_tree("drill_down", node_id=args.node_id, depth=args.depth, query=args.query,
                                        limit=args.limit)
    if t == "cover-window":
        return await client.memory_tree("cover_window", since=_number(args.since), until=_number(args.until),
                                        source_id=args.source_id, limit=args.limit)
    return await client.memory_tree("fetch_leaves", ids=args.ids)


def _number(raw: Optional[str]) -> Optional[float]:
    """An optional timestamp: absent stays absent, anything else must be a number."""
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except ValueError:
        print(f"{raw!r} is not a number (a unix timestamp, or seconds)", file=sys.stderr)
        raise SystemExit(2) from None


async def _agent(args, client: DashboardClient) -> int:
    sub = args.agent_cmd
    if sub == "run":
        return await _agent_run(args, client)
    if sub == "runs":
        rows = await client.list_jobs(status=args.status, limit=args.limit)
        if args.instance is not None:
            rows = [r for r in rows if int(r.get("instance_id") or 0) == args.instance]
        _print(args, rows, table=["id", "instance_id", "action_type", "status", "backend", "created_at"])
        return 0
    if sub == "show":
        return await _agent_show(args, client)
    if sub == "cancel":
        _print(args, await client.cancel_swarm_run(args.swarm_run_id))
        return 0
    if sub == "budget":
        if args.max_children is None and args.max_usd is None and args.enabled is None:
            _print(args, await client.swarm_budget())
        else:
            _print(args, await client.swarm_budget_set(enabled=args.enabled, max_children=args.max_children,
                                                       max_estimated_usd=args.max_usd))
        return 0
    print(f"unknown agent subcommand {sub!r}", file=sys.stderr)
    return 2


async def _agent_show(args, client: DashboardClient) -> int:
    """One run in full: its jobs row, every tool event, and its per-child breakdown (delegation.py's routes)."""
    job = await client.job(args.job_id)
    if job is None:
        print(f"no job #{args.job_id}", file=sys.stderr)
        return 1
    detail: dict[str, Any] = {"job": job}
    try:
        detail["tool_events"] = await client.job_tool_events(args.job_id)
        detail["children"] = await client.job_children(args.job_id)
    except ApiError as exc:                      # a job with no events yet is normal, not a failure
        detail["tool_events"], detail["children"] = [], []
        detail["events_error"] = str(exc)
    _print(args, detail)
    return 0


async def _agent_run(args, client: DashboardClient) -> int:
    """One real native-agent run. Through a bot instance's API turn (its own tools, permissions and
    approvals, recorded as a job the dashboard and `agent runs` both see), or - with --backend, and no
    instance to go through - headlessly on this machine through abp_run."""
    goal = " ".join(args.goal).strip()
    if not goal:
        print("the goal is empty", file=sys.stderr)
        return 2
    instance, row, candidates = await _agent_target(args, client)
    if instance is None and not candidates:
        if args.backend is None:
            print("no agent-backed bot instance here, and --backend was not given. Create one "
                  "(`abp_cli bots create ... --backend native_agent`), name one with --instance, or run the "
                  "agent headlessly on this machine instead: --backend anthropic --model claude-sonnet-5.",
                  file=sys.stderr)
            return 2
        return await _agent_run_local(args, goal)
    if instance is None:
        return 2
    return await _agent_run_via_api(args, client, goal, instance, row)


async def _agent_run_local(args, goal: str) -> int:
    """No bot instance to go through: the same headless agent `python -m abp_run` runs (abp_run/core.py),
    streaming the answer as it is written."""
    from abp_run import core

    workspace = pathlib.Path(args.workspace or ".").expanduser()
    if not workspace.is_dir():
        print(f"--workspace {workspace} is not a folder", file=sys.stderr)
        return 2
    if args.model:
        ref = args.model if "/" in args.model else f"{args.backend or 'anthropic'}/{args.model}"
    elif args.backend and "/" in args.backend:
        ref = args.backend
    else:
        print("a local run needs a model: --model claude-sonnet-5, --model provider/model, or "
              "--backend provider/model", file=sys.stderr)
        return 2
    try:
        provider, model = core.split_model(ref)
    except core.RunError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    streamed: list[str] = []

    async def on_text(text: str) -> None:
        streamed.append(text)
        sys.stdout.write(text)
        sys.stdout.flush()

    # abp_run's own pieces, driven on this command's event loop instead of run_once()'s private one
    # (run_once calls asyncio.run(), which cannot run inside the loop main() already has) - the same
    # transport, the same throwaway database and trace store, the same agent loop.
    try:
        transport = core.transport_for(provider, model)
    except core.RunError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    root = pathlib.Path(tempfile.mkdtemp(prefix="abp-run-"))
    try:
        from bot.agent_runtime import code_intel

        with core.ephemeral_environment(root, "deny" if args.approve == "ask" else args.approve):
            try:
                result = await core.run_turn(goal, transport=transport, model=model, cwd=workspace.resolve(),
                                             permission_mode=args.permission_mode,
                                             on_text=on_text if not args.json else None,
                                             timeout_s=args.timeout)
            finally:
                await code_intel.shutdown_all()   # language servers must not outlive this run
    except core.RunError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if args.json:
        print(json.dumps({"via": "abp_run", "provider": provider, "model": model,
                          "workspace": str(workspace.resolve()), **result.to_dict()},
                         ensure_ascii=False))
    elif not streamed:
        print(result.reply if result.ok else f"error: {result.error}", file=sys.stdout if result.ok else sys.stderr)
    elif not result.ok:
        print(f"\nerror: {result.error}", file=sys.stderr)
    return result.exit_code


async def _agent_run_via_api(args, client: DashboardClient, goal: str, instance: int,
                             instance_row: Optional[dict]) -> int:
    """POST /api/chat/send-to-bot is one synchronous turn, so the progress streamed here is the run's own
    record as it lands: each new job, its tool events, and any tool approval it is waiting on (resolved
    straight away with --approve allow|deny, so nothing waits for a click in the GUI)."""
    seen_jobs: set[int] = {int(r["id"]) for r in await client.list_jobs(limit=200)}
    watching: set[int] = set()
    seen_events: dict[int, int] = {}
    seen_approvals: set[int] = set()
    for row in await client.list_approvals(status="pending", instance_id=instance, limit=200):
        seen_approvals.add(int(row["id"]))
    events: list[dict[str, Any]] = []
    turn = asyncio.ensure_future(client.send_to_bot(instance, goal))

    async def watch() -> None:
        while not turn.done():
            done, _ = await asyncio.wait({turn}, timeout=args.poll)
            if done:
                return
            await _agent_run_poll(client, instance, seen_jobs, watching, seen_events, seen_approvals, args, events)

    await watch()
    try:
        result = await turn
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    reply = result.get("reply") or ""
    if args.json:
        print(json.dumps({"via": "api", "instance_id": instance, "instance": (instance_row or {}).get("name"),
                          "backend": (instance_row or {}).get("backend"),
                          "model": (instance_row or {}).get("model"),
                          "goal": goal, "reply": reply, "events": events}, ensure_ascii=False))
    else:
        if reply:
            print(reply)
        for ev in events:
            print(_event_line(ev))
    jobs = await _agent_run_wait(client, args, instance)
    if args.wait:
        for job in jobs:
            print(f"run {job['id']}: {job.get('status') or '-'} "
                  f"({job.get('duration_ms') or 0} ms, {job.get('tokens') or 0} token(s))")
    return 0


async def _agent_run_poll(client: DashboardClient, instance: int, seen_jobs: set[int], watching: set[int],
                          seen_events: dict[int, int], seen_approvals: set[int], args,
                          events: list[dict[str, Any]]) -> None:
    """One look at what the run has recorded since the last poll: the jobs it started, the tool events
    of those, and any approval it is now blocked on. Only jobs this run started are watched, so an
    earlier run's events are never replayed as if they were this one's."""
    try:
        for row in await client.list_jobs(limit=200):
            jid = int(row["id"])
            if int(row.get("instance_id") or 0) != instance:
                continue
            if jid not in seen_jobs:
                seen_jobs.add(jid)
                watching.add(jid)
                events.append({"kind": "job", "id": jid, "status": row.get("status")})
            if jid not in watching:
                continue
            fresh = await client.job_tool_events(jid)
            for ev in fresh[seen_events.get(jid, 0):]:
                events.append({"kind": "tool", "job_id": jid, "event": ev.get("event_type"),
                               "tool": ev.get("tool_name")})
            seen_events[jid] = len(fresh)
        for row in await client.list_approvals(status="pending", instance_id=instance, limit=200):
            aid = int(row["id"])
            if aid in seen_approvals:
                continue
            seen_approvals.add(aid)
            events.append({"kind": "approval", "id": aid, "tool": row.get("tool"), "summary": row.get("summary")})
            if args.approve != "ask":
                outcome = "once" if args.approve == "allow" else "deny"
                await client.resolve_approval(aid, outcome)
                events[-1]["outcome"] = outcome
    except Exception:  # noqa: BLE001 - a poll that fails is not a reason to fail the run
        return


async def _agent_run_wait(client: DashboardClient, args, instance: int) -> list[dict]:
    """Follow the run's newest job to a terminal state. The turn itself already returned, so this is
    bounded by --timeout and by the job's own status flipping out of running/queued/retrying."""

    async def newest() -> Optional[dict]:
        for row in await client.list_jobs(limit=200):
            if int(row.get("instance_id") or 0) == instance:
                return row
        return None

    if not args.wait:
        return []
    deadline = asyncio.get_running_loop().time() + args.timeout
    job = await newest()
    while job is not None and (job.get("status") or "") in ("running", "queued", "retrying"):
        if asyncio.get_running_loop().time() > deadline:
            print(f"run {job['id']} is still {job.get('status')} after {args.timeout:.0f}s; stopping the wait",
                  file=sys.stderr)
            break
        await asyncio.sleep(args.poll)
        job = await newest() or job
    return [job] if job is not None else []


def _event_line(ev: dict[str, Any]) -> str:
    kind = ev.get("kind")
    if kind == "job":
        return f"  job {ev['id']}: {ev.get('status') or 'new'}"
    if kind == "approval":
        return f"  approval {ev['id']} for {ev.get('tool')}: {ev.get('outcome') or 'waiting'}"
    return f"  {ev.get('event') or 'tool'} {ev.get('tool') or ''}".rstrip()


async def _tools(args, client: DashboardClient) -> int:
    if args.tools_cmd != "list":
        print(f"unknown tools subcommand {args.tools_cmd!r}", file=sys.stderr)
        return 2
    tools = await client.agent_tools()
    if args.read_only:
        tools = [t for t in tools if t.get("read_only")]
    if args.asks_first:
        tools = [t for t in tools if t.get("asks_first")]
    if args.json:
        _print(args, {"tools": tools, "count": len(tools)})
        return 0
    _print(args, tools, table=["name", "permission", "read_only", "asks_first", "origin"])
    return 0


async def _approvals(args, client: DashboardClient) -> int:
    sub = args.approvals_cmd
    if sub == "list":
        rows = await client.list_approvals(status=args.status, instance_id=args.instance, limit=args.limit)
        _print(args, rows, table=["id", "instance_id", "tool", "status", "summary", "created_at"])
        return 0
    if sub == "show":
        _print(args, await client.get_approval(args.approval_id))
        return 0
    if sub in ("approve", "deny"):
        outcome = "deny" if sub == "deny" else args.outcome
        _print(args, await client.resolve_approval(args.approval_id, outcome))
        return 0
    print(f"unknown approvals subcommand {sub!r}", file=sys.stderr)
    return 2


async def _route(args, client: DashboardClient) -> int:
    sub = args.route_cmd
    if sub == "explain":
        _print(args, await client.agent_router_recommend(args.text))
        return 0
    if sub == "rules":
        _print(args, await client.router_policy())
        return 0
    if sub == "set":
        try:
            policy = json.loads(args.policy)
        except ValueError as exc:
            print(f"the policy must be a JSON object: {exc}", file=sys.stderr)
            return 2
        _print(args, await client.router_policy_save(policy, note=args.note))
        return 0
    if sub == "simulate":
        _print(args, await client.router_simulate(args.text, candidates=args.candidates, images=args.images,
                                                  context_tokens=args.context_tokens))
        return 0
    if sub == "overview":
        _print(args, await client.router_overview(hours=args.hours))
        return 0
    if sub == "models":
        _print(args, await client.router_models())
        return 0
    if sub == "decisions":
        _print(args, await client.router_decisions(limit=args.limit, mode=args.mode, status=args.status,
                                                   model=args.model, task_class=args.task_class),
               table=["id", "chosen", "status", "task_class", "created_at"])
        return 0
    if sub == "decision":
        _print(args, await client.router_decision(args.decision_id))
        return 0
    if sub == "feedback":
        _print(args, await client.router_feedback(args.decision_id, rating=args.rating, note=args.note,
                                                  correct_class=args.correct_class,
                                                  preferred_model=args.preferred_model))
        return 0
    if sub == "examples":
        _print(args, await client.router_examples(limit=args.limit), table=["id", "text", "task_class"])
        return 0
    if sub == "example-add":
        _print(args, await client.router_example_add(args.text, task_class=args.task_class,
                                                     preferred_model=args.preferred_model))
        return 0
    if sub == "example-rm":
        _print(args, await client.router_example_delete(args.example_id))
        return 0
    if sub == "events":
        _print(args, await client.router_events(limit=args.limit, kind=args.kind, model=args.model),
               table=["id", "kind", "model", "created_at"])
        return 0
    if sub == "rest":
        _print(args, await client.router_model_rest(args.model, seconds=args.seconds, reason=args.reason))
        return 0
    if sub == "release":
        _print(args, await client.router_model_release(args.model))
        return 0
    if sub == "forget":
        _print(args, await client.router_model_forget(args.model))
        return 0
    if sub == "reset":
        _print(args, await client.router_reset(keep_policy=not args.no_keep_policy,
                                               keep_examples=not args.no_keep_examples))
        return 0
    print(f"unknown route subcommand {sub!r}", file=sys.stderr)
    return 2


async def _models(args, client: DashboardClient) -> int:
    sub = args.models_cmd
    if sub == "list":
        _print(args, await client.models_find(provider=args.provider, query=args.query, free_only=args.free_only,
                                              min_context=args.min_context, needs=args.needs, limit=args.limit),
               table=["provider", "model", "context", "free"])
        return 0
    if sub == "free":
        _print(args, await client.models_find(free_only=True, provider=args.provider, limit=args.limit),
               table=["provider", "model", "context"])
        return 0
    if sub == "usage":
        _print(args, await client.models_usage(days=args.days))
        return 0
    if sub == "info":
        _print(args, await client.models_info(args.provider, args.model))
        return 0
    if sub == "refresh":
        _print(args, await client.models_refresh())
        return 0
    print(f"unknown models subcommand {sub!r}", file=sys.stderr)
    return 2


async def _privacy(args, client: DashboardClient) -> int:
    sub = args.privacy_cmd
    if sub == "get":
        _print(args, await client.privacy_get())
        return 0
    if sub == "set":
        _print(args, await client.privacy_set(enabled=args.enabled, allow_lan=args.allow_lan))
        return 0
    print(f"unknown privacy subcommand {sub!r}", file=sys.stderr)
    return 2


async def _dns(args, client: DashboardClient) -> int:
    sub = args.dns_cmd
    if sub == "resolve":
        _print(args, await client.hosting_resolve(args.name, type=args.type))
        return 0
    if sub == "status":
        _print(args, await client.tailscale_dns_status())
        return 0
    print(f"unknown dns subcommand {sub!r}", file=sys.stderr)
    return 2


async def _doctor(args, client: DashboardClient) -> int:
    """The one screen a person or an agent reads first: can this CLI reach ABP at all (which is also
    the token check - a wrong one is a 401 on the very first call), and which of the feature groups
    answer. /api/agent/overview supplies the ABP Agents page's own readiness checks, so the CLI never
    invents its own opinion about setup."""
    checks: list[dict[str, Any]] = []

    async def probe(name: str, call, detail=lambda v: f"{len(v)}" if isinstance(v, (list, dict)) else str(v)):
        try:
            value = await call()
        except ApiError as exc:
            checks.append({"name": name, "ok": False, "detail": str(exc)})
            return None
        except Exception as exc:  # noqa: BLE001 - an unreachable host, a missing local service
            checks.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
            return None
        checks.append({"name": name, "ok": True, "detail": detail(value)})
        return value

    bots = await probe("dashboard", client.list_bots,
                       lambda v: f"token accepted: {len(v)} bot(s) at {client.base_url}")
    if bots is None:
        _print(args, {"ok": False, "host": client.base_url, "checks": checks})
        return 1
    await probe("providers", client.list_providers)
    overview = await probe("agent overview", client.agent_overview,
                           lambda v: f"{len(v.get('bots') or [])} agent bot(s), "
                                     f"{v.get('permission_mode')} permissions")
    await probe("tools", client.agent_tools)
    await probe("approvals", client.list_approvals, lambda v: f"{len(v)} pending")
    await probe("memory", client.memory_overview,
                lambda v: f"{v.get('threads')} thread(s), {len(v.get('counts') or {})} bucket(s)")
    await probe("swarms", client.list_swarms)
    await probe("modules", lambda: client._request("GET", "/api/modules"),
                lambda v: f"{len(v.get('modules') or [])} module(s)")
    answered = len(checks)
    setup: list[dict[str, Any]] = [
        {"name": f"setup: {check['title']}", "ok": bool(check.get("ok")), "detail": check.get("detail") or ""}
        for check in (overview or {}).get("checks") or []
    ]
    checks.extend(setup)
    # ok: every route answered. ready: ABP's own Agents-page setup checks all pass, which is what a
    # fresh install usually has not got yet and is not this command failing.
    ok = all(c["ok"] for c in checks[:answered])
    payload = {"ok": ok, "ready": all(c["ok"] for c in setup), "host": client.base_url, "checks": checks}
    if args.json:
        _print(args, payload)
        return 0 if ok else 1
    print(f"ABP at {client.base_url}: {'every feature answered' if ok else 'something did not answer'}"
          f"{'' if payload['ready'] else '; setup is not finished yet'}")
    for check in checks:
        print(f"  [{'ok' if check['ok'] else 'XX'}] {check['name']}: {check['detail']}")
    return 0 if ok else 1


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
    if sub == "dispatch":
        return await _swarms_dispatch(args, client)
    if sub == "goal":
        return await _swarms_goal(args, client)
    if sub == "status":
        return await _swarms_status(args, client)
    print(f"unknown swarms subcommand {sub!r}", file=sys.stderr)
    return 2


def _swarm_kind(row: Optional[dict]) -> str:
    """Which dispatch route an instance needs: a native_agent fan-out takes the decomposed task list
    directly, a Hermes one gets a goal prompt asking it to fan the goal out itself."""
    return "native" if (row or {}).get("backend") == "native_agent" else "hermes"


async def _swarms_dispatch(args, client: DashboardClient) -> int:
    """Fan a goal out over sub-agents. --task decomposes it up front (this caller plays the role the
    goal-prompt template plays for an external Hermes instance); without it the goal goes as one task.
    Which route that is depends on the instance: a native_agent fan-out runs the task list directly, a
    Hermes one gets a goal prompt asking its own agent to fan it out (bot/mcp_server.py's
    dispatch_native_swarm_goal and dispatch_swarm_goal, over the same two routes)."""
    instance, row, candidates = await _agent_target(args, client)
    if instance is None:
        return 2
    goal = " ".join(args.goal).strip()
    tasks = [{"goal": t} for t in (args.task or []) if t.strip()]
    if not goal and not tasks:
        print("give a goal, or one or more --task", file=sys.stderr)
        return 2
    payload: dict[str, Any] = {"worker_provider": args.provider, "worker_model": args.model,
                               "max_children": args.max_children, "confirm": args.confirm}
    if _swarm_kind(row) == "native":
        result = await client.native_agent_dispatch(instance, tasks or [{"goal": goal}], **payload)
    else:
        result = await client.hermes_dispatch(instance, goal or "\n".join(t["goal"] for t in tasks), **payload)
    if args.json:
        _print(args, result)
        return 0
    print(result.get("result") or "")
    print(f"job {result.get('job_id')}; {result.get('worker_provider')}/{result.get('worker_model')} "
          f"(dispatch {result.get('dispatch_id') or 'hermes'})")
    return 0


async def _swarms_goal(args, client: DashboardClient) -> int:
    """One goal, handed to an instance to fan out itself - the goal-prompt indirection, and the shape
    `dispatch` uses for a Hermes instance. --native skips the indirection and runs it as a single task."""
    instance, row, candidates = await _agent_target(args, client)
    if instance is None:
        return 2
    goal = " ".join(args.goal).strip()
    if not goal:
        print("the goal is empty", file=sys.stderr)
        return 2
    if not args.native and _swarm_kind(row) == "native":
        print(f"instance {instance} is native_agent, which takes a task list rather than a goal prompt: "
              "`swarms dispatch <goal>` runs it as one task, or pass --native to be explicit.", file=sys.stderr)
        return 2
    payload: dict[str, Any] = {"worker_provider": args.provider, "worker_model": args.model,
                               "max_children": args.max_children, "confirm": args.confirm}
    if args.native:
        result = await client.native_agent_dispatch(instance, [{"goal": goal}], **payload)
    else:
        result = await client.hermes_dispatch(instance, goal, **payload)
    _print(args, result)
    return 0


async def _swarms_status(args, client: DashboardClient) -> int:
    """Everything a headless operator needs to see what fan-out is doing: the spending guard, the
    registered swarms, their latest runs, and the delegation activity log."""
    budget = await client.swarm_budget()
    runs = await client.list_swarm_runs(limit=args.limit)
    payload = {
        "budget": budget,
        "swarms": await client.list_swarms(),
        "runs": runs,
        "delegation": await client.delegation_activity(limit=args.limit),
    }
    _print(args, payload)
    return 0


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
        _print(args, await client.link_peer(
            args.name, args.pairing_token, my_base_url=args.my_base_url, setup_ssh=not args.no_ssh_setup,
        ))
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


async def _editors(args, client: DashboardClient) -> int:
    sub = args.editors_cmd
    if sub == "status":
        data = await client.editors_status()
        if args.json:
            _print(args, data)
            return 0
        v = data["vscode"]
        if not v["cli"]:
            print("VS Code: not found (install it, or put its `code` command on PATH)")
        else:
            state = f"version {v['installed']} installed" if v["installed"] else "extension not installed"
            extra = f"; {v['bundled']} available (abp_cli editors install-vscode)" if v["update_available"] else ""
            print(f"VS Code: {state}{extra}")
        print("ACP editors (Zed and others) run: " + " ".join(data["acp_command"]))
        return 0
    if sub == "install-vscode":
        data = await client.install_vscode_extension()
        if args.json:
            _print(args, data)
        else:
            print(f"Installed the VS Code extension, version {data['vscode']['installed']}.")
        return 0
    print(f"unknown editors subcommand {sub!r}", file=sys.stderr)
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
        _print(args, bots, table=["id", "name", "platform", "backend", "enabled", "live_running", "served_by"])
        return 0
    if sub == "show":
        _print(args, await client.get_bot(args.instance_id))
        return 0
    if sub == "create":
        try:
            credentials = json.loads(args.credentials) if args.credentials else {}
        except json.JSONDecodeError as exc:
            print(f"--credentials must be valid JSON: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
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
        if args.takeover_when_gateway_down is not None:
            payload["takeover_when_gateway_down"] = args.takeover_when_gateway_down
        if not payload:
            print("nothing to change — pass at least one of --name/--backend/--model/--allowed/--admins",
                  file=sys.stderr)
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


async def _modules(args, client: DashboardClient) -> int:
    sub = args.modules_cmd
    api = "/api/modules"
    if sub == "list":
        rows = (await client._request("GET", api))["modules"]
        _print(args, [{**r, "hub": "running" if (r.get("hub") or {}).get("running") else ""} for r in rows],
               table=["id", "name", "area", "installed", "ready", "hub"])
        return 0
    if sub == "show":
        _print(args, await client._request("GET", f"{api}/{args.module}"))
        return 0
    if sub == "ops":
        _print(args, await client._request("GET", f"{api}/{args.module}/operations"),
               table=["id", "summary", "mutating"])
        return 0
    if sub in ("run-op", "call"):
        body = {"operation": args.operation, "args": json.loads(args.args or "{}")}
        if getattr(args, "timeout", None):
            body["timeout_s"] = args.timeout
        _print(args, (await client._request("POST", f"{api}/{args.module}/call", json=body))["result"])
        return 0
    if sub == "start":
        _print(args, await client._request("POST", f"{api}/{args.module}/hub/start"))
        return 0
    if sub == "stop":
        _print(args, await client._request("POST", f"{api}/{args.module}/hub/stop"))
        return 0
    if sub == "status":
        _print(args, await client._request("GET", f"{api}/{args.module}",
                                           params={"fetch": "1"} if args.fetch else None))
        return 0
    if sub == "logs":
        if args.job:
            job = await client._request("GET", f"{api}/jobs/{args.job}")
            if args.json:
                _print(args, job)
            else:
                print(f"{job['id']}  {job.get('module')}  {job.get('kind')}  {job.get('state')}")
                for line in (job.get("log") or [])[-args.lines:]:
                    print(f"  {line}")
            return 0
        jobs = await client._request("GET", f"{api}/{args.module}/jobs")
        if args.json:
            _print(args, jobs)
            return 0
        for job in jobs:
            print(f"{job.get('id')}  {job.get('kind')}  {job.get('state')}")
            for line in (job.get("log") or [])[-args.lines:]:
                print(f"  {line}")
        return 0
    if sub == "conformance":
        _print(args, await client._request("POST", f"{api}/{args.module}/conformance"))
        return 0
    if sub == "setup":
        route = {"install": "setup", "start": "hub/start", "stop": "hub/stop", "open_gui": "gui", "open_tui": "tui",
                 "register_mcp": "mcp"}.get(args.action, args.action)
        _print(args, await client._request("POST", f"{api}/{args.module}/{route}"))
        return 0
    if sub in ("adopt", "new"):
        import os
        path = str((pathlib.Path(os.environ.get("ABP_CALLER_CWD") or ".") / args.path).resolve())
        if sub == "new":
            from abp_modkit.cli import new as modkit_new
            modkit_new(pathlib.Path(path), args.lang, args.name or "")
        res = await client._request("POST", f"{api}/adopt", json={"path": path, "id": args.id or "",
                                                                   "name": args.name or "",
                                                                   "dry_run": bool(getattr(args, "dry_run", False))})
        if args.json or res.get("files"):
            _print(args, res)
        else:
            print(f"{res.get('id')}: {res.get('operations')} operations ({', '.join(res.get('stacks') or [])}); "
                  f"wrote {', '.join(res.get('wrote') or []) or 'nothing new'}"
                  + ("; registered" if res.get("registered") else ""))
            for n in res.get("notes") or []:
                print(f"  note: {n}")
        return 0
    if sub == "add":
        import asyncio
        job = await client._request("POST", f"{api}/add", json={"url": args.url, "id": args.id or "",
                                                                 "name": args.name or "", "shallow": not args.full,
                                                                 "branch": args.branch or ""})
        seen = 0
        while True:
            job = await client._request("GET", f"{api}/jobs/{job['id']}")
            if not args.json:
                for line in (job.get("log") or [])[seen:]:
                    print(line)
                seen = len(job.get("log") or [])
            if job.get("state") != "running":
                break
            await asyncio.sleep(1.0)
        if args.json:
            _print(args, job)
        elif job.get("state") == "done":
            r = job.get("result") or {}
            print(f"\n{r.get('id')}: a module now ({r.get('operations')} operations); its clone: {r.get('path')}")
        else:
            print(f"\nfailed: {job.get('error')}", file=sys.stderr)
        return 0 if job.get("state") == "done" else 1
    if sub == "candidates":
        _print(args, (await client._request("GET", f"{api}/candidates", params={"folder": args.folder}))["projects"],
               table=["name", "module", "markers"])
        return 0
    if sub == "publish":
        _print(args, await client._request("POST", f"{api}/{args.module}/publish", json={"push": args.push}))
        return 0
    if sub == "forget":
        _print(args, await client._request("POST", f"{api}/{args.module}/forget"))
        return 0
    print(f"unknown modules subcommand {sub!r}", file=sys.stderr)
    return 2


def _image_arg(v: str) -> str:
    """A local file is sent as its absolute path (relative to where the command was typed); anything else as is."""
    import os
    if not v or v.lower().startswith(("http://", "https://", "data:", "screen", "camera")):
        return v
    p = pathlib.Path(os.environ.get("ABP_CALLER_CWD") or ".") / v
    return str(p.resolve()) if p.exists() else v


async def _vision(args, client: DashboardClient) -> int:
    sub = args.vision_cmd
    api = "/api/vision"
    if sub == "status":
        _print(args, await client._request("GET", api))
        return 0
    if sub == "analyze":
        body = {"image": _image_arg(args.image), "min_score": args.min_score}
        if args.tasks:
            body["tasks"] = [t.strip() for t in args.tasks.split(",") if t.strip()]
        res = await client._request("POST", f"{api}/analyze", json=body)
        if args.json:
            _print(args, res)
            return 0
        for key in ("objects", "faces", "codes"):
            for it in res.get(key) or []:
                print(f"{key[:-1]:7} {it.get('label') or it.get('kind', '')} {it.get('text', '')} "
                      f"{it.get('score', '')} box={it['box']}".replace("  ", " "))
        if res.get("text_joined"):
            print("text:\n  " + res["text_joined"].replace("\n", "\n  "))
        if res.get("annotated"):
            print(f"picture: {res['annotated']}")
        return 0
    if sub == "find":
        body = {"image": _image_arg(args.image), "text": args.text or "",
                "template": _image_arg(args.template) if args.template else None}
        _print(args, await client._request("POST", f"{api}/find", json=body))
        return 0
    if sub == "compare":
        _print(args, await client._request("POST", f"{api}/compare", json={"before": _image_arg(args.before),
                                                                          "after": _image_arg(args.after)}))
        return 0
    if sub == "edit":
        _print(args, await client._request("POST", f"{api}/edit", json={"image": _image_arg(args.image),
                                                                       "steps": json.loads(args.steps)}))
        return 0
    if sub == "fetch":
        _print(args, await client._request("POST", f"{api}/models/{args.model}/fetch"))
        return 0
    print(f"unknown vision subcommand {sub!r}", file=sys.stderr)
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
    # Telegram only: the token is served by a running Hermes gateway, so ABP does
    # not poll it (see `abp hermes instances`). Taking the token over once that
    # gateway stops is opt-in, default off.
    p.add_argument("--takeover-when-gateway-down", dest="takeover_when_gateway_down",
                   action="store_true", default=None,
                   help="poll this Telegram token once the Hermes gateway that owns it stops")
    p.add_argument("--no-takeover-when-gateway-down", dest="takeover_when_gateway_down", action="store_false",
                   help="keep leaving this token to the Hermes gateway (the default)")
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

    mods = sub.add_parser("modules", help="modules: separate programs ABP builds and drives; adopt any project")
    msub = mods.add_subparsers(dest="modules_cmd", required=True)
    msub.add_parser("list")
    for name in ("show", "status", "ops", "forget", "conformance"):
        p = msub.add_parser(name); p.add_argument("module")
        if name == "status":
            p.add_argument("--fetch", action="store_true", help="also ask the module's own registry for updates")
    for name in ("start", "stop"):
        p = msub.add_parser(name, help="start or stop the module's hub (the thing that runs its operations)")
        p.add_argument("module")
    p = msub.add_parser("logs", help="its background jobs and their logs (setup/build/start/...)")
    p.add_argument("module")
    p.add_argument("--job", default=None, help="one job id (modules logs <module> --job abc123)")
    p.add_argument("--lines", type=int, default=20)
    p = msub.add_parser("run-op", help="call one of its hub operations")
    p.add_argument("module"); p.add_argument("operation"); p.add_argument("args", nargs="?")
    p.add_argument("--timeout", type=float, default=None)
    p = msub.add_parser("call", help="the same as run-op (its older name)")
    p.add_argument("module"); p.add_argument("operation"); p.add_argument("args", nargs="?")
    p.add_argument("--timeout", type=float, default=None)
    p = msub.add_parser("setup")
    p.add_argument("module")
    p.add_argument("action", choices=["install", "update", "build", "pipeline", "start", "stop", "open_gui", "open_tui",
                                      "register_mcp", "jobs"])
    p = msub.add_parser("adopt", help="make a project folder a module (writes abp-module.toml + abp-ops.toml)")
    p.add_argument("path"); p.add_argument("--id", default=""); p.add_argument("--name", default="")
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p = msub.add_parser("new", help="a new empty project that is already a module")
    p.add_argument("path"); p.add_argument("--lang", choices=["python", "node", "powershell", "shell"], default="python")
    p.add_argument("--id", default=""); p.add_argument("--name", default="")
    p = msub.add_parser("add", help="any git repo as a module: clone, adopt as an overlay, check, register")
    p.add_argument("url", help="https://github.com/owner/name (any https git host) or owner/name")
    p.add_argument("--id", default=""); p.add_argument("--name", default=""); p.add_argument("--branch", default="")
    p.add_argument("--full", action="store_true", help="clone the whole history (default: the latest commit)")
    p = msub.add_parser("candidates", help="project folders in a folder, and which are modules")
    p.add_argument("folder")
    p = msub.add_parser("publish", help="commit its module files and create its private GitHub repo")
    p.add_argument("module"); p.add_argument("--push", action="store_true")

    vis = sub.add_parser("vision", help="computer vision on this machine (OpenCV and its model zoo)")
    vsub = vis.add_subparsers(dest="vision_cmd", required=True)
    vsub.add_parser("status")
    p = vsub.add_parser("analyze", help="objects, faces, text, codes... in an image (a file, URL, 'screen')")
    p.add_argument("image")
    p.add_argument("--tasks", default="", help="comma-separated: info,objects,faces,text,codes,people,colors,shapes")
    p.add_argument("--min-score", type=float, default=0.4, dest="min_score")
    p = vsub.add_parser("find", help="where text or a smaller image is (on the screen by default)")
    p.add_argument("--image", default="screen"); p.add_argument("--text", default="")
    p.add_argument("--template", default="")
    p = vsub.add_parser("compare", help="what changed between two images")
    p.add_argument("before"); p.add_argument("after")
    p = vsub.add_parser("edit", help='edit an image: steps as JSON, e.g. [{"op": "resize", "width": 800}]')
    p.add_argument("image"); p.add_argument("steps")
    p = vsub.add_parser("fetch", help="download and verify one zoo model")
    p.add_argument("model")

    from abp_cli import host as _host
    _host.add_parser(sub)
    from abp_cli import nas as _nas
    _nas.add_parser(sub)
    from abp_cli import ai as _ai
    _ai.add_parser(sub)
    from abp_cli import hermes as _hermes
    _hermes.add_parser(sub)

    swarms = sub.add_parser("swarms", help="fan-out/leader-vote/etc. multi-bot swarms, and one-off sub-agent fan-outs")
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
    p = ssub.add_parser("dispatch", help="fan a goal out over sub-agents through one instance")
    p.add_argument("goal", nargs="*", help="the goal; split it up front with --task instead")
    p.add_argument("--task", action="append", default=None, help="a task of your own; repeatable")
    p.add_argument("--instance", type=int, default=None, dest="instance")
    p.add_argument("--provider", default=None, help="the model provider the sub-agents use")
    p.add_argument("--model", default=None, help="the model the sub-agents use")
    p.add_argument("--max-children", type=int, default=None, dest="max_children")
    p.add_argument("--confirm", action="store_true", help="accept a cost above the swarm budget's confirm line")
    p = ssub.add_parser("goal", help="one goal, handed to an instance to fan out itself")
    p.add_argument("goal", nargs="+")
    p.add_argument("--instance", type=int, default=None, dest="instance")
    p.add_argument("--native", action="store_true", help="run it as one task instead of a goal prompt")
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--max-children", type=int, default=None, dest="max_children")
    p.add_argument("--confirm", action="store_true")
    p = ssub.add_parser("status", help="the spending guard, the swarms, their latest runs, delegation activity")
    p.add_argument("--limit", type=int, default=20)

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

    for area, blurb, example in (
        ("tailscale", "every Tailscale setting, Serve/Funnel, devices, ACLs", "tailscale get overview | tailscale post serve --data '{\"target\":\"3000\"}'"),
        ("docker", "containers, images, volumes, networks, stacks, registries", "docker get containers | docker post containers/web/action --data '{\"action\":\"restart\"}'"),
        ("vm", "QEMU / Hyper-V / libvirt machines and disk images", "vm get '' | vm post qemu/dev/start"),
        ("rules", "automation rules for containers, VMs and Tailscale", "rules get '' | rules post '' --data '{...}'"),
        ("browser", "the ABP browser extension: pairing, status, and calling the connected browser", "browser get status | browser post pair/code | browser post rpc --data '{\"method\":\"tabs.list\"}'"),
    ):
        p = sub.add_parser(area, help=blurb, description=f"{blurb}. Examples: {example}")
        p.add_argument("verb", choices=["get", "post", "put", "patch", "delete"])
        p.add_argument("path", help="route under the area (use '' for the area's root)")
        p.add_argument("--data", default=None, help="JSON request body")

    envp = sub.add_parser("env", help="env-file status (redacted), or write a single key")
    envsub = envp.add_subparsers(dest="env_cmd")
    envsub.add_parser("status")
    p = envsub.add_parser("set", help="add/update one KEY=value without reading the file back")
    p.add_argument("key")
    p.add_argument("value")

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
    p.add_argument("--no-ssh-setup", action="store_true", dest="no_ssh_setup",
                    help="skip automatic SSH key exchange/connection setup with this peer")
    p = pesub.add_parser("remove"); p.add_argument("peer_id", type=int)
    for name in ("overview", "bots"):
        p = pesub.add_parser(name); p.add_argument("peer_id", type=int)

    editors = sub.add_parser("editors", help="the VS Code extension and the ACP command for other editors")
    esub = editors.add_subparsers(dest="editors_cmd", required=True)
    esub.add_parser("status")
    esub.add_parser("install-vscode", help="install or update ABP's VS Code extension")

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

    ssh = sub.add_parser("ssh", help="SSH Toolkit (github.com/LoopyLuci/SSH_Toolkit) - a separately maintained "
                                     "connection manager, vendored as a submodule (vendor/ssh_toolkit)")
    sshsub = ssh.add_subparsers(dest="ssh_cmd", required=True)
    sshsub.add_parser("status", help="whether the toolkit is available on this machine")
    sshsub.add_parser("list")
    p = sshsub.add_parser("show"); p.add_argument("name")
    p = sshsub.add_parser("add")
    p.add_argument("--name", required=True)
    p.add_argument("--host-name", required=True, dest="host_name")
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--user", default=None)
    p.add_argument("--identity-file", default=None, dest="identity_file")
    p.add_argument("--generate-key", action="store_true", dest="generate_key")
    p.add_argument("--proxy-jump", default=None, dest="proxy_jump")
    p.add_argument("--tags", default=None)
    p.add_argument("--multiplex", action="store_true")
    p.add_argument("--force", action="store_true")
    for name in ("remove", "test"):
        p = sshsub.add_parser(name); p.add_argument("name")
    p = sshsub.add_parser("run")
    p.add_argument("name")
    p.add_argument("command", nargs="+", help="the remote command; joined with spaces")
    sshsub.add_parser("status-all", help="reachability for every registered connection")
    sshsub.add_parser("visualize", help="the proxy-jump graph, as structured data")
    sshsub.add_parser("check-update")
    sshsub.add_parser("update")
    p = sshsub.add_parser("auto-update", help="get, or set, how a submodule/sidecar install of this toolkit "
                                               "picks up new releases")
    p.add_argument("mode", nargs="?", choices=["never", "notify", "auto"], default=None,
                   help="omit to just show the current setting")

    mem = sub.add_parser("memory", help="the memory fabric: memories, threads, the knowledge tree, sources, the vault")
    msub = mem.add_subparsers(dest="memory_cmd", required=True)
    p = msub.add_parser("search"); p.add_argument("q"); p.add_argument("--instance", type=int, default=None)
    p.add_argument("--limit", type=int, default=8)
    p = msub.add_parser("add"); p.add_argument("content"); p.add_argument("--kind", default=None)
    p.add_argument("--shared", action="store_true"); p.add_argument("--instance", type=int, default=None)
    p.add_argument("--source", default="user")
    p = msub.add_parser("list"); p.add_argument("--scope", default="shared")
    p.add_argument("--status", default=None, choices=["approved", "pending", "rejected"])
    p = msub.add_parser("delete"); p.add_argument("entry_id", type=int); p.add_argument("--scope", default="shared")
    p = msub.add_parser("approve"); p.add_argument("entry_id", type=int)
    p = msub.add_parser("reject"); p.add_argument("entry_id", type=int)
    p = msub.add_parser("context"); p.add_argument("q", nargs="?", default="")
    p.add_argument("--instance", type=int, default=None); p.add_argument("--thread", default="")
    p.add_argument("--backend", default="external")
    p = msub.add_parser("threads"); p.add_argument("--instance", type=int, default=None)
    p.add_argument("--limit", type=int, default=50)
    p = msub.add_parser("thread"); p.add_argument("thread"); p.add_argument("--after", type=int, default=0)
    p.add_argument("--limit", type=int, default=200)
    p = msub.add_parser("post-turn"); p.add_argument("thread"); p.add_argument("role", choices=["user", "assistant"])
    p.add_argument("text"); p.add_argument("--backend", default="external"); p.add_argument("--model", default="")
    p.add_argument("--instance", type=int, default=None)
    p = msub.add_parser("tree", help="query the knowledge tree")
    tsub2 = p.add_subparsers(dest="tree_cmd", required=True)
    q = tsub2.add_parser("walk", help="answer a question from the index, no model call")
    q.add_argument("query"); q.add_argument("--limit", type=int, default=10)
    q.add_argument("--max-hops", type=int, default=2, dest="max_hops")
    q.add_argument("--window", type=float, default=None, help="only the last N days")
    q = tsub2.add_parser("search-entities"); q.add_argument("name"); q.add_argument("--limit", type=int, default=20)
    q = tsub2.add_parser("neighbors"); q.add_argument("entity"); q.add_argument("--limit", type=int, default=20)
    q = tsub2.add_parser("source", help="what one source's index holds")
    q.add_argument("source_id"); q.add_argument("--query", default=""); q.add_argument("--limit", type=int, default=10)
    q.add_argument("--since", default=""); q.add_argument("--until", default="")
    q = tsub2.add_parser("drill-down"); q.add_argument("node_id")
    q.add_argument("--depth", type=int, default=1); q.add_argument("--query", default="")
    q.add_argument("--limit", type=int, default=20)
    q = tsub2.add_parser("cover-window", help="the fewest notes covering a span")
    q.add_argument("since"); q.add_argument("until"); q.add_argument("--source-id", default="", dest="source_id")
    q.add_argument("--limit", type=int, default=20)
    q = tsub2.add_parser("fetch-leaves"); q.add_argument("ids", nargs="+")
    p = msub.add_parser("tree-ingest"); p.add_argument("title"); p.add_argument("text")
    p.add_argument("--source-id", default=None)
    msub.add_parser("tree-stats")
    msub.add_parser("sources")
    p = msub.add_parser("source-add"); p.add_argument("kind", choices=["folder", "notes", "github", "rss", "web",
                                                                      "conversation"])
    p.add_argument("label"); p.add_argument("--path", default=None); p.add_argument("--repo", default=None)
    p.add_argument("--url", default=None); p.add_argument("--glob", default=None)
    p = msub.add_parser("source-sync"); p.add_argument("source_id")
    p = msub.add_parser("source-rm"); p.add_argument("source_id")
    p = msub.add_parser("diff"); p.add_argument("--source-id", default=""); p.add_argument("--checkpoint", default="")
    p.add_argument("--all", action="store_true"); p.add_argument("--no-commit", action="store_true")
    p.add_argument("--text", action="store_true")
    p = msub.add_parser("checkpoint"); p.add_argument("name")
    msub.add_parser("vault")
    msub.add_parser("vault-sync")
    p = msub.add_parser("rules"); p.add_argument("--tool", default="")
    p = msub.add_parser("rule-add"); p.add_argument("tool"); p.add_argument("rule")
    p.add_argument("--priority", default="normal", choices=["critical", "high", "normal"])
    p.add_argument("--tags", default=None); p.add_argument("--rule-id", default="")
    p = msub.add_parser("rule-remove"); p.add_argument("rule_id")
    p = msub.add_parser("goals"); p.add_argument("--all", action="store_true")
    p = msub.add_parser("goal-add"); p.add_argument("text"); p.add_argument("--status", default="active")
    p.add_argument("--goal-id", default="")
    p = msub.add_parser("goal-done"); p.add_argument("goal_id")
    msub.add_parser("settings")
    p = msub.add_parser("settings-set"); p.add_argument("changes", nargs="+")

    agt = sub.add_parser("agent", help="run a native agent, watch its runs, the swarm spending guard")
    agsub = agt.add_subparsers(dest="agent_cmd", required=True)
    p = agsub.add_parser("run", help="one real native-agent run, streaming its progress")
    p.add_argument("goal", nargs="+", help="what the agent should do; joined with spaces")
    p.add_argument("--instance", type=int, default=None, dest="instance",
                   help="the bot instance whose agent runs it (default: the first one an ABP Agent drives)")
    p.add_argument("--backend", default=None,
                   help="no instance: run it headlessly on this machine instead, on this provider")
    p.add_argument("--model", default=None, help="model for the run; provider/model on the local path")
    p.add_argument("--workspace", default=None, help="the folder the agent may use (local path only)")
    p.add_argument("--permission-mode", default=None, dest="permission_mode",
                   choices=["plan", "default", "accept_edits", "bypass"], help="local path only")
    p.add_argument("--approve", default="ask", choices=["ask", "allow", "deny"],
                   help="a tool approval that comes up: leave it pending, answer it once, or refuse it "
                        "(nobody is here, so a local run refuses anything that would need an answer)")
    p.add_argument("--wait", action="store_true", help="after the run, follow its job to a finished state")
    p.add_argument("--timeout", type=float, default=600.0, help="seconds to wait (default 600)")
    p.add_argument("--poll", type=float, default=0.4, help="seconds between progress polls (default 0.4)")
    p = agsub.add_parser("runs", help="the run ledger (/api/jobs)")
    p.add_argument("--instance", type=int, default=None)
    p.add_argument("--status", default=None, help="running, success, failed, ...")
    p.add_argument("--limit", type=int, default=50)
    p = agsub.add_parser("show", help="one run: its job row, tool events and per-child breakdown")
    p.add_argument("job_id", type=int)
    p = agsub.add_parser("cancel", help="cancel a fan-out that is still running")
    p.add_argument("swarm_run_id")
    p = agsub.add_parser("budget", help="the swarm spending guard; with no flag, just show it")
    p.add_argument("--enabled", action="store_true", default=None)
    p.add_argument("--max-children", type=int, default=None, dest="max_children")
    p.add_argument("--max-usd", type=float, default=None, dest="max_usd")

    tools = sub.add_parser("tools", help="what the native agent can call, and what each one needs permission for")
    tsub = tools.add_subparsers(dest="tools_cmd", required=True)
    p = tsub.add_parser("list")
    p.add_argument("--read-only", action="store_true", dest="read_only", help="only tools that change nothing")
    p.add_argument("--asks-first", action="store_true", dest="asks_first", help="only tools that need approval")

    appr = sub.add_parser("approvals", help="tool approvals: list, show, approve, deny")
    apsub = appr.add_subparsers(dest="approvals_cmd", required=True)
    p = apsub.add_parser("list")
    p.add_argument("--status", default="pending",
                   help="pending, all, approved_once, approved_session, approved_always, denied, expired")
    p.add_argument("--instance", type=int, default=None); p.add_argument("--limit", type=int, default=50)
    p = apsub.add_parser("show"); p.add_argument("approval_id", type=int)
    p = apsub.add_parser("approve"); p.add_argument("approval_id", type=int)
    p.add_argument("--outcome", default="once", choices=["once", "session", "always"],
                   help="once this call, for the session, or always (session/always need the dashboard token)")
    p = apsub.add_parser("deny"); p.add_argument("approval_id", type=int)

    rte = sub.add_parser("route", help="the model router: explain, rules, simulate, decisions, examples")
    rsub = rte.add_subparsers(dest="route_cmd", required=True)
    p = rsub.add_parser("explain"); p.add_argument("text")
    rsub.add_parser("rules")
    p = rsub.add_parser("set"); p.add_argument("policy"); p.add_argument("--note", default="")
    p = rsub.add_parser("simulate"); p.add_argument("text")
    p.add_argument("--candidates", default=None); p.add_argument("--images", action="store_true")
    p.add_argument("--context-tokens", type=int, default=0)
    p = rsub.add_parser("overview"); p.add_argument("--hours", type=int, default=24)
    rsub.add_parser("models")
    p = rsub.add_parser("decisions"); p.add_argument("--limit", type=int, default=100)
    p.add_argument("--mode", default=None); p.add_argument("--status", default=None)
    p.add_argument("--model", default=None); p.add_argument("--task-class", default=None)
    p = rsub.add_parser("decision"); p.add_argument("decision_id", type=int)
    p = rsub.add_parser("feedback"); p.add_argument("decision_id", type=int)
    p.add_argument("--rating", type=int, default=None); p.add_argument("--note", default="")
    p.add_argument("--correct-class", default=None); p.add_argument("--preferred-model", default=None)
    p = rsub.add_parser("examples"); p.add_argument("--limit", type=int, default=500)
    p = rsub.add_parser("example-add"); p.add_argument("text")
    p.add_argument("--task-class", default=None); p.add_argument("--preferred-model", default=None)
    p = rsub.add_parser("example-rm"); p.add_argument("example_id", type=int)
    p = rsub.add_parser("events"); p.add_argument("--limit", type=int, default=200)
    p.add_argument("--kind", default=None); p.add_argument("--model", default=None)
    p = rsub.add_parser("rest"); p.add_argument("model"); p.add_argument("--seconds", type=float, default=3600)
    p.add_argument("--reason", default="rested by hand")
    p = rsub.add_parser("release"); p.add_argument("model")
    p = rsub.add_parser("forget"); p.add_argument("model")
    p = rsub.add_parser("reset"); p.add_argument("--no-keep-policy", action="store_true")
    p.add_argument("--no-keep-examples", action="store_true")

    mods = sub.add_parser("models", help="model knowledge: list, free, usage, info")
    mosub = mods.add_subparsers(dest="models_cmd", required=True)
    p = mosub.add_parser("list"); p.add_argument("--provider", default=""); p.add_argument("--query", default="")
    p.add_argument("--free-only", action="store_true"); p.add_argument("--min-context", type=int, default=0)
    p.add_argument("--needs", default=""); p.add_argument("--limit", type=int, default=20)
    p = mosub.add_parser("free"); p.add_argument("--provider", default="")
    p.add_argument("--limit", type=int, default=20)
    p = mosub.add_parser("usage"); p.add_argument("--days", type=int, default=1)
    p = mosub.add_parser("info"); p.add_argument("provider"); p.add_argument("model")
    mosub.add_parser("refresh")

    priv = sub.add_parser("privacy", help="privacy mode: get or set")
    prsub = priv.add_subparsers(dest="privacy_cmd", required=True)
    prsub.add_parser("get")
    p = prsub.add_parser("set"); p.add_argument("--enabled", action="store_true")
    p.add_argument("--allow-lan", action="store_true")

    dns = sub.add_parser("dns", help="DNS: resolve a name, Tailscale DNS status")
    dnsub = dns.add_subparsers(dest="dns_cmd", required=True)
    p = dnsub.add_parser("resolve"); p.add_argument("name"); p.add_argument("--type", default="A")
    dnsub.add_parser("status")

    sub.add_parser("doctor", help="one-screen health check: can it reach ABP, which features answer")

    return ap


def main(argv=None) -> int:
    # No console window may ever appear on the desktop, whatever this or anything it calls
    # starts (bot/sandbox_ns/guard.py); a no-op off Windows.
    from bot.sandbox_ns import guard

    guard.install()
    args = _parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())