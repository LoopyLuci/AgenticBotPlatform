"""Translates BotServer's Anthropic-shaped tool schemas
(bot/agent_runtime/tools.py's TOOL_SCHEMAS: {name, description,
input_schema}) into the OpenAI-compatible function-calling wire format,
so bot/backends/custom_model_backend.py can drive any OpenAI-compatible
endpoint through the exact same execute_tool()/DANGEROUS_TOOLS/approval/
checkpoint pipeline bot/backends/api_backend.py already uses for
Anthropic — one tool implementation, two wire formats.
"""

from __future__ import annotations


def to_openai_tools(anthropic_tool_schemas: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema.get("description", ""),
                "parameters": schema.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for schema in anthropic_tool_schemas
    ]
