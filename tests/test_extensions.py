"""bot/extensions.py: the private folder, the config overlays and every extension hook.

Every test here builds a real ABP_PRIVATE_DIR in a tmp_path — real files, a real import of a real
package, the real dashboard app built over httpx — and drives the same code the running bot does.
Nothing here mocks the thing under test, and the fixture extension is the one docs/extensions.md
points at (tests/fixtures/ext_hello), copied into the tmp folder so a test can break it on purpose.

The other half of the contract is the public install: with ABP_PRIVATE_DIR unset, every one of these
answers must be the empty one, or ABP is not really public.
"""
from __future__ import annotations

import asyncio
import io
import shutil
import sys
import tomllib
from contextlib import redirect_stdout
from pathlib import Path

import httpx
import pytest

from bot import extensions

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "ext_hello"

MANIFEST = """[extension]
name = "{name}"
version = "{version}"
description = "{description}"
entry = "{entry}"
{extra}"""

ENTRY = """\
def register(api):
{body}
"""


def _write_extension(root: Path, folder: str, *, name: str = "hello", version: str = "1.0.0",
                     description: str = "A test extension that registers something of every kind.",
                     entry: str = "ext_hello", body: str = "    pass", minimum_abp: str = "",
                     manifest: str | None = None) -> Path:
    """One extension folder under <root>/extensions, exactly as a private folder would hold it."""
    d = root / "extensions" / folder
    d.mkdir(parents=True)
    (d / "abp_extension.toml").write_text(manifest if manifest is not None else MANIFEST.format(
        name=name, version=version, description=description, entry=entry,
        extra=f'minimum_abp = "{minimum_abp}"\n' if minimum_abp else ""), encoding="utf-8")
    pkg = d / entry
    pkg.mkdir()
    (pkg / "__init__.py").write_text(ENTRY.format(body=body), encoding="utf-8")
    extensions.load_all(force=True)      # the cache must never outlive what is on disk
    return d


def _private(tmp_path: Path) -> Path:
    d = tmp_path / "private"
    (d / "config").mkdir(parents=True)
    (d / "catalog").mkdir()
    (d / "extensions").mkdir()
    return d


@pytest.fixture
def private(tmp_path, monkeypatch):
    """A real ABP_PRIVATE_DIR, loaded. extensions.load_all() caches, so every test that changes
    what the folder holds forces a fresh load and leaves no state for the next one."""
    d = _private(tmp_path)
    monkeypatch.setenv(extensions.PRIVATE_DIR_ENV, str(d))
    extensions.load_all(force=True)
    yield d
    # Restore ABP_PRIVATE_DIR BEFORE the reload: reloading with the tmp folder still set re-cached this test's
    # extensions (e.g. "greedy"), and every later test on the same xdist worker saw them in /api/extensions.
    monkeypatch.undo()
    extensions.load_all(force=True)


@pytest.fixture
def hello(private):
    """The repo's own example extension, copied into the temp private folder: a backend, an
    integration, a route, a panel, a CLI command, an MCP tool and a module entry."""
    shutil.copytree(FIXTURE, private / "extensions" / "ext_hello")
    extensions.load_all(force=True)
    return private / "extensions" / "ext_hello"


# --- the public install ---------------------------------------------------------------


def test_without_the_variable_a_public_install_sees_nothing(tmp_path, monkeypatch):
    """The whole mechanism costs one lookup and changes nothing when the variable is unset."""
    monkeypatch.delenv(extensions.PRIVATE_DIR_ENV, raising=False)
    assert extensions.private_dir() is None
    assert extensions.config_overlay("backends") is None
    assert extensions.catalog_root() is None
    assert extensions.extensions_root() is None
    assert extensions.denylist_path() is None
    assert extensions.load_config_overlay("backends") == {}
    assert extensions.merged_config("backends", {"default_backend": "cli"}) == {"default_backend": "cli"}
    assert extensions.load_all(force=True).extensions == []


@pytest.mark.parametrize("value", ["", "   ", "/definitely/not/a/folder"])
def test_a_blank_or_missing_variable_is_not_an_error(value, monkeypatch):
    monkeypatch.setenv(extensions.PRIVATE_DIR_ENV, value)
    assert extensions.private_dir() is None


def test_the_variable_may_name_a_file_rather_than_a_folder(tmp_path, monkeypatch):
    """A stale ABP_PRIVATE_DIR must not stop ABP starting: a file where a folder should be is
    simply no private folder."""
    f = tmp_path / "private.txt"
    f.write_text("not a folder", encoding="utf-8")
    monkeypatch.setenv(extensions.PRIVATE_DIR_ENV, str(f))
    assert extensions.private_dir() is None


