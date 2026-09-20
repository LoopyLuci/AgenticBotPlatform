"""ToolSpec: every built-in tool says what it is, and the facts agree with the
approval and admin tables that already exist."""
from __future__ import annotations

import asyncio

import pytest

from bot.agent_runtime import toolspec, tools


def test_every_builtin_schema_has_a_spec_and_every_spec_a_schema():
    schema_names = {s["name"] for s in tools.TOOL_SCHEMAS}
    assert schema_names == set(toolspec.builtin_names()), (
        "a tool was added to (or removed from) TOOL_SCHEMAS without updating bot/agent_runtime/toolspec.py"
    )


def test_read_only_tools_never_need_approval_and_dangerous_tools_are_never_read_only():
    for name in toolspec.builtin_names():
        spec = toolspec.spec_for(name)
        if spec.read_only:
            assert name not in tools.DANGEROUS_TOOLS, f"{name} is read-only but needs approval"
        if name in tools.DANGEROUS_TOOLS:
            assert not spec.read_only, f"{name} needs approval but is marked read-only"


def test_admin_tools_are_classified_admin_or_read():
    for name in tools.ADMIN_TOOLS:
        assert toolspec.spec_for(name).permission in ("admin", "read"), name


def test_shell_and_plugin_authoring_are_execute():
    for name in ("run_shell", "create_plugin", "enable_plugin"):
        assert toolspec.spec_for(name).permission == "execute"


def test_only_read_only_tools_can_be_concurrency_safe():
    with pytest.raises(ValueError):
        toolspec.ToolSpec("x", "write", read_only=False, concurrency_safe=True)
    assert toolspec.is_concurrency_safe("read_file")
    assert not toolspec.is_concurrency_safe("write_file")


def test_unknown_permission_class_is_rejected():
    with pytest.raises(ValueError):
        toolspec.ToolSpec("x", "banana")


def test_unknown_tools_are_treated_conservatively():
    spec = toolspec.spec_for("some_tool_nobody_registered")
    assert spec.permission == "external" and not spec.read_only and not spec.concurrency_safe


def test_limit_output_keeps_head_and_tail_and_says_what_was_omitted():
    big = "A" * 20_000 + "MIDDLE" + "Z" * 20_000
    out = toolspec.limit_output("some_plugin_tool", big)
    assert len(out) < len(big)
    assert out.startswith("AAAA") and out.endswith("ZZZZ")
    assert "characters omitted" in out
    small = "short"
    assert toolspec.limit_output("some_plugin_tool", small) == small


def test_registered_tool_is_offered_dispatched_and_approval_gated_unless_read_only():
    seen = {}

    async def handler(tool_input, *, workspace, instance_id, device_tier):
        seen.update(tool_input=tool_input, workspace=workspace)
        return "handled"

    schema = {"name": "demo_edit", "description": "demo", "input_schema": {"type": "object", "properties": {}}}
    toolspec.register(schema, toolspec.ToolSpec("demo_edit", "write", origin="registered"), handler)
    try:
        assert any(s["name"] == "demo_edit" for s in tools.all_tool_schemas())
        assert tools.is_dangerous("demo_edit")
        out = asyncio.run(tools.execute_tool("demo_edit", {"a": 1}, workspace=tools.WORKSPACES_ROOT))
        assert out == "handled" and seen["tool_input"] == {"a": 1}
    finally:
        toolspec.unregister("demo_edit")
    assert not any(s["name"] == "demo_edit" for s in tools.all_tool_schemas())


def test_registering_over_a_builtin_or_with_mismatched_names_fails():
    async def handler(*a, **k):
        return ""

    with pytest.raises(ValueError):
        toolspec.register({"name": "run_shell"}, toolspec.ToolSpec("run_shell", "execute"), handler)
    with pytest.raises(ValueError):
        toolspec.register({"name": "a"}, toolspec.ToolSpec("b", "read", read_only=True), handler)
