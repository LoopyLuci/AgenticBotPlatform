"""Router.ask() must refuse new work while the global e-stop is engaged,
for EVERY backend type — not just the ones that happen to route through
NativeAgentBackend (api/custom_model/native_agent). Real gap found live:
cli/ui/hermes_cli/hermes_gateway backends never checked estop themselves
and have no shared base class of their own to add it to, so engaging the
estop silently failed to block new turns on any bot instance configured
with one of those four. Fixed by checking once at Router.ask() itself,
the one entry point every backend flows through — this test locks that
in for a non-NativeAgentBackend backend specifically, since that's
exactly the case that was broken.
"""
from __future__ import annotations

import asyncio

import pytest

from bot.agent_runtime import estop
from bot.agent_runtime.estop import EstopEngagedError
from bot.router import Router


def test_ask_refuses_when_estop_engaged_for_a_non_native_backend(temp_db, monkeypatch):
    # cli_backend (CliBackend) has no estop check of its own — it's one
    # of the four backend types that used to slip through entirely.
    monkeypatch.setattr(Router, "resolve_chain", lambda self, *a, **k: ["cli"])
    estop.engage("testing")

    async def _run():
        router = Router()
        with pytest.raises(EstopEngagedError):
            await router.ask("hello")

    asyncio.run(_run())


def test_ask_proceeds_past_the_estop_check_when_disengaged(temp_db, monkeypatch):
    # Disengaged is the default; resolve_chain is monkeypatched to a
    # backend name with no live process behind it so this test can assert
    # "got past the estop check" without a real backend actually running
    # — any error OTHER than EstopEngagedError proves the check let it
    # through and something further down (backend construction/dispatch)
    # is what failed instead.
    monkeypatch.setattr(Router, "resolve_chain", lambda self, *a, **k: ["cli"])
    assert not estop.is_engaged()

    async def _run():
        router = Router()
        try:
            await router.ask("hello")
        except EstopEngagedError:
            pytest.fail("estop.check() blocked a call when disengaged")
        except Exception:
            pass  # any other failure is fine — this test only cares about estop

    asyncio.run(_run())