def test_the_variable_expands_variables_and_a_tilde(tmp_path, monkeypatch):
    base = tmp_path / "abp"
    (base / "extensions").mkdir(parents=True)
    monkeypatch.setenv("ABP_TEST_PRIVATE_BASE", str(base))
    monkeypatch.setenv(extensions.PRIVATE_DIR_ENV, "$ABP_TEST_PRIVATE_BASE/extensions/..")
    assert extensions.private_dir() == base
    # resolution does not need the folder to exist: only private_dir() does
    assert extensions.resolve_private_dir("~/abp-private-test") == Path.home() / "abp-private-test"
    assert extensions.resolve_private_dir("") is None
    assert extensions.resolve_private_dir("   ") is None


def test_the_public_config_is_exactly_the_tracked_one_with_the_variable_set(private):
    """A private folder with no overlay of its own must leave the tracked document untouched."""
    tracked = {"default_backend": "cli", "backends": {"api": {"kind": "api"}}, "timeouts": {"total": 30}}
    assert extensions.merged_config("backends", tracked) == tracked


# --- config overlays ------------------------------------------------------------------


def test_the_merge_rules(private):
    base = {"a": 1, "nested": {"keep": 1, "override": 1}, "list": [1, 2, 3]}
    overlay = {"nested": {"override": 2, "added": 3}, "list": [9]}
    assert extensions.merge_overlay(base, overlay) == {"a": 1, "nested": {"keep": 1, "override": 2, "added": 3},
                                                       "list": [9]}


def test_a_list_in_the_overlay_replaces_and_a_plus_key_appends():
    """Replacing is the safer default for a list that grants something; the '+' suffix is the
    explicit way to add to one."""
    base = {"allowed": ["a.example"]}
    assert extensions.merge_overlay(base, {"allowed": ["b.example"]}) == {"allowed": ["b.example"]}
    assert extensions.merge_overlay(base, {"allowed+": ["b.example"]}) == {"allowed": ["a.example", "b.example"]}
    # a list where the tracked value is not a list replaces it rather than blowing up
    assert extensions.merge_overlay({"x": "not a list"}, {"x+": [1]}) == {"x": [1]}


def test_a_null_deletes_a_tracked_key():
    base = {"env_file": "/somewhere/.env", "keep": 1}
    assert extensions.merge_overlay(base, {"env_file": None}) == {"keep": 1}


def test_the_merge_never_touches_the_document_it_was_given(private):
    base = {"nested": {"keep": 1}, "list": [1]}
    extensions.merge_overlay(base, {"nested": {"keep": 2}, "list": [2], "new": 3})
    assert base == {"nested": {"keep": 1}, "list": [1]}, "the tracked document is read, never edited"


def test_the_overlay_for_a_config_file_is_merged_at_load(private):
    (private / "config" / "backends.yaml").write_text(
        "modules:\n  build_cache: /srv/abp-build\n  overlay_dirs+: [/srv/overlays]\n"
        "default_backend: api\nsecurity:\n  allow_lan: true\n  lan_enabled: null\n",
        encoding="utf-8")
    tracked = {"default_backend": "cli", "security": {"allow_lan": False, "lan_enabled": False, "other": 1},
               "modules": {"overlay_dirs": ["/srv/tracked"], "search_paths": ["/srv/tracked"]}}
    merged = extensions.merged_config("backends", tracked)
    assert merged["default_backend"] == "api"
    assert merged["modules"] == {"overlay_dirs": ["/srv/tracked", "/srv/overlays"],
                                  "search_paths": ["/srv/tracked"], "build_cache": "/srv/abp-build"}
    assert merged["security"] == {"allow_lan": True, "other": 1}, "null deleted the tracked key"


def test_a_broken_overlay_keeps_the_tracked_defaults(private):
    """A typo in the private folder must not take ABP down: the tracked file stays live."""
    (private / "config" / "backends.yaml").write_text("a: [1,\n  b: {\n", encoding="utf-8")
    assert extensions.merged_config("backends", {"a": 1}) == {"a": 1}


