"""A config reload is logged and written to the audit trail; it must never carry a key.

The summary used to hide only a key named exactly "secret", so adding a provider
printed its api_key into logs/bot.log and into the config_versions / audit rows.
"""
from __future__ import annotations

from bot.config import _diff_summary

PLACEHOLDER = "unused-placeholder-value"


def test_a_provider_added_whole_does_not_print_its_api_key():
    old = {"providers": {}}
    new = {"providers": {"acme": {"base_url": "https://example.test/v1", "api_key": PLACEHOLDER}}}
    summary = _diff_summary(old, new)
    assert PLACEHOLDER not in summary
    assert "https://example.test/v1" in summary  # the useful part is still there


def test_a_changed_api_key_is_reported_as_changed_without_the_value():
    old = {"providers": {"acme": {"api_key": PLACEHOLDER}}}
    new = {"providers": {"acme": {"api_key": PLACEHOLDER + "-2"}}}
    summary = _diff_summary(old, new)
    assert "api_key: changed" in summary
    assert PLACEHOLDER not in summary


def test_other_secret_named_keys_and_nested_lists_are_hidden():
    old = {"a": {"webhook": None}}
    new = {"a": {"webhook": {"items": [{"token": PLACEHOLDER}], "password": PLACEHOLDER, "name": "kept"}}}
    summary = _diff_summary(old, new)
    assert PLACEHOLDER not in summary
    assert "kept" in summary


def test_the_old_secret_key_is_still_hidden():
    summary = _diff_summary({"x": {"secret": PLACEHOLDER}}, {"x": {"secret": PLACEHOLDER + "2"}})
    assert PLACEHOLDER not in summary
    assert "secret: changed" in summary


def test_ordinary_changes_are_unchanged():
    summary = _diff_summary({"router": {"timeout": 5}}, {"router": {"timeout": 9}})
    assert summary == "router.timeout: 5 -> 9"
