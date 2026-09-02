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

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from bot import db, setup_wizard
from bot.backends.api_backend import ApiBackend
from bot.backends.base import Backend, BackendError, BackendResult
from bot.backends.cli_backend import CliBackend
from bot.backends.custom_model_backend import CustomModelBackend
from bot.backends.hermes_cli_backend import HermesCliBackend
from bot.backends.hermes_gateway_backend import HermesGatewayBackend
from bot.backends.ui_backend import UiBackend
from bot.config import config

logger = logging.getLogger("bot.router")

VALID_BACKENDS = ("api", "cli", "ui", "hermes_cli", "hermes_gateway", "custom_model")

# A bot instance whose backend is crash-looping (every single request
# fails — bad credentials, a dead binary, a revoked key) used to just keep
# failing forever with nothing backing off, the same failure shape as the
# orphaned-scheduled-command incident this project already hit once in a
# different subsystem. OPEN_THRESHOLD consecutive failures trips the
# breaker; it stays open for COOLDOWN_S, then allows exactly one
# "half-open" trial call through before deciding whether to close (on
# success) or re-open (on failure) — the standard circuit-breaker shape.
CIRCUIT_OPEN_THRESHOLD = 5
CIRCUIT_COOLDOWN_S = 300


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    opened_at: Optional[float] = None  # time.monotonic(), None while closed


class CircuitOpenError(BackendError):
    """Raised without ever attempting the backend — the whole point of a
    breaker is to stop hammering something already known to be failing."""


