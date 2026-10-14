"""A dollar budget for one turn of the agent loop (roadmap: cost budget per turn).

    native_agent:
      budget:
        max_usd_per_turn: 0        # 0 = off. Stop a turn once its model calls have cost this much
        unknown_price: allow       # allow = carry on and say so once in the trace; stop = end the turn
        prices: {}                 # your own prices, $ per million tokens, replacing anything below:
                                   #   "openrouter/some-model": {input: 0.5, output: 1.5, cache_read: 0.05}
                                   #   "myhost/*": {free: true}

Prices are never fetched here. They come, in this order, from: the `prices` table above; "this model runs on
this computer or your own network, so it costs nothing" (ModelMistress, Ollama, LocalAI, LM Studio, vLLM,
llama.cpp by provider name; a private address alone is not free, since a local proxy may forward to a paid API); then bot/model_catalog.py, which reads the
models.dev catalog ABP already cached on disk (plus `native_agent.models.overrides` price_input/price_output).
A model none of those know is "unknown", and `unknown_price` decides what that means.

Reaching the budget is handled like the token limit (loop_guard.Watchdog): the turn ends with a short
summary, the session keeps every step, and "continue" carries on. Sub-agents started during a turn share
the turn's meter, so what they spend counts against it.
"""

from __future__ import annotations

import contextvars
import fnmatch
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("bot.turn_budget")

_MILLION = 1_000_000.0
LOCAL_PROVIDERS = frozenset({
    "ollama", "modelmistress", "localai", "lmstudio", "lm-studio", "llamacpp", "llama.cpp", "llama-cpp", "vllm", "unsloth", "unsloth-studio",
})


