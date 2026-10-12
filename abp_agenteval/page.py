"""A static results page from eval reports (roadmap P9): `python -m abp_agenteval page --out docs/benchmarks/index.html report.json ...`

The page shows every report it is given, side by side, and says plainly what each one *is*:

* a **scripted** report replays golden trajectories, so it verifies that the tools, permissions, security defences and the grading
  work - it says nothing about how good any model is;
* a **live** report ran a real model on the same tasks and is the only kind that compares models. There must be live reports
  from more than one model before the page is a comparison, and it says when there is not.

Nothing on the page compares ABP with Claude Code, OpenCode or any other product: those were not run here. Each report keeps
its date and its task list, and a task a model failed is listed with the reason, not hidden in a percentage."""
from __future__ import annotations

import html
import json
import sys
import time
from pathlib import Path

CSS = """
:root { --bg:#fff; --fg:#1b1b1b; --muted:#666; --line:#ddd; --ok:#1a7f37; --bad:#b42318; --warn:#9a6700; }
@media (prefers-color-scheme: dark) { :root { --bg:#111; --fg:#e6e6e6; --muted:#9a9a9a; --line:#333; --ok:#4ac26b; --bad:#f97066; --warn:#e3b341; } }
body { background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; max-width:1000px; margin:2rem auto; padding:0 16px; }
h1 { font-size:1.5rem } h2 { font-size:1.15rem; margin-top:2rem } table { border-collapse:collapse; width:100%; font-size:.9rem }
th, td { border-bottom:1px solid var(--line); padding:.35rem .5rem; text-align:left; vertical-align:top } th { color:var(--muted); font-weight:600 }
.ok { color:var(--ok) } .bad { color:var(--bad) } .warn { color:var(--warn) } .muted { color:var(--muted) } .note { border-left:3px solid var(--warn); padding:.2rem .8rem; margin:1rem 0 }
"""


def _esc(v) -> str:
    return html.escape(str(v))


def _kind(report: dict) -> str:
    return "live" if report.get("mode") == "live" else "scripted"


def render_html(reports: list[dict], *, title: str = "ABP agent evaluation results") -> str:
    live_models = sorted({r["model"] for r in reports if _kind(r) == "live"})
    out = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>{_esc(title)}</title><style>{CSS}</style></head><body><h1>{_esc(title)}</h1>"]
    if len(live_models) < 2:
        out.append("<div class=note><strong>This is not a model comparison.</strong> "
                   + ("No live run is included." if not live_models else f"Only one model ({_esc(live_models[0])}) has a live run.")
                   + " Scripted results verify ABP's tools, permission checks, security defences and grading; they say nothing about how good any model "
                     "is. Nothing here compares ABP with Claude Code, OpenCode, Hermes, OpenClaw or any other product: they were not run.</div>")
    out.append("<h2>Summary</h2><table><tr><th>Run</th><th>Mode</th><th>Model</th><th>Passed</th><th>Score</th><th>Tokens</th><th>When</th></tr>")
    for i, r in enumerate(reports, 1):
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(r.get("when", 0))) if r.get("when") else ""
        # A task the provider's own limit stopped measured the limit, not the model: it is left out of
        # the denominator and says so here, rather than reading as a model that could not do the work.
        measured = r.get("measured", r["total"])
        skipped = (f"<div class=muted>{r['limited']} not measured</div>" if r.get("limited") else "")
        out.append(f"<tr><td>{i}</td><td class={'ok' if _kind(r) == 'live' else 'muted'}>{_kind(r)}</td><td>{_esc(r['model'])}</td>"
                   f"<td>{r['passed']}/{measured}{skipped}</td><td>{r['score']}%</td><td>{r['tokens']}</td><td>{_esc(when)}</td></tr>")
    out.append("</table>")
    ids = sorted({x["id"] for r in reports for x in r["results"]})
    titles = {x["id"]: (x.get("title", ""), x.get("category", "")) for r in reports for x in r["results"]}
    out.append("<h2>Tasks</h2><table><tr><th>Task</th><th>Category</th>" + "".join(f"<th>Run {i}</th>" for i in range(1, len(reports) + 1)) + "</tr>")
    for tid in ids:
        cells = []
        for r in reports:
            hit = next((x for x in r["results"] if x["id"] == tid), None)
            cells.append("<td class=muted>-</td>" if hit is None else
                         f"<td class={'muted' if hit.get('limited') else ('ok' if hit['passed'] else 'bad')}>"
                         f"{'skip' if hit.get('limited') else ('pass' if hit['passed'] else 'FAIL')}</td>")
        out.append(f"<tr><td>{_esc(tid)}<div class=muted>{_esc(titles[tid][0])}</div></td><td>{_esc(titles[tid][1])}</td>{''.join(cells)}</tr>")
    out.append("</table>")
    failures = [(i, x) for i, r in enumerate(reports, 1) for x in r["results"] if not x["passed"] and not x.get("limited")]
    if failures:
        out.append("<h2>Failures</h2><table><tr><th>Run</th><th>Task</th><th>Why</th></tr>")
        for i, x in failures:
            why = "; ".join(f"{c['name']}: {c['detail']}" for c in x.get("checks", []) if not c["ok"]) or x.get("error", "")
            out.append(f"<tr><td>{i}</td><td>{_esc(x['id'])}</td><td>{_esc(why)}</td></tr>")
        out.append("</table>")
    out.append("<p class=muted>Generated by <code>python -m abp_agenteval page</code>. Each task's graders check files, the reply and the recorded tool calls; "
               "see docs/agents/evals.md.</p></body></html>")
    return "".join(out)