class Router:
    def __init__(self):
        self._backends: dict[str, Backend] = {}
        self._cfg_version = -1
        self._circuits: dict[int, _CircuitState] = {}
        config.on_reload(lambda old, new: self._invalidate())

    async def shutdown_backends(self) -> None:
        """Tears down any backend holding a live external process/connection
        — currently just HermesGatewayBackend's spawned `hermes serve`.
        Called from bot/main.py's shutdown path."""
        await self._shutdown_backend_set(self._backends)

    @staticmethod
    async def _shutdown_backend_set(backends: dict[str, Backend]) -> None:
        for backend in backends.values():
            shutdown = getattr(backend, "shutdown", None)
            if shutdown is not None:
                try:
                    await shutdown()
                except Exception as exc:
                    logger.warning("error shutting down backend %r: %s", backend, exc)

    def _invalidate(self) -> None:
        # A config reload used to just drop the cache here with no shutdown
        # call, silently leaking anything holding a live external
        # process/connection (HermesGatewayBackend's spawned `hermes serve`,
        # confirmed to leak this way on every backends.yaml edit) — the
        # dashboard's own restore-backup path calls shutdown_backends()
        # properly; this hot-reload path just never did. Cache is swapped
        # first so a request landing mid-shutdown builds a fresh backend
        # immediately rather than waiting on the old one's teardown.
        old_backends = self._backends
        self._backends = {}
        if not old_backends:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "config reload discarded %d backend(s) with no running event loop to shut them down cleanly",
                len(old_backends),
            )
            return
        loop.create_task(self._shutdown_backend_set(old_backends))

    def _build_backend(
        self, name: str, cfg: dict, model_override: Optional[str] = None, hermes_home: Optional[str] = None
    ) -> Backend:
        b_cfg = (cfg.get("backends") or {}).get(name, {})
        if name == "api":
            from bot.models import DEFAULT_API_MODEL

            return ApiBackend(
                model=model_override or b_cfg.get("model", DEFAULT_API_MODEL),
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
                new_chat_button_automation_id=b_cfg.get("new_chat_button_automation_id"),
                sidebar_item_control_type=b_cfg.get("sidebar_item_control_type", "ListItem"),
            )
        if name == "hermes_cli":
            return HermesCliBackend(
                binary=b_cfg.get("binary", "hermes"),
                extra_args=b_cfg.get("extra_args", []),
                model=model_override or b_cfg.get("model"),
            )
        if name == "hermes_gateway":
            port = b_cfg.get("port", 8799)
            if hermes_home:
                # An isolated instance needs its own port too, or its
                # spawn would collide with the shared-default gateway (or
                # another isolated instance) already bound to `port` —
                # deterministic from hermes_home (stable across restarts,
                # unlike Python's randomized hash()) rather than random,
                # so the same instance always resolves to the same port.
                import zlib

                port = 8800 + (zlib.crc32(hermes_home.encode()) % 500)
            return HermesGatewayBackend(
                binary=b_cfg.get("binary", "hermes"),
                port=port,
                model=model_override or b_cfg.get("model"),
                hermes_home=hermes_home,
            )
        if name == "custom_model":
            from bot import providers

            model_ref = model_override or b_cfg.get("model")
            if not model_ref:
                raise ValueError(
                    "custom_model backend needs a model of the form '<provider>/<model_id>' "
                    "(set it on the bot instance, or as backends.custom_model.model)"
                )
            provider_name, model_id = providers.parse_model_ref(model_ref)
            provider = providers.get_provider(provider_name)
            if provider is None:
                raise ValueError(f"no provider named {provider_name!r} configured in config/providers.yaml")
            return CustomModelBackend(
                provider_name=provider_name,
                model_id=model_id,
                base_url=provider["base_url"],
                api_key=providers.get_api_key(provider_name),
                max_tokens=b_cfg.get("max_tokens", 4096),
            )
        raise ValueError(f"unknown backend {name!r}")

    def _get_backend(
        self, name: str, cfg: dict, model_override: Optional[str] = None, hermes_home: Optional[str] = None
    ) -> Backend:
        # A per-instance model override (bot_instances.model) or hermes_home
        # override (bot_instances.hermes_home) gets its own cache slot
        # instead of reusing the shared/global backend object for `name` —
        # otherwise one instance's custom model/isolated Hermes home would
        # leak onto every other instance routed to the same backend.
        key = f"{name}::{model_override}::{hermes_home}" if (model_override or hermes_home) else name
        if key not in self._backends:
            self._backends[key] = self._build_backend(name, cfg, model_override=model_override, hermes_home=hermes_home)
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
        # to it. The substitute is hardcoded to "api", not re-read from
        # cfg.get("default_backend") — if the user set default_backend
        # itself to "ui" (a one-click option in the dashboard), re-reading
        # it here would just hand back "ui" again, silently defeating this
        # exact guard.
        if not backend_override and chain and chain[0] == "ui" and not entry:
            chain = ["api"]
        return chain

    def circuit_status(self, instance_id: int) -> dict:
        """For the dashboard: whether this instance's breaker is
        currently open, and how many consecutive failures it's on."""
        state = self._circuits.get(instance_id)
        if state is None:
            return {"open": False, "consecutive_failures": 0}
        is_open = (
            state.opened_at is not None
            and time.monotonic() - state.opened_at < CIRCUIT_COOLDOWN_S
        )
        return {"open": is_open, "consecutive_failures": state.consecutive_failures}

    def reset_circuit(self, instance_id: int) -> None:
        """Manual override — a dashboard "retry now" action."""
        self._circuits.pop(instance_id, None)

    def _check_circuit(self, instance_id: int) -> None:
        state = self._circuits.get(instance_id)
        if state is None or state.opened_at is None:
            return
        elapsed = time.monotonic() - state.opened_at
        if elapsed < CIRCUIT_COOLDOWN_S:
            remaining = int(CIRCUIT_COOLDOWN_S - elapsed)
            db.log_telemetry(component=f"instance_{instance_id}", metric="circuit_open_reject", value=1)
            raise CircuitOpenError(
                f"bot instance {instance_id} is temporarily paused after {state.consecutive_failures} "
                f"consecutive failures — will automatically retry in {remaining}s, or fix the backend "
                f"and use the dashboard's retry action to resume immediately."
            )
        # Cooldown elapsed: allow exactly one half-open trial through by
        # falling through without raising. _record_circuit_result below
        # decides whether that trial closes the breaker or re-opens it.

    def _record_circuit_result(self, instance_id: int, *, success: bool) -> None:
        state = self._circuits.setdefault(instance_id, _CircuitState())
        if success:
            if state.consecutive_failures or state.opened_at is not None:
                logger.info("circuit breaker for instance %s closed (recovered)", instance_id)
            self._circuits.pop(instance_id, None)
            return
        state.consecutive_failures += 1
        if state.consecutive_failures >= CIRCUIT_OPEN_THRESHOLD:
            if state.opened_at is None:
                db.log_audit(
                    actor="system", action="circuit_breaker_open",
                    detail=f"instance {instance_id}: {state.consecutive_failures} consecutive failures",
                )
                logger.warning("circuit breaker for instance %s opened after %d consecutive failures",
                                instance_id, state.consecutive_failures)
            state.opened_at = time.monotonic()

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
        thread_id: Optional[Any] = None,
    ) -> BackendResult:
        if instance_id is not None:
            self._check_circuit(instance_id)

        cfg = config.current
        chain = self.resolve_chain(action_type, backend_override, instance_id=instance_id)
        timeouts = cfg.get("timeouts", {})

        instance_model: Optional[str] = None
        instance_hermes_home: Optional[str] = None
        desktop_session_key: Optional[str] = None
        # effective_prompt is what actually goes to the backend; prompt
        # itself stays the clean, original text for db.create_job() below
        # so Jobs-tab history isn't cluttered with the same instructions
        # repeated on every row for this instance.
        effective_prompt = prompt
        if instance_id is not None:
            from bot import bot_instances

            instance = bot_instances.get_instance(instance_id)
            if instance:
                instance_model = instance.get("model")
                instance_hermes_home = instance.get("hermes_home")
                # A chat-specific link (set by /new or /resume — see
                # db.link_chat_session()) always wins over the instance-wide
                # fallback bot_instances.desktop_session_key is used for:
                # per-chat linking is the real routing unit now, the
                # instance-level value only still matters for calls with no
                # chat_id (the dashboard's own "New Session" button) or for
                # a chat that's never linked its own session yet.
                if chat_id is not None:
                    chat_session = db.get_active_chat_session(instance_id, chat_id, thread_id=thread_id)
                    if chat_session is not None:
                        desktop_session_key = chat_session["desktop_session_key"]
                if desktop_session_key is None:
                    desktop_session_key = instance.get("desktop_session_key")
                custom_instructions = (instance.get("custom_instructions") or "").strip()
                if custom_instructions:
                    effective_prompt = f"{custom_instructions}\n\n{prompt}"

        # ui/hermes_gateway are session-aware backends (see their own
        # ask()) — they need to know which bot instance this call belongs
        # to and which already-created chat/session it's linked to, so they
        # never send into an unlinked/wrong conversation. Other backends
        # ignore these keys.
        context = dict(context or {})
        if instance_id is not None:
            context.setdefault("instance_id", instance_id)
            context.setdefault("desktop_session_key", desktop_session_key)

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

            backend = self._get_backend(backend_name, cfg, model_override=instance_model, hermes_home=instance_hermes_home)
            timeout_s = timeouts.get(backend_name, 30)
            t0 = time.monotonic()
            try:
                result = await backend.ask(effective_prompt, context=context, timeout_s=timeout_s)
                latency_ms = (time.monotonic() - t0) * 1000
                db.log_telemetry(component=backend_name, metric="latency_ms", value=latency_ms)
                db.log_connection_event(component=backend_name, event="request_ok")
                db.mark_job_done(job_id, status="success", result=result.text, tokens=result.tokens)
                if instance_id is not None and isinstance(result.raw, dict) and result.raw.get("desktop_session_key"):
                    reported_key = result.raw["desktop_session_key"]
                    if chat_id is not None:
                        # The backend lazily created a session because this
                        # chat had none linked yet (see create_session()'s
                        # docstring) — link it the same way an explicit
                        # /new would, so future messages in this chat keep
                        # reusing it instead of re-triggering this path.
                        db.link_chat_session(instance_id, chat_id, reported_key, thread_id=thread_id)
                    else:
                        from bot import bot_instances

                        bot_instances.set_desktop_session_key(instance_id, reported_key, actor="system")
                elif instance_id is not None and chat_id is not None and desktop_session_key is not None:
                    db.touch_active_chat_session(instance_id, chat_id, thread_id=thread_id)
                if instance_id is not None:
                    self._record_circuit_result(instance_id, success=True)
                return result
            except BackendError as exc:
                latency_ms = (time.monotonic() - t0) * 1000
                db.log_telemetry(component=backend_name, metric="latency_ms", value=latency_ms)
                db.log_connection_event(component=backend_name, event="request_error", detail=str(exc))
                logger.warning("job %s: backend %s failed: %s", job_id, backend_name, exc)
                last_error = exc

        db.mark_job_done(job_id, status="failed", error=str(last_error))
        if instance_id is not None:
            self._record_circuit_result(instance_id, success=False)
        raise last_error or BackendError("all backends in chain failed")

    def get_backend_for_instance(self, instance_id: int) -> Optional[Backend]:
        """The live Backend object this instance's own `backend`/`model`
        columns resolve to right now — same resolution `ask()` and
        create_session() use, exposed for callers that need to reach a
        backend-specific capability beyond ask() (e.g. HermesGatewayBackend's
        fetch_model_options()). Returns None if the instance doesn't exist."""
        from bot import bot_instances

        instance = bot_instances.get_instance(instance_id)
        if instance is None:
            return None
        cfg = config.current
        return self._get_backend(
            instance["backend"], cfg, model_override=instance.get("model"), hermes_home=instance.get("hermes_home")
        )

    async def create_session(self, instance_id: int, chat_id: Optional[Any] = None, thread_id: Optional[Any] = None) -> str:
        """Explicitly opens a brand-new chat/session in the real desktop
        agent app (Claude Desktop for "ui", Hermes for "hermes_gateway")
        for this specific bot instance, persists the link, and returns the
        new session key. With chat_id given (the normal /new path), the
        link is per-chat via db.link_chat_session() — that chat's future
        ask() calls resolve to this key regardless of what any other chat
        talking to the same instance is linked to. Without chat_id (the
        dashboard's own "New Session" button, which isn't chat-scoped),
        it falls back to the old instance-wide default. ask() always
        reuses whatever is already linked, never silently creates or
        switches one on its own (aside from the very first call for a
        chat with nothing linked yet, which lazily creates one exactly
        like this — see ask()'s own comment)."""
        from bot import bot_instances

        instance = bot_instances.get_instance(instance_id)
        if instance is None:
            raise BackendError(f"bot instance {instance_id} not found")

        backend_name = instance.get("backend")
        cfg = config.current
        backend = self._get_backend(
            backend_name, cfg, model_override=instance.get("model"), hermes_home=instance.get("hermes_home")
        )

        create = getattr(backend, "create_session", None)
        if create is None:
            raise BackendError(
                f"backend {backend_name!r} does not support session linking — only ui/hermes_gateway do"
            )
        key = await create()
        if chat_id is not None:
            db.link_chat_session(instance_id, chat_id, key, thread_id=thread_id)
        else:
            bot_instances.set_desktop_session_key(instance_id, key, actor="dashboard")
        return key

    async def resume_session(
        self, instance_id: int, chat_id: Any, chat_session_id: int, thread_id: Optional[Any] = None
    ) -> dict:
        """Re-links this chat to a previously-created backend session (one
        this instance made via create_session() at some point, for this
        chat or any other one) instead of creating a new one — the actual
        conversation on the backend side is whatever it was left as; this
        only changes which key BotServer's ask() sends future messages to.
        Raises BackendError if the id doesn't belong to this instance."""
        target = db.get_chat_session(chat_session_id)
        if target is None or target["instance_id"] != instance_id:
            raise BackendError(f"session {chat_session_id} not found for this bot")
        db.link_chat_session(instance_id, chat_id, target["desktop_session_key"], title=target["title"], thread_id=thread_id)
        return dict(target)


router = Router()