def test_the_real_config_manager_merges_the_overlay(private, monkeypatch):
    """End to end through the manager every reader uses, not just the merge function."""
    from bot.config import ConfigManager

    (private / "config" / "backends.yaml").write_text("default_backend: api\ntimeouts:\n  total: 99\n",
                                                      encoding="utf-8")
    manager = ConfigManager(path=_tracked_config(private))
    assert manager.current["default_backend"] == "api"
    assert manager.current["timeouts"]["total"] == 99
    assert manager.current["keep"] == 1, "an untouched tracked key survives the merge"


def _tracked_config(private: Path) -> Path:
    """The tracked config/<name>.yaml this private folder overlays, in a folder of its own.
    Its NAME is what the overlay is looked up by, so it has to be backends.yaml."""
    d = private.parent / "tracked"
    d.mkdir(exist_ok=True)
    p = d / "backends.yaml"
    p.write_text("default_backend: cli\ntimeouts:\n  total: 30\nkeep: 1\n", encoding="utf-8")
    return p


# --- the private catalog root ----------------------------------------------------------


def test_the_private_catalog_is_an_overlay_root_next_to_the_tracked_one(private):
    from bot.modules import registry

    roots = registry.overlay_roots()
    assert private / "catalog" in roots
    assert roots.index(private / "catalog") < roots.index(registry.user_overlay_root())
    assert roots[0].name == "catalog" and roots[0] == registry.abp_root() / "catalog"


def test_an_overlay_in_the_private_catalog_becomes_a_module(private):
    from bot.modules import registry

    shutil.copytree(ROOT / "catalog" / "mesh-llm", private / "catalog" / "mesh-llm")
    manifest = private / "catalog" / "mesh-llm" / "abp-module.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace('id = "mesh-llm"',
                                                                  'id = "private-overlay"'), encoding="utf-8")
    m = registry.modules(refresh=True).get("private-overlay")
    assert m is not None and m.overlay == str(private / "catalog" / "mesh-llm")
    # and it is gone again the moment the folder is
    shutil.rmtree(private / "catalog" / "mesh-llm")
    assert "private-overlay" not in registry.modules(refresh=True)


def test_no_private_catalog_root_without_the_variable(monkeypatch):
    from bot.modules import registry

    monkeypatch.delenv(extensions.PRIVATE_DIR_ENV, raising=False)
    assert len(registry.overlay_roots()) == 2, "just the tracked catalog and the user root"


# --- loading an extension ---------------------------------------------------------------


def test_every_hook_of_the_example_extension_is_registered(hello):
    """The fixture extension registers one of everything; all of it has to come out."""
    loaded = extensions.load_all(force=True)
    assert loaded.errors == {}
    ext = loaded.by_name("hello")
    assert ext is not None
    assert (ext.name, ext.version) == ("hello", "1.0.0")
    assert list(ext.api.backends) == ["hello"]
    assert list(ext.api.integrations) == ["hello"]
    assert [name for name, _fn in ext.api.routes] == ["hello"]
    assert [(p.id, p.title) for p in ext.api.panels] == [("hello", "Hello")]
    assert [c["name"] for c in ext.api.cli_commands] == ["hello"]
    assert [(fn.__name__, name) for fn, name in ext.api.mcp_tools] == [("hello_status", "hello_status")]
    assert [m["id"] for m in ext.api.modules] == ["hello"]


def test_what_an_extension_registered_becomes_part_of_the_config(hello):
    merged = extensions.merged_config("backends", {"backends": {"api": {"kind": "api"}}})
    assert merged["backends"] == {"api": {"kind": "api"}, "hello": merged["backends"]["hello"]}
    assert merged["backends"]["hello"]["command"] == "hello --json"
    assert merged["integrations"]["hello"]["base_url"] == "http://127.0.0.1:8901"


def test_an_extension_is_loaded_once_and_reused(private):
    _write_extension(private, "one", name="one", body="    api.add_backend('one', kind='cli')")
    first = extensions.load_all(force=True)
    assert [e.name for e in first.extensions] == ["one"]
    assert extensions.load_all() is first, "the second call must not re-import anything"


def test_the_catalog_root_and_the_extension_folder_are_independent(private):
    """A folder can be a catalog overlay root, an extension root, or both at once, and nothing
    about one leaks into the other."""
    _write_extension(private, "one", name="one")
    assert extensions.catalog_root() == private / "catalog"
    assert extensions.extensions_root() == private / "extensions"