def render_bench_html(summary: dict, reports: list[dict], *, title: str = "ABP agent benchmark") -> str:
    """The leaderboard of a `bench run` folder: every model on the same suite, repeated, with how the numbers were made."""
    head = (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(title)}</title><style>{CSS}</style></head><body><h1>{_esc(title)}</h1>")
    if not summary["models"]:
        return head + "<p>No complete benchmark runs yet.</p></body></html>"
    rows = summary["models"]
    span = f"{time.strftime('%Y-%m-%d', time.gmtime(summary['from']))} to {time.strftime('%Y-%m-%d', time.gmtime(summary['to']))}"
    out = [head, "<p>ABP's own agent, running the same tasks with each model. A task passes only if every one of its checks "
                 "passes: the files it left, what it said, and the tools it did and did not use.</p>"]
    if len(rows) < 2:
        out.append("<div class=note><strong>Only one model so far</strong>, so this is not yet a comparison.</div>")
    out.append("<h2>Leaderboard</h2><table><tr><th>Model</th><th>Mean score</th><th>Range</th><th>Reliable</th><th>Passed at least once</th>"
               "<th>Runs</th><th>Tokens per run</th><th>Minutes per run</th></tr>")
    for r in rows:
        out.append(f"<tr><td>{_esc(r['model'])}</td><td><strong>{r['mean']}%</strong></td><td>{r['min']}-{r['max']}%"
                   + (f" <span class=muted>(sd {r['stdev']})</span>" if r["stdev"] is not None else "") + "</td>"
                   f"<td>{r['reliable']}/{r['tasks']}</td><td>{r['ever']}/{r['tasks']}</td><td>{r['runs']}</td>"
                   f"<td>{r['tokens']:,}</td><td>{r['minutes']}</td></tr>")
    out.append("</table><p class=muted><strong>Reliable</strong>: passed in every run. A model's mean can hide a task it passes only "
               "sometimes; those are listed under each model below.</p>")
    cats = sorted({c for r in rows for c in r["categories"]})
    out.append("<h2>By category</h2><table><tr><th>Model</th>" + "".join(f"<th>{_esc(c)}</th>" for c in cats) + "</tr>")
    for r in rows:
        out.append(f"<tr><td>{_esc(r['model'])}</td>" + "".join(
            f"<td>{r['categories'][c]}%</td>" if c in r["categories"] else "<td class=muted>-</td>" for c in cats) + "</tr>")
    out.append("</table>")
    flaky = [(r["model"], r["flaky"]) for r in rows if r["flaky"]]
    if flaky:
        out.append("<h2>Passed only sometimes</h2><table><tr><th>Model</th><th>Tasks</th></tr>")
        out += [f"<tr><td>{_esc(m)}</td><td>{_esc(', '.join(ids))}</td></tr>" for m, ids in flaky]
        out.append("</table>")
    left_out = f" ({summary['left_out']} left out)" if summary["left_out"] else ""
    cut_short = f" ({summary['incomplete']} such run(s) here)" if summary["incomplete"] else ""
    out.append(f"<h2>How these numbers were made</h2><ul>"
               f"<li>Suite <code>{_esc(summary['fingerprint'])}</code>: a fingerprint of the tasks, prompts, fixtures and graders. Runs of "
               f"another suite are never averaged in{left_out}.</li>"
               f"<li>ABP {_esc(summary['abp_version'])}, commit <code>{_esc(summary['commit'] or 'unknown')}</code>; runs from {_esc(span)}.</li>"
               f"<li>Approvals are answered automatically, except where a task tests that a denial holds. Auto-checkpoints are off.</li>"
               f"<li>A run cut short by a model's rate limit or free allowance does not count{cut_short}.</li>"
               "<li>This measures ABP's agent with each model on ABP's own tasks. No other product (Claude Code, OpenCode, Hermes, "
               "OpenClaw) was run, so nothing here compares ABP with them.</li></ul>"
               "<p class=muted>Generated by <code>python -m abp_agenteval page --bench</code>; the run files beside this page hold every "
               "check of every task. See docs/agents/evals.md.</p></body></html>")
    return "".join(out)


