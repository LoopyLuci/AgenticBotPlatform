"""Backend router — resolves which backend answers a given message.

Precedence, cheapest override first:
  1. explicit --backend= flag on the message
  2. (if instance_id given) that bot instance's own action_overrides[action_type]
  3. (if instance_id given) that bot instance's own default backend
  4. the action_type's entry in config.action_overrides (global fallback)
  5. config.default_backend (global fallback)
  6. on failure, retry once against the resolved entry's backup chain

Backend *definitions* (model, binary path, timeouts) stay global/shared in
config/backends.yaml regardless of instance — only the routing *choice* is
per-instance, so two bot instances both on "cli" share one CliBackend.

Every attempt is logged to jobs/telemetry so the dashboard reflects exactly
what actually happened, not just what was configured to happen.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from bot import db, setup_wizard
from bot.backends.api_backend import ApiBackend
from bot.backends.base import Backend, BackendError, BackendResult
from bot.backends.cli_backend import CliBackend
from bot.backends.hermes_cli_backend import HermesCliBackend
from bot.backends.hermes_gateway_backend import HermesGatewayBackend
from bot.backends.ui_backend import UiBackend
from bot.config import config

logger = logging.getLogger("bot.router")

VALID_BACKENDS = ("api", "cli", "ui", "hermes_cli", "hermes_gateway")


class Router:
    def __init__(self):
        self._backends: dict[str, Backend] = {}
        self._cfg_version = -1
        config.on_reload(lambda old, new: self._invalidate())

    async def shutdown_backends(self) -> None:
        """Tears down any backend holding a live external process/connection
        — currently just HermesGatewayBackend's spawned `hermes serve`.
        Called from bot/main.py's shutdown path."""
        for backend in self._backends.values():
            shutdown = getattr(backend, "shutdown", None)
            if shutdown is not None:
                try:
                    await shutdown()
                except Exception as exc:
                    logger.warning("error shutting down backend %r: %s", backend, exc)

    def _invalidate(self) -> None:
        self._backends = {}

    def _build_backend(self, name: str, cfg: dict, model_override: Optional[str] = None) -> Backend:
        b_cfg = (cfg.get("backends") or {}).get(name, {})
        if name == "api":
            return ApiBackend(
                model=model_override or b_cfg.get("model", "claude-sonnet-5"),
                max_tokens=b_cfg.get("max_tokens", 4096),
            )
        if name == "cli":
            from bot import desktop

            configured = b_cfg.get("binary", "claude")
            return CliBackend(
                binary=desktop.find_cli_path(configured) or configured,
                allowed_tools=b_cfg.get("allowed_tools", []),
                cwd=b_cfg.get("cwd"),
                extra_args=b_cfg.get("extra_args", []),
            )
        if name == "ui":
            return UiBackend(
                window_title_re=b_cfg.get("window_title_re", "Claude.*"),
                poll_interval_s=b_cfg.get("poll_interval_s", 0.5),
                input_automation_id=b_cfg.get("input_automation_id"),
                send_button_automation_id=b_cfg.get("send_button_automation_id"),
            )
        if name == "hermes_cli":
            return HermesCliBackend(
                binary=b_cfg.get("binary", "hermes"),
                extra_args=b_cfg.get("extra_args", []),
                model=model_override or b_cfg.get("model"),
            )
        if name == "hermes_gateway":
            return HermesGatewayBackend(
                binary=b_cfg.get("binary", "hermes"),
                port=b_cfg.get("port", 8799),
                model=model_override or b_cfg.get("model"),
            )
        raise ValueError(f"unknown backend {name!r}")

    def _get_backend(self, name: str, cfg: dict, model_override: Optional[str] = None) -> Backend:
        # A per-instance model override (bot_instances.model) gets its own
        # cache slot instead of reusing the shared/global backend object for
        # `name` — otherwise one instance's custom model would leak onto
        # every other instance routed to the same backend.
        key = f"{name}::{model_override}" if model_override else name
        if key not in self._backends:
            self._backends[key] = self._build_backend(name, cfg, model_override=model_override)
        return self._backends[key]

    def resolve_chain(
        self,
        action_type: str,
        backend_override: Optional[str] = None,
        instance_id: Optional[int] = None,
    ) -> list[str]:
        cfg = config.current
        if backend_override:
            if backend_override not in VALID_BACKENDS:
                raise ValueError(f"unknown backend {backend_override!r}, expected one of {VALID_BACKENDS}")
            return [backend_override]

        if instance_id is not None:
            from bot import bot_instances

            instance = bot_instances.get_instance(instance_id)
            if instance:
                inst_entry = (instance.get("action_overrides") or {}).get(action_type)
                if inst_entry:
                    return [inst_entry["backend"]] + list(inst_entry.get("backup", []))
                if instance.get("backend"):
                    # An instance's own backend is always an explicit choice
                    # (set when the bot was created/edited), so it's exempt
                    # from the "ui never gets a silent default" guard below —
                    # that guard only protects the global-config fallback.
                    return [instance["backend"]]

        overrides = cfg.get("action_overrides", {}) or {}
        entry = overrides.get(action_type)
        if entry:
            chain = [entry["backend"]] + list(entry.get("backup", []))
        else:
            chain = [cfg.get("default_backend", "api")]

        # ui never gets a silent default — only an explicit flag, an
        # explicit action_override, or an instance's own backend may route
        # to it.
        if not backend_override and chain and chain[0] == "ui" and not entry:
            chain = [cfg.get("default_backend", "api")]
        return chain

    async def ask(
        self,
        prompt: str,
        *,
        action_type: str = "quick_question",
        user_id: int = 0,
        backend_override: Optional[str] = None,
        context: Optional[dict] = None,
        instance_id: Optional[int] = None,
        swarm_run_id: Optional[str] = None,
        chat_id: Optional[Any] = None,
    ) -> BackendResult:
        cfg = config.current
        chain = self.resolve_chain(action_type, backend_override, instance_id=instance_id)
        timeouts = cfg.get("timeouts", {})

        instance_model: Optional[str] = None
        if instance_id is not None:
            from bot import bot_instances

            instance = bot_instances.get_instance(instance_id)
            if instance:
                instance_model = instance.get("model")

        job_id = db.create_job(
            action_type=action_type,
            backend=chain[0],
            user_id=user_id,
            prompt=prompt,
            instance_id=instance_id,
            swarm_run_id=swarm_run_id,
            chat_id=chat_id,
        )

        last_error: Optional[Exception] = None
        for i, backend_name in enumerate(chain):
            if i == 0:
                db.mark_job_running(job_id, backend=backend_name)
            else:
                db.mark_job_retrying(job_id, backend=backend_name)
                logger.info("job %s: %s failed, retrying via backup %s", job_id, chain[i - 1], backend_name)

            ready, reason = setup_wizard.check_backend_ready(backend_name)
            if not ready:
                exc = BackendError(
                    f"{backend_name} backend isn't set up yet — {reason}. "
                    f"Open the setup wizard from the dashboard (Control Center -> Environment) to fix it."
                )
                db.log_connection_event(component=backend_name, event="request_error", detail=str(exc))
                logger.warning("job %s: backend %s not ready: %s", job_id, backend_name, reason)
                last_error = exc
                continue

            backend = self._get_backend(backend_name, cfg, model_override=instance_model)
            timeout_s = timeouts.get(backend_name, 30)
            t0 = time.monotonic()
            try:
                result = await backend.ask(prompt, context=context, timeout_s=timeout_s)
                latency_ms = (time.monotonic() - t0) * 1000
                db.log_telemetry(component=backend_name, metric="latency_ms", value=latency_ms)
                db.log_connection_event(component=backend_name, event="request_ok")
                db.mark_job_done(job_id, status="success", result=result.text, tokens=result.tokens)
                return result
            except BackendError as exc:
                latency_ms = (time.monotonic() - t0) * 1000
                db.log_telemetry(component=backend_name, metric="latency_ms", value=latency_ms)
                db.log_connection_event(component=backend_name, event="request_error", detail=str(exc))
                logger.warning("job %s: backend %s failed: %s", job_id, backend_name, exc)
                last_error = exc

        db.mark_job_done(job_id, status="failed", error=str(last_error))
        raise last_error or BackendError("all backends in chain failed")


router = Router()
