"""bot/hotreload.py's DENYLIST/RELOAD_ORDER classification — the thing
that keeps this from silently rotting as the codebase grows. Parses
every bot/**/*.py file's real imports via the `ast` module (no need to
actually import anything, so this can't accidentally reload a stateful
module as a side effect of testing) and checks two properties:

1. Every bot/**/*.py file (except __init__.py, and bot/main.py's own
   package markers) appears in exactly one of DENYLIST/RELOAD_ORDER — a
   newly added file that isn't classified fails this immediately instead
   of silently never hot-reloading.
2. RELOAD_ORDER is a genuine topological order against the real
   bot-internal import edges found — if module A imports something from
   module B, B must appear at an earlier index than A, or a reload cycle
   could leave A holding B's stale pre-reload value.
"""

from __future__ import annotations

import ast
from pathlib import Path

from bot.envfile import PROJECT_ROOT
from bot.hotreload import DENYLIST, RELOAD_ORDER

BOT_PKG_DIR = PROJECT_ROOT / "bot"


def _all_bot_modules() -> dict[str, Path]:
    """dotted module name -> file path, for every real bot/**/*.py file
    except __init__.py markers."""
    modules: dict[str, Path] = {}
    for path in BOT_PKG_DIR.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        rel = path.relative_to(BOT_PKG_DIR)
        dotted = "bot." + ".".join(rel.with_suffix("").parts)
        modules[dotted] = path
    return modules


def _toplevel_statements(body: list[ast.stmt]):
    """Yields statements at module scope, recursing into `if`/`try`
    blocks (still module scope, just conditionally executed) but not
    into function/class bodies. A lazy `from bot import X` inside a
    function re-resolves fresh every call — it never binds a stale
    reference the way a module-level import does — so it doesn't create
    the ordering hazard this check is for."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, ast.If):
            yield from _toplevel_statements(stmt.body)
            yield from _toplevel_statements(stmt.orelse)
        elif isinstance(stmt, ast.Try):
            yield from _toplevel_statements(stmt.body)
            for handler in stmt.handlers:
                yield from _toplevel_statements(handler.body)
            yield from _toplevel_statements(stmt.orelse)
            yield from _toplevel_statements(stmt.finalbody)


def _resolve_import_edges(path: Path, known_modules: set[str]) -> set[str]:
    """bot-internal module dotted-names this file's *module-level*
    imports depend on (see _toplevel_statements for why lazy/function-
    local imports are excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    edges: set[str] = set()
    for node in _toplevel_statements(tree.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("bot.") or alias.name == "bot":
                    # Truncate to the longest known-module prefix.
                    parts = alias.name.split(".")
                    for i in range(len(parts), 0, -1):
                        candidate = ".".join(parts[:i])
                        if candidate in known_modules:
                            edges.add(candidate)
                            break
        elif isinstance(node, ast.ImportFrom):
            if node.module is None or not (node.module == "bot" or node.module.startswith("bot.")):
                continue
            for alias in node.names:
                # `from bot.foo import bar` where bar is itself a submodule.
                submodule = f"{node.module}.{alias.name}"
                if submodule in known_modules:
                    edges.add(submodule)
                elif node.module in known_modules:
                    # bar is a symbol (function/class) defined in node.module itself.
                    edges.add(node.module)
    return edges


def test_every_bot_module_is_classified_exactly_once():
    all_modules = set(_all_bot_modules().keys())
    reload_set = set(RELOAD_ORDER)
    assert len(reload_set) == len(RELOAD_ORDER), "RELOAD_ORDER has a duplicate entry"
    overlap = DENYLIST & reload_set
    assert not overlap, f"modules in both DENYLIST and RELOAD_ORDER: {sorted(overlap)}"

    classified = DENYLIST | reload_set
    missing = all_modules - classified
    assert not missing, f"unclassified bot/*.py module(s) — add to DENYLIST or RELOAD_ORDER: {sorted(missing)}"

    extra = classified - all_modules
    assert not extra, f"classified module(s) no longer exist on disk: {sorted(extra)}"


def test_reload_order_is_a_valid_topological_order():
    all_files = _all_bot_modules()
    known_modules = set(all_files.keys())
    index = {name: i for i, name in enumerate(RELOAD_ORDER)}

    violations = []
    for mod_name in RELOAD_ORDER:
        path = all_files[mod_name]
        edges = _resolve_import_edges(path, known_modules)
        for dep in edges:
            if dep == mod_name:
                continue
            if dep in index and index[dep] > index[mod_name]:
                violations.append(f"{mod_name} (index {index[mod_name]}) imports {dep} (index {index[dep]})")
    assert not violations, "RELOAD_ORDER is not a valid topological order:\n" + "\n".join(violations)