def classify(data) -> str:
    """'run' for a report of one suite run, 'compare' for a `compare` report, '' for anything else."""
    if not isinstance(data, dict):
        return ""
    if isinstance(data.get("results"), list):
        return "run"
    if isinstance(data.get("variants"), list):
        return "compare"
    return ""


def summarize_run(r: dict) -> dict:
    """The numbers a reader wants from one run report, tolerant of missing keys."""
    results = [x for x in r.get("results", []) if isinstance(x, dict)]
    failed = [x for x in results if not x.get("passed") and not x.get("limited")]
    cats: dict[str, list[int]] = {}
    for x in results:
        if x.get("limited"):
            continue
        c = cats.setdefault(str(x.get("category", "")), [0, 0])
        c[1] += 1
        c[0] += bool(x.get("passed"))
    total = r.get("total", len(results))
    return {
        "mode": r.get("mode", ""), "model": r.get("model", ""), "when": r.get("when") or 0,
        "total": total, "measured": r.get("measured", total), "limited": r.get("limited", 0),
        "passed": r.get("passed", 0), "score": r.get("score", 0), "tokens": r.get("tokens", 0),
        "duration_ms": r.get("duration_ms", 0),
        "model_calls": sum(int(x.get("iterations") or 0) for x in results),
        "categories": {k: {"passed": v[0], "total": v[1]} for k, v in sorted(cats.items())},
        "failed": [{"id": str(x.get("id", "")), "title": str(x.get("title", "")), "category": str(x.get("category", ""))} for x in failed],
        "tasks_failed": len(failed),
    }


def summarize_compare(c: dict) -> dict:
    keys = ("name", "passed", "total", "score", "tokens", "iterations", "gained", "lost", "verdict")
    return {"mode": c.get("mode", ""), "model": c.get("model", ""), "note": c.get("note"),
            "variants": [{k: v.get(k) for k in keys} for v in c.get("variants", []) if isinstance(v, dict)]}


