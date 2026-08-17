"""Backend router — resolves which backend answers a given message.

Precedence, cheapest override first:
  1. explicit --backend= flag on the message
  2. the action_type's entry in config.action_overrides
  3. config.default_backend
  4. on failure, retry once against the resolved entry's backup chain

Every attempt is logged to jobs/telemetry so the dashboard reflects exactly
what actually happened, not just what was configured to happen.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from bot import db, setup_wizard
from bot.backends.api_backend import ApiBackend
from bot.backends.base import Backend, BackendError, BackendResult
from bot.backends.cli_backend import CliBackend
from bot.backends.ui_backend import UiBackend
from bot.config import config

logger = logging.getLogger("bot.router")

VALID_BACKENDS = ("api", "cli", "ui")


class Router:
    def __init__(self):
        self._backends: dict[str, Backend] = {}
        self._cfg_version = -1
        config.on_reload(lambda old, new: self._invalidate())

    def _invalidate(self) -> None:
        self._backends = {}

    def _build_backend(self, name: str, cfg: dict) -> Backend:
        b_cfg = (cfg.get("backends") or {}).get(name, {})
        if name == "api":
            return ApiBackend(
                model=b_cfg.get("model", "claude-sonnet-5"),
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
        raise ValueError(f"unknown backend {name!r}")

    def _get_backend(self, name: str, cfg: dict) -> Backend:
        if name not in self._backends:
            self._backends[name] = self._build_backend(name, cfg)
        return self._backends[name]

    def resolve_chain(self, action_type: str, backend_override: Optional[str] = None) -> list[str]:
        cfg = config.current
        if backend_override:
            if backend_override not in VALID_BACKENDS:
                raise ValueError(f"unknown backend {backend_override!r}, expected one of {VALID_BACKENDS}")
            return [backend_override]

        overrides = cfg.get("action_overrides", {}) or {}
        entry = overrides.get(action_type)
        if entry:
            chain = [entry["backend"]] + list(entry.get("backup", []))
        else:
            chain = [cfg.get("default_backend", "api")]

        # ui never gets a silent default — only an explicit flag or an
        # explicit action_override may route to it.
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
    ) -> BackendResult:
        cfg = config.current
        chain = self.resolve_chain(action_type, backend_override)
        timeouts = cfg.get("timeouts", {})

        job_id = db.create_job(action_type=action_type, backend=chain[0], user_id=user_id, prompt=prompt)

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

            backend = self._get_backend(backend_name, cfg)
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
