"""Build and test caches (Local Factory LF8, throughput part 1), backed by CacheIt.

    keys = TestKeys(root, python)                      # content keys: a test file + everything it imports + the environment
    tc = TestCache.from_settings()
    cached, run, kmap = tc.partition(keys, files)      # files whose exact inputs already passed / files that must run
    ... run pytest on `run` ...
    if rc == 0: tc.record_passes(kmap, run, seconds)   # only a PASS is ever stored

    wheelhouse(lock, python)  -> a local folder of wheels for a lock file (wheels kept in CacheIt, content addressed)
    venv_for(repo)            -> a ready venv for a worker worktree, built once per (lock files, interpreter)
    cargo_env(crate_dir)      -> CARGO_TARGET_DIR for a crate, one target folder per exact content (maturin builds too)

What a key holds. A test file's key hashes the file and every Python file it reaches through imports (testselect's import graph,
walked to the end, not depth-limited), plus an environment hash: the interpreter (version, platform), every installed
distribution and its version, the test infrastructure (conftest.py, pytest.ini, pyproject.toml, setup.cfg, requirements*,
tests/fixtures, tests/data, config/), every non-Python file under the indexed roots, and the ABP_/PYTEST_/PYTHON* environment.
Change any of these and the key changes, so a miss: the file runs again.

What a cache may never do: hide a failure. Only passes are stored; a failure or a flaky file stores nothing; a test that reaches
code the import graph cannot see (a module loaded by a computed name) is the one gap, and that is why every deploy runs the
whole suite with no cache lookups at all (scripts/local_pipeline.py: lookups only on a selected, non-deploy run). A full run
still records its passes, which is what makes the next selected or Factory run fast.

CacheIt is reached through bot/conductor/cache.py's client (the `cacheit` module's hub); when it is down every lookup is a
miss. Bulky things that cannot move (a venv, a cargo target folder) live on disk under the conductor's state folder; CacheIt
holds their index and the relocatable bytes (wheels).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from bot.conductor import cache as cachelib
from bot.conductor import testselect

NS_TESTS = "conductor.tests"
NS_BUILD = "conductor.build"
TEST_TTL_S = 14 * 24 * 3600
BUILD_TTL_S = 30 * 24 * 3600
SWITCHES = ("tests", "wheels", "venvs", "cargo")
DEFAULTS = {"tests": True, "wheels": True, "venvs": False, "cargo": True, "keep_targets": 6, "keep_venvs": 4}
_BIG = 5 << 20                     # a non-Python file bigger than this is keyed by size and mtime, not read
_ENV_PREFIXES = ("ABP_", "PYTEST", "PYTHON")
_ENV_IGNORED = ("ABP_PIPELINE_", "ABP_PYTEST_MAX_WORKERS", "PYTHONUNBUFFERED")   # knobs that change how, not what, runs
LOCK_NAMES = ("requirements.lock", "requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.cfg", "setup.py")


def settings(cfg: Any = None) -> dict[str, Any]:
    """`conductor.buildcache` over its defaults (cfg = the conductor settings dict, or None to read them)."""
    if cfg is None:
        try:
            from bot import conductor

            cfg = conductor.settings()
        except Exception:  # noqa: BLE001 - outside ABP (the pipeline before config loads): the defaults
            cfg = {}
    own = cfg.get("buildcache") if isinstance(cfg, dict) else None
    return {**DEFAULTS, **(own if isinstance(own, dict) else {})}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cache_root() -> Path:
    from bot.agent_runtime.state import state_dir

    return state_dir("conductor", "buildcache")


# ------------------------------------------------------------------ stats
_STATS: dict[str, dict[str, float]] = {}


def _count(kind: str, field: str, n: float = 1) -> None:
    s = _STATS.setdefault(kind, {"hits": 0, "misses": 0, "stores": 0, "seconds_saved": 0.0})
    s[field] = s.get(field, 0) + n


def stats() -> dict[str, dict[str, float]]:
    """Hits, misses, stores and seconds saved per kind since this process started."""
    return {k: dict(v) for k, v in _STATS.items()}


# ------------------------------------------------------------- test keys
def _interpreter_facts(python: str) -> str:
    code = ("import sys, platform, importlib.metadata as m; print(sys.version); print(platform.platform()); "
            "print('\\n'.join(sorted({(d.metadata['Name'] or '').lower() + '==' + d.version for d in m.distributions()})))")
    try:
        proc = subprocess.run([python, "-c", code], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else ""


def _file_digest(p: Path) -> str:
    try:
        st = p.stat()
        if st.st_size > _BIG:
            return f"big:{st.st_size}:{st.st_mtime_ns}"
        return _sha(p.read_bytes())
    except OSError:
        return "missing"


def env_inputs(root: Path, indexed: Iterable[str] = ()) -> list[str]:
    """Every file of the checkout a test may depend on outside its import closure, sorted: all of git's tracked and untracked
    (not ignored) files except the `indexed` Python files the closure covers. That takes in test infrastructure, lock files,
    config, data, and Python code the import graph does not index (a package outside bot/, scripts/, tests/). Outside git: a
    walk of the tree without its build/cache folders."""
    root = Path(root)
    skip = {r for r in indexed if r.rsplit("/", 1)[-1] not in testselect.FULL_NAMES}   # conftest.py shapes every test
    try:
        proc = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"], capture_output=True, timeout=120)
        if proc.returncode != 0:
            raise OSError("not a git checkout")
        rels = [r for r in proc.stdout.decode("utf-8", "replace").split("\0") if r]
    except (OSError, subprocess.SubprocessError):
        rels = []
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            pre = "" if rel_dir == "." else rel_dir + "/"
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"
                           and (d not in testselect.SKIP_DIRS or (pre + d + "/").startswith(testselect.FULL_PREFIXES))]
            rels += [pre + f for f in filenames if not f.endswith(".pyc")]
    return sorted(r for r in rels if r not in skip and "__pycache__/" not in r)


class TestKeys:
    """Content keys for test files of one checkout, run by one interpreter."""

    __test__ = False                       # not a pytest class

    def __init__(self, root: Path, python: str = sys.executable, graph: Optional[testselect.Graph] = None):
        self.root = Path(root)
        self.python = python
        self.graph = graph or testselect.Graph(self.root)
        self._digests: dict[str, str] = {}
        self._env = ""

    def env_hash(self) -> str:
        if not self._env:
            facts = _interpreter_facts(self.python)
            h = hashlib.sha256(facts.encode())
            for rel in env_inputs(self.root, self.graph.files):
                h.update(f"\0{rel}\0{_file_digest(self.root / rel)}".encode())
            for k in sorted(os.environ):
                if k.startswith(_ENV_PREFIXES) and not k.startswith(_ENV_IGNORED):
                    h.update(f"\0{k}={os.environ[k]}".encode())
            # no facts = the interpreter could not be asked: a key that never matches, so nothing is reused
            self._env = h.hexdigest() if facts else "unknown:" + str(time.time_ns())
        return self._env

    def closure(self, rel: str) -> list[str]:
        """`rel` and every indexed file it reaches through imports, with no depth limit."""
        seen, stack = {rel}, [rel]
        while stack:
            for d in self.graph.deps.get(stack.pop(), ()):
                if d not in seen:
                    seen.add(d)
                    stack.append(d)
        return sorted(seen)

    def _digest(self, rel: str) -> str:
        d = self._digests.get(rel)
        if d is None:
            text = self.graph.files.get(rel)
            d = self._digests[rel] = _sha(text.encode("utf-8", "replace")) if text is not None else _file_digest(self.root / rel)
        return d

    def key(self, rel: str) -> str:
        h = hashlib.sha256(self.env_hash().encode())
        for f in self.closure(rel):
            h.update(f"\0{f}\0{self._digest(f)}".encode())
        return h.hexdigest()


class TestCache:
    """Per-test-file pass records in CacheIt (namespace conductor.tests). `cache` is injectable for tests."""

    __test__ = False

    def __init__(self, enabled: bool = True, cache: Optional[cachelib.Cache] = None):
        self.enabled = bool(enabled)
        self.cache = cache if cache is not None else cachelib.Cache(enabled=self.enabled)

    @classmethod
    def from_settings(cls, cfg: Any = None) -> "TestCache":
        return cls(enabled=bool(settings(cfg)["tests"]))

    def partition(self, keys: TestKeys, files: Iterable[str]) -> tuple[list[str], list[str], dict[str, str]]:
        """(cached, to_run, file -> key): a file is cached only when its exact key already passed."""
        files = list(dict.fromkeys(files))
        if not self.enabled:
            return [], files, {}
        kmap: dict[str, str] = {}
        cached, run = [], []
        for f in files:
            try:
                kmap[f] = keys.key(f)
            except Exception:  # noqa: BLE001 - a file that cannot be keyed always runs
                run.append(f)
                continue
            rec = self.cache.get_json(NS_TESTS, kmap[f])
            if rec and rec.get("passed") is True:
                cached.append(f)
                _count("tests", "hits")
                _count("tests", "seconds_saved", float(rec.get("seconds") or 0))
            else:
                run.append(f)
                _count("tests", "misses")
        return cached, run, kmap

    def record_passes(self, kmap: dict[str, str], files: Sequence[str], seconds: float = 0.0,
                      exclude: Iterable[str] = ()) -> int:
        """Store a pass for each of `files` (call only after a run of exactly those files exited 0). `exclude`: files with a
        flaky test, which are never recorded. Returns how many were stored."""
        if not self.enabled or not files:
            return 0
        skip = {e.split("::", 1)[0] for e in exclude}
        each = round(seconds / max(1, len(files)), 2)
        n = 0
        for f in files:
            k = kmap.get(f)
            if not k or f in skip:
                continue
            if self.cache.put_json(NS_TESTS, k, {"passed": True, "file": f, "seconds": each, "at": time.time()}, TEST_TTL_S):
                n += 1
        _count("tests", "stores", n)
        return n


_DESELECT = ("-k", "-m", "--deselect", "--lf", "--last-failed", "--ff", "--sw", "--stepwise", "--co", "--collect-only")


def whole_files(cmd: Sequence[str]) -> bool:
    """True when a pytest command runs every test of the files it names (nothing deselected), so its exit 0 is a pass per file."""
    cmd = list(cmd)
    at = next((i for i, a in enumerate(cmd) if Path(a).stem == "pytest" or a == "pytest"), None)
    if at is None:
        return False                       # not a pytest command we can read
    cmd = cmd[at + 1:]
    return not any(a in _DESELECT or a.startswith(("-k=", "-m=", "--deselect=")) or "::" in a for a in cmd)


def narrow(cmd: Sequence[str], files: Sequence[str], keep: Sequence[str], target: str = "tests") -> list[str]:
    """`cmd` running only `keep` instead of `files` (its file arguments, or its `tests` folder argument)."""
    cmd = list(cmd)
    fs = set(files)
    idx = [i for i, a in enumerate(cmd) if a in fs] or ([cmd.index(target)] if target in cmd else [])
    if not idx:
        return [*cmd, *keep]
    drop = set(idx)
    base = [a for j, a in enumerate(cmd) if j not in drop]
    return [*base[:idx[0]], *keep, *base[idx[0]:]]


# ----------------------------------------------------------------- wheels
def lock_key(files: Sequence[Path], python: str = sys.executable) -> str:
    facts = _interpreter_facts(python).splitlines()[:2]          # version and platform; the packages are what gets built
    h = hashlib.sha256("\n".join(facts).encode())
    for p in files:
        h.update(f"\0{Path(p).name}\0{_file_digest(Path(p))}".encode())
    return h.hexdigest()


def wheelhouse(lock: Path, python: str = sys.executable, *, cache: Optional[cachelib.Cache] = None,
               root: Optional[Path] = None, build: bool = True) -> Optional[Path]:
    """A folder holding a wheel for every requirement of `lock`, for `python`. Kept locally and in CacheIt (each wheel content
    addressed, plus a manifest per lock key), so a fresh machine or a cleaned folder refills from CacheIt without pip.
    None when there is no such wheelhouse and `build` is false or `pip wheel` fails."""
    cache = cache or cachelib.Cache()
    key = lock_key([lock], python)
    folder = Path(root or cache_root()) / "wheels" / key[:24]
    done = folder / ".abp-complete"
    if done.is_file():
        _count("wheels", "hits")
        return folder
    manifest = cache.get_json(NS_BUILD, "wheels:" + key)
    if manifest and isinstance(manifest.get("wheels"), list):
        folder.mkdir(parents=True, exist_ok=True)
        ok = True
        for w in manifest["wheels"]:
            data = cache.get_bytes(NS_BUILD, str(w.get("cas") or ""))
            if data is None or _sha(data) != w.get("sha256"):
                ok = False
                break
            (folder / Path(str(w["name"])).name).write_bytes(data)
        if ok:
            done.write_text(key, encoding="utf-8")
            _count("wheels", "hits")
            return folder
    _count("wheels", "misses")
    if not build:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    proc = subprocess.run([python, "-m", "pip", "wheel", "-q", "-r", str(lock), "-w", str(folder)], capture_output=True, timeout=3600)
    if proc.returncode != 0:
        return None
    wheels = []
    for p in sorted(folder.glob("*.whl")):
        data = p.read_bytes()
        cas = cache.put_cas(NS_BUILD, data, BUILD_TTL_S)
        if cas:
            wheels.append({"name": p.name, "cas": cas, "sha256": _sha(data)})
    if wheels:
        cache.put_json(NS_BUILD, "wheels:" + key, {"wheels": wheels, "seconds": round(time.time() - t0, 1)}, BUILD_TTL_S)
        _count("wheels", "stores", len(wheels))
    done.write_text(key, encoding="utf-8")
    return folder


# ------------------------------------------------------------------ venvs
def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _prune(parent: Path, keep: int) -> None:
    """Keep the `keep` most recently used folders in `parent` (used = their .abp-used stamp)."""
    if not parent.is_dir():
        return
    dirs = sorted((d for d in parent.iterdir() if d.is_dir()), key=lambda d: _file_mtime(d / ".abp-used"), reverse=True)
    for d in dirs[max(1, keep):]:
        shutil.rmtree(d, ignore_errors=True)


def _file_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _touch(p: Path) -> None:
    try:
        p.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def venv_for(repo: Path, python: str = sys.executable, *, cfg: Any = None, cache: Optional[cachelib.Cache] = None,
             root: Optional[Path] = None) -> Optional[str]:
    """The interpreter of a ready venv for `repo`'s requirements (requirements.lock, else requirements.txt, plus
    requirements-dev.txt), built once per (those files, interpreter) from the wheelhouse and reused by every worker worktree
    after. None when venvs are off, the repo declares no requirements, or the build fails (the caller keeps its interpreter)."""
    s = settings(cfg)
    if not s["venvs"]:
        return None
    repo = Path(repo)
    locks = [repo / n for n in ("requirements.lock", "requirements.txt") if (repo / n).is_file()][:1]
    if not locks:
        return None
    dev = repo / "requirements-dev.txt"
    files = [*locks, *([dev] if dev.is_file() else [])]
    key = lock_key(files, python)
    base = Path(root or cache_root()) / "venvs"
    venv = base / key[:24]
    exe = _venv_python(venv)
    if (venv / ".abp-ready").is_file() and exe.is_file():
        _count("venvs", "hits")
        _touch(venv / ".abp-used")
        return str(exe)
    _count("venvs", "misses")
    shutil.rmtree(venv, ignore_errors=True)
    t0 = time.time()
    try:
        subprocess.run([python, "-m", "venv", str(venv)], check=True, capture_output=True, timeout=600)
        for req in files:
            house = wheelhouse(req, python, cache=cache, root=root) if s["wheels"] else None
            args = ["--no-index", "--find-links", str(house)] if house else []
            subprocess.run([str(exe), "-m", "pip", "install", "-q", *args, "-r", str(req)], check=True, capture_output=True,
                           timeout=3600)
        # a worker's tests and proof run with it: without pytest it is no use (the caller keeps ABP's runtime, which has it)
        subprocess.run([str(exe), "-c", "import pytest"], check=True, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(venv, ignore_errors=True)
        return None
    (venv / ".abp-ready").write_text(key, encoding="utf-8")
    _touch(venv / ".abp-used")
    (cache or cachelib.Cache()).put_json(NS_BUILD, "venv:" + key, {"path": str(venv), "seconds": round(time.time() - t0, 1)},
                                         BUILD_TTL_S)
    _count("venvs", "stores")
    _prune(base, int(s["keep_venvs"]))
    return str(exe)


# ------------------------------------------------------------------ cargo
def _rustc_version() -> str:
    try:
        proc = subprocess.run(["rustc", "-vV"], capture_output=True, timeout=30)
        return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def crate_key(crate_dir: Path, rustc: Optional[str] = None) -> str:
    """A hash of a crate's exact content (every tracked-looking file except target/), its Cargo.lock and the toolchain.

    Content, not mtimes: cargo's own freshness check compares mtimes, so a target folder shared between worktrees holding
    different sources could be wrongly reused. One folder per exact content cannot."""
    crate_dir = Path(crate_dir)
    h = hashlib.sha256((rustc if rustc is not None else _rustc_version()).encode())
    for dirpath, dirnames, filenames in os.walk(crate_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in ("target", ".git", "node_modules") and not d.startswith("."))
        for f in sorted(filenames):
            p = Path(dirpath) / f
            h.update(f"\0{p.relative_to(crate_dir).as_posix()}\0{_file_digest(p)}".encode())
    return h.hexdigest()


def cargo_env(crate_dir: Path, *, cfg: Any = None, root: Optional[Path] = None, rustc: Optional[str] = None) -> dict[str, str]:
    """{"CARGO_TARGET_DIR": a folder for this crate's exact content} to add to a cargo/maturin build's environment, or {} when
    the cargo cache is off or no toolchain answers. A hit is a folder that already holds a finished build."""
    s = settings(cfg)
    if not s["cargo"]:
        return {}
    rv = rustc if rustc is not None else _rustc_version()
    if not rv:
        return {}
    base = Path(root or cache_root()) / "cargo"
    target = base / crate_key(crate_dir, rv)[:24]
    _count("cargo", "hits" if (target / ".abp-used").is_file() else "misses")
    target.mkdir(parents=True, exist_ok=True)
    _touch(target / ".abp-used")
    _prune(base, int(s["keep_targets"]))
    return {"CARGO_TARGET_DIR": str(target)}