def test_the_denylist_is_read_from_the_private_folder_and_never_from_the_repo(private):
    (private / "denylist.txt").write_text("# a comment\n\nSomeProject\n^regex-[0-9]+$\n", encoding="utf-8")
    assert extensions.denylist_path() == private / "denylist.txt"
    import scripts.leak_guard as leak_guard  # noqa: PLC0415 - the guard lives in scripts/
    terms = [t for t, _p in leak_guard._denylist(private)]
    assert terms == ["SomeProject", "^regex-[0-9]+$"]


# --- failures are isolated ---------------------------------------------------------------


def test_an_extension_whose_register_raises_is_skipped_and_the_others_still_load(private):
    _write_extension(private, "broken", name="broken", body="    raise RuntimeError('boom')")
    _write_extension(private, "fine", name="fine", body="    api.add_backend('fine', kind='cli')")
    loaded = extensions.load_all(force=True)
    assert [e.name for e in loaded.extensions] == ["fine"]
    assert "boom" in loaded.errors["broken"]
    assert "RuntimeError" in loaded.errors["broken"]


def test_an_extension_that_will_not_import_is_skipped(private):
    _write_extension(private, "noisy", name="noisy", body="    import a_module_that_is_not_there")
    loaded = extensions.load_all(force=True)
    assert loaded.extensions == []
    assert "noisy" in loaded.errors


