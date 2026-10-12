"""Agent eval reports API (roadmap: Evals): the Evals screen's read-only routes over the harness's JSON reports.

    GET /api/agent/evals/reports           every *.json report in the report folders, newest first: kind (run / compare) and a summary
    GET /api/agent/evals/reports/{name}    one report's summary; for a run also each failed task with the checks that failed

Report folders: `docs/agents/eval-reports` in the install, plus any folder named in ABP_EVAL_REPORTS_DIR (separated with the
OS path separator). `name` must be a plain file name from the listing: nothing with a separator, a drive or a leading dot is
looked up, and only *.json files directly inside those folders are ever read. Nothing is written.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,150}\.json$")
_MAX_BYTES = 20_000_000


def report_dirs() -> list[Path]:
    from bot import envfile

    dirs = [Path(envfile.PROJECT_ROOT) / "docs" / "agents" / "eval-reports"]
    dirs += [Path(p) for p in os.environ.get("ABP_EVAL_REPORTS_DIR", "").split(os.pathsep) if p.strip()]
    return dirs


def _files() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for d in report_dirs():
        try:
            for f in sorted(d.glob("*.json")):
                if _NAME.match(f.name) and f.is_file() and f.name not in out:
                    out[f.name] = f
        except OSError:
            continue
    return out


def _read(path: Path):
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _summary(name: str, data) -> dict | None:
    from abp_agenteval import page

    kind = page.classify(data)
    if kind == "run":
        return {"name": name, "kind": "run", **page.summarize_run(data)}
    if kind == "compare":
        return {"name": name, "kind": "compare", **page.summarize_compare(data)}
    return None


def register(app: FastAPI, read_auth: Callable) -> None:
    read = [Depends(read_auth)]

    @app.get("/api/agent/evals/reports", dependencies=read)
    async def list_eval_reports():
        items = []
        for name, path in _files().items():
            s = _summary(name, _read(path))
            if s:
                s.pop("failed", None)
                items.append(s)
        items.sort(key=lambda s: (s.get("when") or 0, s["name"]), reverse=True)
        return {"reports": items, "total": len(items)}

    @app.get("/api/agent/evals/reports/{name}", dependencies=read)
    async def get_eval_report(name: str):
        path = _files().get(name) if _NAME.match(name) else None
        if path is None:
            raise HTTPException(404, "No such report")
        data = _read(path)
        s = _summary(name, data)
        if s is None:
            raise HTTPException(404, "No such report")
        if s["kind"] == "run":
            why = {}
            for x in data.get("results", []):
                if isinstance(x, dict) and not x.get("passed") and not x.get("limited"):
                    why[str(x.get("id", ""))] = "; ".join(
                        f"{c.get('name')}: {c.get('detail')}" for c in x.get("checks", []) if isinstance(c, dict) and not c.get("ok")
                    ) or str(x.get("error") or "")
            for f in s["failed"]:
                f["why"] = why.get(f["id"], "")
        return s