@dataclass(frozen=True)
class Budget:
    max_usd: float = 0.0
    unknown_price: str = "allow"      # "allow" | "stop"

    @property
    def on(self) -> bool:
        return self.max_usd > 0


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens. `source` says where the figure came from."""
    input: float
    output: float
    cache_read: Optional[float] = None
    cache_write: Optional[float] = None
    source: str = ""


def _cfg() -> dict:
    try:
        from bot.config import config

        return ((config.current.get("native_agent") or {}).get("budget")) or {}
    except Exception:  # noqa: BLE001
        return {}


def budget() -> Budget:
    cfg = _cfg()
    try:
        usd = max(0.0, float(cfg.get("max_usd_per_turn") or 0))
    except (TypeError, ValueError):
        usd = 0.0
    mode = str(cfg.get("unknown_price") or "allow").strip().lower()
    return Budget(usd, mode if mode in ("allow", "stop") else "allow")


# ---- where a price comes from -------------------------------------------------------------
def is_local(transport: Any) -> bool:
    """True when the provider is a local engine (LOCAL_PROVIDERS). A private or loopback address alone is not
    enough: a local router or proxy there may forward to a paid provider. Mark such a model `free: true` in
    `budget.prices` if it really costs nothing."""
    try:
        return str(getattr(transport, "provider_key", "") or "").lower() in LOCAL_PROVIDERS
    except Exception:  # noqa: BLE001
        return False


def is_loopback_endpoint(transport: Any) -> bool:
    """True when the transport's base URL host is this machine (localhost, 127.x, ::1). Only for choices that do not
    involve money (small-model mode's auto decision); it does not make the provider free - see is_local."""
    try:
        from urllib.parse import urlparse
        import ipaddress

        host = (urlparse(str(getattr(transport, "base_url", "") or "")).hostname or "").strip("[]").lower()
        if host == "localhost" or host.endswith(".localhost"):
            return True
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except Exception:  # noqa: BLE001
        return False


def _from_table(provider: str, model: str) -> Optional[Price]:
    table = _cfg().get("prices")
    if not isinstance(table, dict):
        return None
    names = [f"{provider}/{model}", model]
    keys = [str(k) for k in table]
    ordered = [k for k in keys if k in names] + sorted((k for k in keys if k not in names), key=len, reverse=True)
    for key in ordered:
        if key not in names and not any(fnmatch.fnmatchcase(n.lower(), key.lower()) for n in names):
            continue
        row = table.get(key)
        if not isinstance(row, dict):
            continue
        if row.get("free") is True:
            return Price(0.0, 0.0, source="config")
        if row.get("input") is None or row.get("output") is None:
            continue
        try:
            cache_read, cache_write = (None if row.get(k) is None else float(row[k]) for k in ("cache_read", "cache_write"))
            return Price(float(row["input"]), float(row["output"]), cache_read, cache_write, "config")
        except (TypeError, ValueError):
            continue
    return None


def price_for(transport: Any, model: str) -> Optional[Price]:
    """The price of `model` on this transport, or None when it is not known. Local files only."""
    provider = str(getattr(transport, "provider_key", "") or "")
    found = _from_table(provider, model)
    if found:
        return found
    if is_local(transport):
        return Price(0.0, 0.0, source="local")
    try:
        from bot import model_catalog

        info = model_catalog.lookup(provider, model, base_url=str(getattr(transport, "base_url", "") or ""))
    except Exception:  # noqa: BLE001
        return None
    if info.free:
        return Price(0.0, 0.0, source="catalog")
    if info.price_input is None or info.price_output is None:
        return None
    return Price(float(info.price_input), float(info.price_output), info.price_cache_read, info.price_cache_write, "catalog")


def cost_of(response: Any, price: Price) -> float:
    """Dollars for one model response. Tokens the provider read from its cache (or wrote to it) are charged at the
    cache price when there is one, else at the plain input price; a response that gives only a total is charged
    at the dearer of the two prices, so the figure errs high rather than low."""
    total = int(getattr(response, "tokens", None) or 0)
    if total <= 0:
        return 0.0
    cc = int(getattr(response, "cache_creation_tokens", None) or 0)
    cr = int(getattr(response, "cache_read_tokens", None) or 0)
    inp = getattr(response, "input_tokens", None)
    if inp is None:
        return total * max(price.input, price.output) / _MILLION
    plain_in = max(0, int(inp) - cc - cr)
    out = max(0, total - plain_in)
    cache_read = price.input if price.cache_read is None else price.cache_read
    cache_write = price.input if price.cache_write is None else price.cache_write
    return (plain_in * price.input + out * price.output + cr * cache_read + cc * cache_write) / _MILLION


# ---- the meter for one turn ------------------------------------------------------------------
class Meter:
    """What one turn (and the sub-agents it starts) has spent so far."""

    def __init__(self, bud: Optional[Budget] = None):
        self.budget = bud or budget()
        self.usd = 0.0                      # the part of the spend whose price was known
        self.unknown_calls = 0              # calls to a model with no known price
        self._noted: set[str] = set()

    def add(self, response: Any, transport: Any, model: str) -> Optional[float]:
        """Count one response. Returns its cost, or None when the model's price is not known."""
        try:
            price = price_for(transport, model)
        except Exception:  # noqa: BLE001
            logger.debug("price lookup failed", exc_info=True)
            price = None
        if price is None:
            self.unknown_calls += 1
            if self.budget.on and model not in self._noted:
                self._noted.add(model)
                from bot.agent_runtime import trace

                trace.active().note(
                    f"no price is known for {model}, so its cost is not counted toward the dollar budget"
                    + (" and the turn will stop (native_agent.budget.unknown_price)" if self.budget.unknown_price == "stop" else ""),
                    level="warn")
            return None
        cost = cost_of(response, price)
        self.usd += cost
        return cost

    def stop_reason(self) -> Optional[str]:
        b = self.budget
        if not b.on:
            return None
        if self.usd >= b.max_usd:
            return f"it reached the budget of ${b.max_usd:g} for one turn (about ${self.usd:.4f} spent)"
        if b.unknown_price == "stop" and self.unknown_calls:
            return "it used a model whose price is not known and the budget is set to stop in that case"
        return None


_current: contextvars.ContextVar[Optional[Meter]] = contextvars.ContextVar("turn_budget_meter", default=None)


def begin() -> tuple[Meter, Optional[contextvars.Token]]:
    """The turn's meter. A turn started inside another (a sub-agent) shares the outer one and gets no token."""
    existing = _current.get()
    if existing is not None:
        return existing, None
    meter = Meter()
    return meter, _current.set(meter)


def end(token: Optional[contextvars.Token]) -> None:
    if token is not None:
        _current.reset(token)


def active() -> Optional[Meter]:
    return _current.get()
