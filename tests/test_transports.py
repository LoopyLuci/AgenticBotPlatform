"""bot/agent_runtime/transports/ — the per-protocol translation layer
extracted from api_backend.py/custom_model_backend.py during the Native
Hermes-parity refactor. Covers the stored-history <-> wire-format
round trip directly, including the double-wrap bug fix found in the old
custom_model_backend.py (see openai_compatible.py's module docstring):
old rows stored a full message dict (with a redundant inner "role") as
the DB content value; _to_wire_messages must reconstruct correctly from
BOTH the old (buggy) and new (fixed) stored shapes, since real existing
databases may still have old-shape rows.
"""

from __future__ import annotations

from bot.agent_runtime.transports.anthropic import AnthropicTransport
from bot.agent_runtime.transports.base import ToolCall
from bot.agent_runtime.transports.openai_compatible import OpenAICompatibleTransport, _to_wire_messages


def test_anthropic_user_message_shape():
    t = AnthropicTransport()
    assert t.user_message("hi") == {"role": "user", "content": "hi"}


def test_anthropic_tool_result_messages_batches_into_one_user_message():
    t = AnthropicTransport()
    results = [(ToolCall(id="tu_1", name="list_dir", arguments={}), "a.txt"),
               (ToolCall(id="tu_2", name="read_file", arguments={}), "contents")]
    msgs = t.tool_result_messages(results)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "a.txt"},
        {"type": "tool_result", "tool_use_id": "tu_2", "content": "contents"},
    ]


def test_openai_user_message_shape():
    t = OpenAICompatibleTransport(base_url="http://x")
    assert t.user_message("hi") == {"role": "user", "content": "hi"}


def test_openai_tool_result_messages_one_per_call():
    t = OpenAICompatibleTransport(base_url="http://x")
    results = [(ToolCall(id="call_1", name="list_dir", arguments={}), "a.txt"),
               (ToolCall(id="call_2", name="read_file", arguments={}), "contents")]
    msgs = t.tool_result_messages(results)
    assert msgs == [
        {"role": "tool", "content": {"tool_call_id": "call_1", "content": "a.txt"}},
        {"role": "tool", "content": {"tool_call_id": "call_2", "content": "contents"}},
    ]


def test_wire_messages_reconstructs_new_shape_assistant_with_tool_calls():
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": {"content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}
        ]}},
        {"role": "tool", "content": {"tool_call_id": "call_1", "content": "a.txt"}},
    ]
    wire = _to_wire_messages(history)
    assert wire == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "a.txt"},
    ]


def test_wire_messages_tolerates_old_double_wrapped_shape():
    # Real pre-fix rows: the OLD custom_model_backend.py stored the WHOLE
    # message dict (redundant "role" key included) as the content value —
    # _to_wire_messages must still reconstruct a valid wire message from
    # this shape so an existing multi-turn conversation doesn't break on
    # the first /ask call after this fix ships.
    history = [
        {"role": "assistant", "content": {"role": "assistant", "content": "done", "tool_calls": None}},
        {"role": "tool", "content": {"role": "tool", "tool_call_id": "call_1", "content": "a.txt"}},
    ]
    wire = _to_wire_messages(history)
    assert wire[0] == {"role": "assistant", "content": "done"}
    assert wire[1] == {"role": "tool", "tool_call_id": "call_1", "content": "a.txt"}


def test_wire_messages_plain_string_content_passthrough():
    history = [{"role": "user", "content": "plain text"}]
    assert _to_wire_messages(history) == [{"role": "user", "content": "plain text"}]
