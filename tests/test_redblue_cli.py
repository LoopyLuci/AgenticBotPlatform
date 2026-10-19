"""`abp_cli redblue run|coverage`: output shape, --json validity, exit codes, and that it stays propose-only."""
import json

import pytest

from abp_cli import redblue as cli
from abp_cli.__main__ import _parser, main
from bot.redblue import containment
from bot.redblue.journal import IncidentJournal
from bot.redblue.schemas import VectorClass
from bot.redblue.taxonomy import PROBES, VectorProbe
from bot.redblue.watchdog import WatchDog

_BLIND = VectorProbe("novel_unknown", VectorClass.AGENT_RUNTIME,
                     {"action": "memory_poison", "target": "context", "grant_result": "allowed"})


def _json(capsys, argv):
    code = main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_run_text_reports_tried_detected_missed_and_proposed_containment(capsys):
    assert main(["redblue", "run"]) == 0
    out = capsys.readouterr().out
    assert f"{len(PROBES)} tried, {len(PROBES)} detected, 0 missed" in out
    assert out.count("[PROPOSED, dry-run]") == len(PROBES)
    for p in PROBES:
        assert p.name in out


def test_run_json_is_valid_and_every_containment_is_proposed_dry_run(capsys):
    code, rep = _json(capsys, ["redblue", "run", "--json"])
    assert code == 0
    assert rep["total"] == len(PROBES) and rep["detected"] == len(PROBES) and rep["missed"] == []
    assert rep["coverage"] == 1.0
    assert [v["name"] for v in rep["vectors"]] == [p.name for p in PROBES]
    for v in rep["vectors"]:
        c = v["containment"]
        assert c["status"] == "PROPOSED" and c["dry_run"] is True and c["executed"] is False
        assert c["outcome"] == f"dry-run:{c['action']}"


def test_global_json_flag_also_works(capsys):
    code, rep = _json(capsys, ["--json", "redblue", "coverage"])
    assert code == 0 and rep["overall"]["coverage"] == 1.0


def test_coverage_table_lists_every_class_and_overall(capsys):
    assert main(["redblue", "coverage"]) == 0
    lines = capsys.readouterr().out.splitlines()
    names = [ln.split()[0] for ln in lines[1:]]
    assert names == [vc.value for vc in VectorClass] + ["overall"]
    assert lines[-1].split()[1:4] == [str(len(PROBES)), str(len(PROBES)), "100%"]


def test_coverage_json_counts_add_up_and_unprobed_class_is_unmeasured(capsys):
    code, rep = _json(capsys, ["redblue", "coverage", "--json"])
    assert code == 0
    cats = {c["vector_class"]: c for c in rep["categories"]}
    assert set(cats) == {vc.value for vc in VectorClass}
    assert sum(c["total"] for c in cats.values()) == rep["overall"]["total"] == len(PROBES)
    for vc, c in cats.items():
        if c["total"] == 0:
            assert c["coverage"] is None, vc          # no probe is "n/a", never a fake 100%
        else:
            assert c["coverage"] == 1.0, vc


def test_a_missed_vector_exits_1_and_is_listed(capsys, monkeypatch):
    probes = PROBES + (_BLIND,)
    real_run, real_cov = cli.run_report, cli.coverage_report
    monkeypatch.setattr(cli, "run_report", lambda: real_run(probes))
    monkeypatch.setattr(cli, "coverage_report", lambda: real_cov(probes))
    code, rep = _json(capsys, ["redblue", "run", "--json"])
    assert code == 1 and rep["missed"] == ["novel_unknown"]
    assert rep["vectors"][-1]["detected"] is False and rep["vectors"][-1]["containment"] is None
    code, rep = _json(capsys, ["redblue", "coverage", "--json"])
    assert code == 1 and rep["overall"]["missed"] == ["novel_unknown"]
    assert main(["redblue", "run"]) == 1
    assert "MISSED    novel_unknown" in capsys.readouterr().out


def test_bad_usage_exits_2():
    with pytest.raises(SystemExit) as exc:
        main(["redblue"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["redblue", "run", "--live"])
    assert exc.value.code == 2


def test_no_flag_can_turn_on_live_containment():
    sub = _parser()._subparsers._group_actions[0].choices["redblue"]
    rsub = sub._subparsers._group_actions[0].choices
    assert set(rsub) == {"run", "coverage"}
    flags = {opt for p in rsub.values() for a in p._actions for opt in a.option_strings}
    assert flags <= {"-h", "--help", "--json", "--journal"}


def test_nothing_live_is_executed(capsys, monkeypatch):
    made = []
    real_init = containment.ContainmentExecutor.__init__

    def spy_init(self, *a, **kw):
        real_init(self, *a, **kw)
        made.append(self)
    monkeypatch.setattr(containment.ContainmentExecutor, "__init__", spy_init)

    def boom(*_a, **_kw):
        raise AssertionError("a live containment path was reached")
    monkeypatch.setattr(WatchDog, "contain", boom)            # the handler path is never taken either
    assert main(["redblue", "run", "--json"]) == 0
    capsys.readouterr()
    assert made, "the run should route containment through a ContainmentExecutor"
    for ex in made:
        assert ex.dry_run is True and ex.sinks == {}
        assert ex.audit and all(not r.executed for r in ex.audit)


def test_journal_appends_findings_and_incidents(tmp_path, capsys):
    path = tmp_path / "ws" / "redblue.jsonl"
    code, rep = _json(capsys, ["redblue", "run", "--json", "--journal", str(path)])
    assert code == 0 and rep["journal"] == {"path": str(path), "findings": len(PROBES), "incidents": len(PROBES)}
    j = IncidentJournal(path)
    assert len(j.findings()) == len(PROBES) and len(j.incidents()) == len(PROBES)
    main(["redblue", "run", "--journal", str(path)])            # append-only: a second run adds, never rewrites
    assert "journal: +" in capsys.readouterr().out
    assert len(j.findings()) == 2 * len(PROBES)


def test_run_without_journal_writes_nothing(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    assert main(["redblue", "run"]) == 0
    capsys.readouterr()
    assert list(ws.iterdir()) == []
