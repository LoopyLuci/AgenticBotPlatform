"""Likeness: face swap, recast and identity-consistent characters for Video Studio, on this machine only (V3).

Every job uses a real person's likeness only under a consent record (the owner attests the person agreed, or that it
is their own likeness, and that the person is an adult); the source face must match that record's reference faces.
Sexual content involving real people and anything involving minors is refused. Every output carries a visible
"AI-generated" label and embedded provenance (plus a sidecar manifest), and every job, refusal included, is audited.

Off by default: likeness.enabled, switched from the Workspace.

    store.py       consent records, character profiles, the job log (data/likeness)
    policy.py      the refusal rules
    models.py      the pinned model files (InsightFace inswapper + ArcFace), fetched and verified on first use
    onnxfix.py     the dependency-free model patch that lets OpenCV's DNN run the swapper
    engine.py      detect, align, embed, swap, blend; images and videos
    provenance.py  the visible label, embedded metadata and the sidecar manifest
    jobs.py        the job runner the API and tools use
"""
