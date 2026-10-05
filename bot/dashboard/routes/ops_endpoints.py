"""Dashboard routes: ops endpoints.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

import json

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from bot import bot_instances, db
from bot import commands as bot_commands
from bot import tasks as bg


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _app_chat_sessions, _require_token, _require_token_or_api_key, logger

    # Unauthenticated by design, like a load balancer's/orchestrator's health
    # probe is expected to be — neither returns anything a token would need
    # to protect (no secrets, no message content).

    @app.get("/healthz")
    def healthz():
        try:
            db.get_conn().execute("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False
        status_code = 200 if db_ok else 503
        # server_id: a random, non-secret identifier for THIS install, so the
        # Android app can tell "my server at a new address" from "some other
        # ABP on the network" before adopting an address (see
        # bot/server_identity.py).
        from bot import server_identity

        # bundle: which commit the code that is answering right now was built
        # from, and whether that differs from the checkout it was started
        # from. A release build runs the Python bundled next to its own exe,
        # so this is the only place a stale deploy can be seen from outside -
        # and scripts/deploy_local.py reads it to report the same thing after
        # every deploy. A commit hash is not a secret (this route is
        # unauthenticated by design), and every field is an empty string /
        # None rather than an error when it can't be determined.
        from bot.diagnostics import build_status

        return JSONResponse(
            {"status": "ok" if db_ok else "degraded", "db_ok": db_ok, "server_id": server_identity.get_server_id(),
             "bundle": build_status()},
            status_code=status_code,
        )

    @app.get("/api/activity", dependencies=[Depends(_require_token)])
    async def api_activity(since_id: int = 0, limit: int = 200):
        """Backs the GUI's Activity tab — every log record this process
        has emitted (bot/activity_log.py's ring buffer over the SAME
        root-logger handler chain logs/bot.log already uses), not a
        separate parallel event system. `since_id` lets a client that's
        already caught up ask for only what's new instead of re-fetching
        the whole buffer on every poll."""
        from bot import activity_log

        return {"entries": activity_log.recent(limit=min(limit, 2000), since_id=since_id)}

    @app.get("/api/diagnostics/summary", dependencies=[Depends(_require_token)])
    def api_diagnostics_summary():
        """Backs the GUI's Diagnostics tab: system info, local-only
        telemetry counters (error rates, self-heal/auto-restart counts),
        and how many crash reports are on disk. Nothing here is ever sent
        anywhere on its own — see /api/diagnostics/bundle for the
        exportable version a human can attach to a bug report."""
        from bot import diagnostics

        return {
            "system_info": diagnostics.system_info(),
            "telemetry": diagnostics.telemetry.snapshot(),
            "crash_report_count": len(diagnostics.list_crash_reports(limit=diagnostics.MAX_CRASH_REPORTS)),
        }

    @app.get("/api/diagnostics/crash-reports", dependencies=[Depends(_require_token)])
    def api_diagnostics_crash_reports(limit: int = 50):
        from bot import diagnostics

        return {"reports": diagnostics.list_crash_reports(limit=min(limit, diagnostics.MAX_CRASH_REPORTS))}

    @app.get("/api/diagnostics/crash-reports/{report_id}", dependencies=[Depends(_require_token)])
    def api_diagnostics_crash_report_detail(report_id: str):
        from bot import diagnostics

        report = diagnostics.get_crash_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="no such crash report")
        return report

    @app.get("/api/diagnostics/bundle", dependencies=[Depends(_require_token)])
    def api_diagnostics_bundle():
        """Builds (fresh, on demand — never pre-generated/cached) a zip of
        system info, telemetry, recent crash reports, and the bot.log
        tail for the user to download and attach to a bug report."""
        from bot import diagnostics

        path = diagnostics.build_support_bundle()
        return FileResponse(path, filename=path.name, media_type="application/zip")

    @app.post("/api/terminal/exec", dependencies=[Depends(_require_token)])
    async def api_terminal_exec(payload: dict = Body(...)):
        """The scoped terminal panel's only way of doing anything — runs
        exactly one ABP slash command (bot/commands.py::dispatch_command,
        the SAME dispatcher every Telegram/Discord/Slack handler already
        uses) and returns its reply text. Deliberately not a raw-shell
        route: this is reachable from the plain browser dashboard, which
        this app can expose to the public internet via Tailscale Funnel —
        real system-shell access lives only in the desktop app's own
        Tauri terminal (desktop-app/src-tauri/src/terminal.rs), which a
        browser can never reach regardless of network exposure. Strict
        _require_token (not _require_token_or_api_key): a paired mobile
        device has no business typing raw commands into this console."""
        text = (payload.get("text") or "").strip()
        instance_id = payload.get("instance_id")
        if not text:
            return {"output": ""}
        if not text.startswith("/"):
            return {"output": f"Not a recognized command: {text!r}. Commands start with / — try /help."}
        instance_name = ""
        if instance_id is not None:
            instance = bot_instances.get_instance(int(instance_id))
            if instance is None:
                return {"output": f"no such bot instance: {instance_id}"}
            instance_name = instance["name"]
        session = _app_chat_sessions.setdefault((instance_id, "terminal"), {})
        cmd_ctx = bot_commands.CmdContext(
            instance_id=int(instance_id) if instance_id is not None else None,
            instance_name=instance_name, user_id="terminal", chat_id="terminal",
            actor="terminal:dashboard", session=session,
        )
        try:
            reply = await bot_commands.dispatch_command(text, cmd_ctx)
        except Exception as exc:
            logger.exception("terminal command failed: %s", text)
            return {"output": f"error: {exc}"}
        return {"output": reply if reply is not None else f"Unknown command: {text!r}. Try /help."}

    @app.get("/metrics", dependencies=[Depends(_require_token_or_api_key)])
    def metrics():
        # Hand-rolled Prometheus text exposition format rather than the
        # prometheus_client dependency — this project's bundled venv is
        # deliberately kept minimal (see the NumPy-over-scikit-learn
        # rewrite), and a handful of gauges/counters don't need a library.
        overview = db.get_overview()
        lines = [
            "# HELP agenticbotplatform_up Always 1 if this endpoint responded at all.",
            "# TYPE agenticbotplatform_up gauge",
            "agenticbotplatform_up 1",
            "# HELP agenticbotplatform_jobs_running Jobs currently running.",
            "# TYPE agenticbotplatform_jobs_running gauge",
            f"agenticbotplatform_jobs_running {overview.get('jobs_running', 0)}",
            "# HELP agenticbotplatform_jobs_queued Jobs currently queued.",
            "# TYPE agenticbotplatform_jobs_queued gauge",
            f"agenticbotplatform_jobs_queued {overview.get('jobs_queued', 0)}",
            "# HELP agenticbotplatform_jobs_completed_today Jobs completed successfully today (resets at midnight local time).",
            "# TYPE agenticbotplatform_jobs_completed_today counter",
            f"agenticbotplatform_jobs_completed_today {overview.get('completed_today', 0)}",
            "# HELP agenticbotplatform_jobs_failed_today Jobs failed today (resets at midnight local time).",
            "# TYPE agenticbotplatform_jobs_failed_today counter",
            f"agenticbotplatform_jobs_failed_today {overview.get('failed_today', 0)}",
            "# HELP agenticbotplatform_job_success_rate_7d Fraction of jobs that succeeded over the trailing 7 days.",
            "# TYPE agenticbotplatform_job_success_rate_7d gauge",
            f"agenticbotplatform_job_success_rate_7d {overview.get('success_rate_7d', 0.0)}",
            "# HELP agenticbotplatform_job_avg_duration_ms Average job duration in milliseconds.",
            "# TYPE agenticbotplatform_job_avg_duration_ms gauge",
            f"agenticbotplatform_job_avg_duration_ms {overview.get('avg_duration_ms', 0)}",
            "# HELP agenticbotplatform_db_size_bytes SQLite database file size in bytes.",
            "# TYPE agenticbotplatform_db_size_bytes gauge",
            f"agenticbotplatform_db_size_bytes {db.get_db_size_bytes()}",
        ]
        from bot import diagnostics

        telemetry_counters = diagnostics.telemetry.snapshot()["counters"]
        lines += [
            "# HELP agenticbotplatform_crash_reports_total Crash reports written since process start.",
            "# TYPE agenticbotplatform_crash_reports_total counter",
            f"agenticbotplatform_crash_reports_total {telemetry_counters.get('crash_reports.written', 0)}",
            "# HELP agenticbotplatform_platform_crashes_total Bot instance crashes since process start.",
            "# TYPE agenticbotplatform_platform_crashes_total counter",
            f"agenticbotplatform_platform_crashes_total {telemetry_counters.get('platform.crash', 0)}",
            "# HELP agenticbotplatform_platform_auto_restarts_total Automatic bot-instance restarts since process start.",
            "# TYPE agenticbotplatform_platform_auto_restarts_total counter",
            f"agenticbotplatform_platform_auto_restarts_total {telemetry_counters.get('platform.auto_restart', 0)}",
        ]
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    # WhatsApp Cloud API delivers messages via a webhook Meta calls
    # directly — it can't send a dashboard token, so these two routes are
    # deliberately unauthenticated (same posture as /healthz above). The
    # POST route's real security boundary is the X-Hub-Signature-256 HMAC
    # check in whatsapp_platform.verify_signature(), not a header token.
    # See bot/platforms/whatsapp_platform.py's module docstring for setup.
    @app.get("/webhooks/whatsapp")
    async def whatsapp_verify(request: Request):
        from bot.platforms import whatsapp_platform

        params = request.query_params
        challenge = whatsapp_platform.verify_challenge(
            params.get("hub.mode", ""), params.get("hub.verify_token", ""), params.get("hub.challenge", "")
        )
        if challenge is None:
            raise HTTPException(status_code=403, detail="verification failed")
        return PlainTextResponse(challenge)

    @app.post("/webhooks/whatsapp")
    async def whatsapp_webhook(request: Request):
        from bot.platforms import whatsapp_platform

        raw = await request.body()
        if not whatsapp_platform.verify_signature(raw, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(status_code=403, detail="invalid signature")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": True}
        # Ack immediately — Meta retries aggressively if this endpoint is
        # slow, and a real agent turn can easily take longer than its
        # timeout. The reply goes out separately via the Graph API once
        # router.ask()/dispatch_command() finish, same as every other
        # platform's outbound path.
        bg.spawn(whatsapp_platform.handle_webhook_payload(payload))
        return {"ok": True}

    @app.get("/api/hotreload/status", dependencies=[Depends(_require_token)])
    async def api_hotreload_status():
        from bot import hotreload

        return hotreload.status()

    @app.post("/api/hotreload/run", dependencies=[Depends(_require_token)])
    async def api_hotreload_run():
        from bot import hotreload

        return await hotreload.trigger_manual_reload()

    # ------------------------------------------------------------ the lease
    # Who owns this data directory right now, and the two calls that move
    # ownership. The gate calls release on the outgoing instance and take on
    # the incoming one during a swap; `abp_cli instance swap` needs no direct
    # access at all beyond these.

    @app.get("/api/lease", dependencies=[Depends(_require_token)])
    async def api_lease_status():
        from bot import lease

        controller = lease.controller()
        if controller is not None:
            return controller.status()
        # Imported outside bot.main (a test, a one-off script): no controller
        # exists, so report the raw lock state instead of pretending to lead.
        return {**lease.status(), "singletons_running": False, "held": False, "wants_leadership": False}

    @app.post("/api/lease/release", dependencies=[Depends(_require_token)])
    async def api_lease_release():
        """Stop the lease-gated services and give up the lease, but keep
        serving the API. Idempotent - releasing when this process never held it
        is a no-op, not an error."""
        from bot import lease

        controller = lease.controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="this process has no lease controller (not started by bot.main)")
        return await controller.release()

    @app.post("/api/lease/take", dependencies=[Depends(_require_token)])
    async def api_lease_take(timeout: float = 30.0):
        """Take the lease as soon as it is free (up to `timeout` seconds).
        A --standby instance normally refuses, but gate-managed instances
        (ABP_GATE=1) are allowed to take it when explicitly requested."""
        import os
        from bot import lease

        controller = lease.controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="this process has no lease controller (not started by bot.main)")
        force = os.environ.get("ABP_GATE") == "1"
        return await controller.acquire(timeout=timeout, force=force)