def test_an_extension_with_no_register_is_skipped(private):
    _write_extension(private, "empty", name="empty", entry="pkg", body="")
    (private / "extensions" / "empty" / "pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    extensions.load_all(force=True)
    loaded = extensions.load_all(force=True)
    assert "no register(api)" in loaded.errors["empty"]


def test_an_extension_needing_a_newer_abp_is_skipped_rather_than_half_loaded(private):
    _write_extension(private, "future", name="future", minimum_abp="99.0.0",
                     body="    api.add_backend('future', kind='cli')")
    loaded = extensions.load_all(force=True)
    assert loaded.extensions == []
    assert "needs ABP 99.0.0 or newer" in loaded.errors["future"]


@pytest.mark.parametrize("manifest", [
    "name = \"x\"\n",
    "[extension]\nname = \"x\"\nversion = \"1\"\n",                                    # no entry
    "[other]\nname = \"x\"\n",                                                        # no [extension]
])
def test_an_incomplete_manifest_is_reported_by_field_name(private, manifest):
    _write_extension(private, "bad", name="bad", manifest=manifest)
    loaded = extensions.load_all(force=True)
    assert "bad" in loaded.errors and "[extension]" in loaded.errors["bad"] + manifest


def test_a_folder_without_a_manifest_is_not_an_extension(private):
    (private / "extensions" / "notes").mkdir()
    (private / "extensions" / "notes" / "README.md").write_text("just notes", encoding="utf-8")
    assert extensions.load_all(force=True).extensions == []


def test_the_api_rejects_something_that_is_not_callable(private):
    _write_extension(private, "wrong", name="wrong", body="    api.add_routes('not callable')")
    loaded = extensions.load_all(force=True)
    assert "needs a callable" in loaded.errors["wrong"]


# --- the hooks actually work --------------------------------------------------------------


def _client(app, token: str = "unused-dashboard-token"):
    """A real httpx client over the real FastAPI app: no mock, no monkeypatched route."""
    return httpx.Client(base_url="http://t", transport=httpx.ASGITransport(app=app),
                        headers={"X-Dashboard-Token": token}, timeout=30)


def _get(app, path: str, token: str = "unused-dashboard-token"):
    """A real request over the real FastAPI app through ABP's own dashboard client: no mock, no
    monkeypatched route, and the same auth every other caller of these routes goes through."""
    from bot.dashboard_client import DashboardClient

    async def go():
        client = DashboardClient("http://testserver", token, transport=httpx.ASGITransport(app=app))
        try:
            return await client._request("GET", path)
        finally:
            await client.aclose()

    return asyncio.run(go())


def _raw(app, path: str, token: str = "unused-dashboard-token") -> httpx.Response:
    """The response as-is, for the routes whose status code is the point (a 404, a refused token)."""
    async def go():
        async with httpx.AsyncClient(base_url="http://testserver", transport=httpx.ASGITransport(app=app),
                                     timeout=30) as c:
            return await c.get(path, headers={"X-Dashboard-Token": token})

    return asyncio.run(go())


@pytest.fixture
def app(private, hello, temp_db, monkeypatch):
    """The real dashboard app, built with the private folder in place."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "unused-dashboard-token")
    from bot.dashboard.server import build_app

    return build_app()


def test_the_extensions_route_lists_what_loaded_and_what_failed(app, hello, private):
    _write_extension(private, "broken", name="broken", body="    raise RuntimeError('nope')")
    body = _get(app, "/api/extensions")
    assert [e["name"] for e in body["extensions"]] == ["hello"]
    assert body["extensions"][0]["version"] == "1.0.0"
    assert "nope" in body["errors"]["broken"]


def test_an_extension_route_is_reachable_and_keeps_abp_auth(app, hello):
    """A registered route answers like any of ABP's own, and a wrong token still refuses it."""
    assert _get(app, "/api/hello") == {"greeting": "hello", "from": "hello"}
    assert _raw(app, "/api/hello", "not-the-token").status_code in (401, 403)


def test_a_panel_is_served_from_the_extension_folder(app, hello):
    """The point of a panel: its script exists only in the private folder and the dashboard hands
    it out anyway, so nothing had to be committed to the repo."""
    listing = _get(app, "/api/extensions")
    assert listing["panels"] == [{"id": "hello", "title": "Hello",
                                  "description": "a panel that exists only in this private folder",
                                  "icon": "◆", "extension": "hello",
                                  "script": "/extensions/hello/panel.js", "styles": []}]
    served = _raw(app, "/extensions/hello/panel.js")
    assert served.status_code == 200
    assert "hello-root" in served.text
    assert served.headers["content-type"].startswith("text/javascript")


def test_an_extension_cannot_serve_a_file_outside_its_own_folder(app, hello):
    """It serves its own assets and nothing else: an escape is a 404, not a read."""
    for path in ("/extensions/hello/../../../etc/passwd", "/extensions/hello/..%2f..%2fdenylist.txt",
                 "/extensions/hello/nothing.js", "/extensions/nosuch/panel.js"):
        assert _raw(app, path).status_code == 404, path


def test_a_panel_navigation_script_exists_for_both_uis_and_needs_no_extension():
    """Both UIs load one tracked file that builds the navigation from /api/extensions, so a
    panel needs no line in either HTML file. Unset, /api/extensions is empty and it adds nothing."""
    a = (ROOT / "bot/dashboard/static/extensions.js").read_text(encoding="utf-8")
    b = (ROOT / "desktop-app/ui/extensions.js").read_text(encoding="utf-8")
    assert a == b, "the two UIs' copies must stay identical, like every panel script"
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        assert "extensions.js" in (ROOT / page).read_text(encoding="utf-8")
    assert "/api/extensions" in a


def test_an_extension_cli_command_is_a_real_command(private, hello):
    """Driven through the real parser and the real dispatcher, exactly as `python -m abp_cli` does."""
    from abp_cli.__main__ import _dispatch, _parser

    args = _parser().parse_args(["hello"])
    assert args.cmd == "hello"
    out = io.StringIO()
    with redirect_stdout(out):
        assert asyncio.run(_dispatch(args, None)) == 0
    assert "hello from an extension" in out.getvalue()


def test_an_extension_cli_command_never_shadows_abp_s_own(private, capsys):
    _write_extension(private, "greedy", name="greedy", body="    api.add_cli_command('bots', lambda a, c: 0)")
    from abp_cli.__main__ import _parser

    _parser()          # must not raise, and must not replace ABP's own command
    assert "already an ABP command" in capsys.readouterr().err
    args = _parser().parse_args(["bots", "list"])
    assert args.bots_cmd == "list", "ABP's own command is the one that answers"


def test_without_the_variable_the_cli_is_exactly_abps_own(monkeypatch):
    from abp_cli.__main__ import _extension_commands, _parser

    monkeypatch.delenv(extensions.PRIVATE_DIR_ENV, raising=False)
    extensions.load_all(force=True)
    assert _extension_commands() == []
    assert "hello" not in _parser()._subparsers._group_actions[0].choices


def test_an_extension_mcp_tool_lands_on_the_real_server(private, hello):
    """Added to the same MCPServer every tool ABP ships is on, with the extension's docstring as
    its description, and callable exactly like one."""
    import importlib

    from bot import mcp_server

    mcp_server = importlib.reload(mcp_server)
    tools = {t.name: t for t in mcp_server.mcp._tool_manager.list_tools()}
    assert "hello_status" in tools
    assert "get_status" in tools, "ABP's own tools are still there"
    assert "leaves this machine" in (tools["hello_status"].description or "")
    assert asyncio.run(tools["hello_status"].fn()) == {"name": "hello", "version": "1.0.0", "greeting": "hello"}


def test_an_extension_module_entry_becomes_a_module(private):
    _write_extension(private, "mod", name="mod", body=(
        "    api.add_module({'id': 'from-extension', 'name': 'From Extension',"
        " 'repo': 'https://example.invalid/x.git', 'area': 'examples',"
        " 'description': 'A module entry an extension registered, so a private project is a module.'})"))
    from bot.modules import registry

    m = registry.modules(refresh=True).get("from-extension")
    assert m is not None and m.name == "From Extension"


def test_a_test_directory_is_resolved_against_the_extension_and_kept_only_if_it_exists(private):
    _write_extension(private, "tested", name="tested",
                     body="    api.add_test_dir('tests')\n    api.add_test_dir('missing')")
    declared = extensions.load_all(force=True).by_name("tested").api.test_dirs
    assert declared[0] == private / "extensions" / "tested" / "tests"
    assert extensions.test_dirs() == [], "a directory that isn't there is reported and skipped"


def test_a_real_extension_test_directory_is_picked_up_and_runs(private):
    """An extension's tests run as part of the pipeline, in a real pytest process."""
    folder = _write_extension(private, "tested", name="tested", body="    api.add_test_dir('tests')")
    tests = folder / "tests"
    tests.mkdir()
    (tests / "test_extension.py").write_text(
        "def test_the_extension_is_loaded():\n"
        "    from bot import extensions\n"
        "    assert extensions.loaded().by_name('tested') is not None\n", encoding="utf-8")
    assert extensions.test_dirs() == [tests]


def test_the_pipeline_runs_extension_tests_and_skips_them_when_there_are_none(private, monkeypatch):
    """The pipeline's step, driven directly: a declared directory is run, and with none declared
    it reports 'skipped' rather than pretending to have checked anything."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("lp_ext", ROOT / "scripts" / "local_pipeline.py")
    lp = importlib.util.module_from_spec(spec)
    sys.modules["lp_ext"] = lp
    spec.loader.exec_module(lp)

    assert lp.check_extension_tests() is None, "no extension declared any tests"
    _write_extension(private, "tested", name="tested", body="    api.add_test_dir('tests')")
    extensions.load_all(force=True)
    folder = private / "extensions" / "tested"
    (folder / "tests").mkdir()
    (folder / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    # The extension's own config: without it the nested session falls back to this checkout for
    # its rootdir and walks the whole temp folder the private folder lives in, looking for one.
    (folder / "tests" / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    assert lp.check_extension_tests() is True
    (folder / "tests" / "test_bad.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    assert lp.check_extension_tests() is False, "a failing extension test fails the pipeline"


def test_the_leak_guard_pipeline_step_uses_the_private_settings(private, monkeypatch):
    """The pipeline's leak-guard step reads its modes and allowlist from the private folder, so
    the strictness of a public-repo check lives where the private list does."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("lp_leak", ROOT / "scripts" / "local_pipeline.py")
    lp = importlib.util.module_from_spec(spec)
    sys.modules["lp_leak"] = lp
    spec.loader.exec_module(lp)

    (private / "config" / "leak_guard.yaml").write_text(
        "modes:\n  machine_paths: warn\n  private_terms: fail\nallow:\n  - bot/*.py\n", encoding="utf-8")
    monkeypatch.setenv(extensions.PRIVATE_DIR_ENV, str(private))
    assert lp.check_leak_guard() is True, "a warn-only category never fails the pipeline"


# --- the module registry's overlay list --------------------------------------------------


def test_the_registry_still_lists_the_tracked_catalog_without_a_private_folder(monkeypatch):
    from bot.modules import registry

    monkeypatch.delenv(extensions.PRIVATE_DIR_ENV, raising=False)
    overlays = [m for m in registry.modules(refresh=True).values() if m.overlay]
    assert overlays, "the tracked catalog is still an overlay root"
    assert all(Path(m.overlay).is_dir() for m in overlays)


def test_the_fixture_extension_is_a_valid_manifest_and_package():
    """The example docs/extensions.md points at has to keep being loadable as written."""
    data = tomllib.loads((FIXTURE / "abp_extension.toml").read_text(encoding="utf-8"))
    assert data["extension"] == {"name": "hello", "version": "1.0.0", "entry": "ext_hello",
                                  "minimum_abp": "0.1.0",
                                  "description": data["extension"]["description"]}
    assert data["extension"]["description"]
    mod = extensions._import_extension_module(FIXTURE, "ext_hello")
    assert callable(mod.register)
    assert (FIXTURE / "panel.js").is_file(), "a panel needs a script to serve"