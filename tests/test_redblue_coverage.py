"""Blue coverage benchmark: WatchDog must detect every escape vector in the canonical taxonomy.

This is the design's 'did blue detect it' measurement in test form — a regression guard that the rule
detector keeps full coverage of the known vectors the native-sandbox stress suite blocks.
"""
from bot.redblue.taxonomy import PROBES
from bot.redblue.watchdog import WatchDog


def _detect(probe):
    wd = WatchDog()
    for ev in probe.prelude_events():
        wd.observe_event(ev)
    return wd.observe_event(probe.trigger())


def test_watchdog_covers_every_taxonomy_vector():
    misses = []
    for p in PROBES:
        inc = _detect(p)
        if inc is None or inc.verdict != p.expect:
            misses.append((p.name, None if inc is None else inc.verdict))
    coverage = (len(PROBES) - len(misses)) / len(PROBES)
    assert misses == [], f"WatchDog missed {misses}; coverage {coverage:.0%}"
    assert coverage == 1.0


def test_each_vector_names_a_signature_and_proposes_containment():
    from bot.redblue.detector import CONTAINMENT
    for p in PROBES:
        inc = _detect(p)
        assert inc.matched_signature, p.name
        assert inc.containment_action in CONTAINMENT, p.name


def test_taxonomy_spans_multiple_vector_classes():
    classes = {p.vector_class for p in PROBES}
    assert len(classes) >= 4          # filesystem, network, process, resource at least
