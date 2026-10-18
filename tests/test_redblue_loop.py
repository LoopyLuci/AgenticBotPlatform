"""Red<->blue loop: RedTeam enumerates valid Findings, and run_loop reports blue's coverage."""
from bot.redblue.redteam import LoopReport, RedTeam, run_loop
from bot.redblue.schemas import validate_finding
from bot.redblue.taxonomy import VectorProbe
from bot.redblue.schemas import VectorClass
from bot.redblue.watchdog import WatchDog


def test_redteam_enumerates_schema_valid_findings():
    findings = RedTeam().enumerate()
    assert len(findings) >= 6
    for f in findings:
        validate_finding(f.to_dict())           # each is schema-valid
        assert f.found_by == "wild"


def test_loop_reports_full_coverage_over_the_taxonomy():
    rep = run_loop(RedTeam())
    assert isinstance(rep, LoopReport)
    assert rep.missed == [] and rep.coverage == 1.0
    assert rep.to_dict()["coverage"] == 1.0


def test_loop_flags_a_vector_blue_cannot_see():
    # a bespoke vector WatchDog has no signature for -> it must show up as missed, coverage < 1
    blind = VectorProbe("novel_unknown", VectorClass.AGENT_RUNTIME,
                        {"action": "memory_poison", "target": "context", "grant_result": "allowed"})
    rep = run_loop(RedTeam(probes=(blind,)))
    assert rep.missed == ["novel_unknown"] and rep.coverage == 0.0


def test_loop_accepts_an_external_watchdog():
    wd = WatchDog()
    run_loop(RedTeam(), wd)
    assert wd.summary()["total"] >= 6          # the loop fed its incidents into the given WatchDog