def load_reports(folder) -> list[tuple[str, dict]]:
    """Every readable *.json report in `folder`, as (file name, data), sorted by name. Unreadable files are skipped."""
    out = []
    try:
        files = sorted(Path(folder).glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            out.append((f.name, json.loads(f.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return out


def _when(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(float(ts))) if ts else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _secs(ms) -> str:
    try:
        s = float(ms or 0) / 1000
    except (TypeError, ValueError):
        return ""
    return f"{int(s // 60)} m {int(s % 60):02d} s" if s >= 60 else f"{s:.1f} s"


def render_reports_html(named: list[tuple[str, dict]], *, title: str = "ABP live evaluation reports", body_only: bool = False) -> str:
    """Per-run summaries and a cross-run table for run reports, plus any `compare` reports. Everything is escaped."""
    runs = [(n, summarize_run(d)) for n, d in named if classify(d) == "run"]
    comps = [(n, summarize_compare(d)) for n, d in named if classify(d) == "compare"]
    runs.sort(key=lambda x: (str(x[1]["when"]), x[0]))
    out = []
    if not runs and not comps:
        out.append("<p>No eval reports found.</p>")
    if runs:
        out.append("<h2>All runs</h2><table><tr><th>Report</th><th>Mode</th><th>Model</th><th>Date</th><th>Pass rate</th><th>Passed</th>"
                   "<th>Failed</th><th>Model calls</th><th>Tokens</th><th>Wall time</th></tr>")
        for n, s in runs:
            out.append(f"<tr><td>{_esc(n)}</td><td class={'ok' if s['mode'] == 'live' else 'muted'}>{_esc(s['mode'])}</td><td>{_esc(s['model'])}</td>"
                       f"<td>{_esc(_when(s['when']))}</td><td>{_esc(s['score'])}%</td><td>{_esc(s['passed'])}/{_esc(s['measured'])}</td>"
                       f"<td>{s['tasks_failed']}</td><td>{s['model_calls']}</td><td>{_esc(s['tokens'])}</td><td>{_esc(_secs(s['duration_ms']))}</td></tr>")
        out.append("</table>")
    for n, s in runs:
        out.append(f"<h2>{_esc(s['model'])} <span class=muted>{_esc(_when(s['when']))} - {_esc(n)}</span></h2>"
                   f"<p>{_esc(s['mode'])} run: <strong>{_esc(s['passed'])}/{_esc(s['measured'])}</strong> tasks passed ({_esc(s['score'])}%), "
                   f"{s['model_calls']} model calls, {_esc(s['tokens'])} tokens, {_esc(_secs(s['duration_ms']))}."
                   + (f" {_esc(s['limited'])} task(s) not measured (allowance ran out)." if s["limited"] else "") + "</p>")
        if s["categories"]:
            out.append("<table><tr><th>Category</th><th>Passed</th></tr>" + "".join(
                f"<tr><td>{_esc(k)}</td><td>{v['passed']}/{v['total']}</td></tr>" for k, v in s["categories"].items()) + "</table>")
        if s["failed"]:
            out.append("<p class=bad>Failed tasks</p><table><tr><th>Task</th><th>Category</th></tr>" + "".join(
                f"<tr><td>{_esc(x['id'])}<div class=muted>{_esc(x['title'])}</div></td><td>{_esc(x['category'])}</td></tr>" for x in s["failed"]) + "</table>")
    for n, c in comps:
        out.append(f"<h2>Prompt comparison <span class=muted>{_esc(n)}</span></h2><p>{_esc(c['mode'])} - {_esc(c['model'])}"
                   + (f" - {_esc(c['note'])}" if c["note"] else "") + "</p><table><tr><th>Variant</th><th>Passed</th><th>Score</th><th>Tokens</th>"
                   "<th>Model calls</th><th>Gained</th><th>Lost</th><th>Verdict</th></tr>")
        for v in c["variants"]:
            out.append(f"<tr><td>{_esc(v['name'])}</td><td>{_esc(v['passed'])}/{_esc(v['total'])}</td><td>{_esc(v['score'])}%</td><td>{_esc(v['tokens'])}</td>"
                       f"<td>{_esc(v['iterations'])}</td><td>{_esc(', '.join(map(str, v['gained'] or [])))}</td>"
                       f"<td>{_esc(', '.join(map(str, v['lost'] or [])))}</td><td>{_esc(v['verdict'])}</td></tr>")
        out.append("</table>")
    body = "".join(out)
    if body_only:
        return body
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(title)}</title><style>{CSS}</style></head><body><h1>{_esc(title)}</h1>{body}"
            "<p class=muted>Generated by <code>python -m abp_agenteval page --reports</code>. See docs/agents/evals.md.</p></body></html>")


def _main_reports(paths: list[str], out: str, bench_dir: str | None, reports_dir: str) -> int:
    named = load_reports(reports_dir)
    for p in paths:
        try:
            named.append((Path(p).name, json.loads(Path(p).read_text(encoding="utf-8"))))
        except (OSError, ValueError) as exc:
            print(f"could not read {p}: {exc}", file=sys.stderr)
            return 2
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    if bench_dir:
        from . import bench

        runs = bench.load(Path(bench_dir))
        page = render_bench_html(bench.summarize(runs), runs)
        text = page.replace("</body></html>", render_reports_html(named, body_only=True) + "</body></html>")
    else:
        text = render_reports_html(named)
    Path(out).write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    return 0


def main(paths: list[str], out: str, *, bench_dir: str | None = None, reports_dir: str | None = None) -> int:
    if reports_dir:
        return _main_reports(paths, out, bench_dir, reports_dir)
    reports = []
    for p in paths:
        try:
            reports.append(json.loads(Path(p).read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"could not read {p}: {exc}", file=sys.stderr)
            return 2
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    if bench_dir:
        from . import bench

        runs = bench.load(Path(bench_dir))
        Path(out).write_text(render_bench_html(bench.summarize(runs), runs), encoding="utf-8")
        print(f"wrote {out}")
        return 0
    if not reports:
        print("give at least one report (a --out file from `abp_agenteval run`), or --bench DIR", file=sys.stderr)
        return 2
    Path(out).write_text(render_html(reports), encoding="utf-8")
    print(f"wrote {out}")
    return 0
