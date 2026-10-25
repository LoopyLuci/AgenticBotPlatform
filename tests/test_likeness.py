"""Likeness (Video Studio V3): the consent, refusal, provenance and audit rules, the ONNX patch, and the API.

The engine's models are not loaded here (450 MB); the face functions are replaced with stand-ins, so these tests
check the rules around the swap, not the swap itself (that was measured live and is recorded in PLAN-video-studio)."""
from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from bot.likeness import engine, jobs, onnxfix, policy, provenance, store

FACE = {"box": [10, 10, 40, 40], "score": 0.9, "landmarks": {"right_eye": [20, 20], "left_eye": [40, 20], "nose": [30, 30],
                                                              "mouth_right": [22, 40], "mouth_left": [38, 40]}}


@pytest.fixture
def lk(tmp_path, monkeypatch, temp_db):
    monkeypatch.setenv("ABP_LIKENESS_DIR", str(tmp_path / "likeness"))
    monkeypatch.setattr(jobs, "settings", lambda: {"enabled": True, "max_seconds": 5})
    feats = {"same": [1.0, 0.0, 0.0], "other": [0.0, 1.0, 0.0]}
    who = {"next": "same"}
    monkeypatch.setattr(engine, "faces", lambda img, min_score=0.6: [dict(FACE)])
    monkeypatch.setattr(engine, "sface_feature", lambda img, f: feats[who["next"]])
    monkeypatch.setattr(engine, "source_latent", lambda img, f: np.zeros((1, 512), np.float32))
    monkeypatch.setattr(engine, "swap_image", lambda img, lat, target="largest", ref=None: (img.copy(), 1))
    for i in (1, 2):
        cv2.imwrite(str(tmp_path / f"p{i}.png"), np.full((64, 64, 3), 100 + i, np.uint8))
    return tmp_path, who


def _consent(tmp_path, **over):
    body = {"name": "Owner", "kind": "self", "attested_by": "owner", "adult": True,
            "statement": "This is my own face and I agree to its use.", "images": [str(tmp_path / "p1.png")]}
    return jobs.create_consent(body | over)


def _wait(job_id):
    import time
    for _ in range(200):
        j = jobs.job(job_id)
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.02)
    raise AssertionError("job did not finish")


# ---- policy ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["make her nude", "an NSFW clip", "a sexy scene", "put him in a porn video",
                                  "my 12 year old son", "a teen at school", "for the kids party", "undress the photo"])
def test_sexual_and_minor_requests_are_refused(text):
    assert policy.text_problem([text])


@pytest.mark.parametrize("text", ["birthday greeting from me", "product ad with my face", "a wedding toast clip"])
def test_ordinary_requests_pass(text):
    assert policy.text_problem([text]) is None


def test_check_needs_switch_consent_and_an_adult():
    ok = {"id": "c", "adult": True, "name": "A"}
    with pytest.raises(policy.Refused, match="switched off"):
        policy.check(ok, [], enabled=False)
    with pytest.raises(policy.Refused, match="consent record is required"):
        policy.check(None, [], enabled=True)
    with pytest.raises(policy.Refused, match="revoked"):
        policy.check(ok | {"revoked": {"at": 1}}, [], enabled=True)
    with pytest.raises(policy.Refused, match="adult"):
        policy.check(ok | {"adult": False}, [], enabled=True)
    policy.check(ok, ["a toast"], enabled=True)


# ---- consent -----------------------------------------------------------------------------------------------------
def test_consent_needs_attestation_and_adult(lk):
    tmp, _ = lk
    with pytest.raises(ValueError, match="adult"):
        _consent(tmp, adult=False)
    with pytest.raises(ValueError, match="statement"):
        _consent(tmp, statement=" ")
    with pytest.raises(ValueError, match="kind"):
        _consent(tmp, kind="celebrity")
    c = _consent(tmp)
    assert c["id"].startswith("c_") and c["reference_faces"] == 1 and "faces" not in c
    assert jobs.revoke_consent(c["id"], "changed my mind")["revoked"]["reason"] == "changed my mind"
    assert store.consent(c["id"])["revoked"]                 # revoked, never deleted
    assert [e["event"] for e in store.job_log()][:2] == ["consent_revoked", "consent_added"]


# ---- jobs --------------------------------------------------------------------------------------------------------
def test_faceswap_image_is_labelled_with_provenance_and_audited(lk):
    from bot import db
    tmp, _ = lk
    c = _consent(tmp)
    j = _wait(jobs.submit({"mode": "faceswap", "consent_id": c["id"], "source": str(tmp / "p1.png"),
                           "target": str(tmp / "p2.png"), "purpose": "a birthday card"})["id"])
    assert j["status"] == "done", j
    out = store.sub("out") / j["output"]
    m = provenance.read_embedded(out)
    assert m["ai_generated"] and m["likeness"]["consent_id"] == c["id"] and m["digital_source_type"].endswith("trainedAlgorithmicMedia")
    assert json.loads(out.with_name(out.name + ".provenance.json").read_text())["likeness"]["job_id"] == j["id"]
    img = cv2.imread(str(out))
    assert img is not None and img[-6:, :40].mean() < 100        # the dark label band in the corner
    acts = [r["action"] for r in db.get_conn().execute("SELECT action FROM audit_log").fetchall()]
    assert "likeness_job_queued" in acts and "likeness_job_done" in acts


def test_refusals_are_logged(lk):
    tmp, _ = lk
    c = _consent(tmp)
    base = {"mode": "faceswap", "consent_id": c["id"], "source": str(tmp / "p1.png"), "target": str(tmp / "p2.png")}
    with pytest.raises(policy.Refused):
        jobs.submit(base | {"purpose": "make a nude version"})
    with pytest.raises(policy.Refused):
        jobs.submit(base | {"consent_id": "c_missing", "purpose": "card"})
    jobs.revoke_consent(c["id"])
    with pytest.raises(policy.Refused, match="revoked"):
        jobs.submit(base | {"purpose": "card"})
    assert sum(e["event"] == "refused" for e in store.job_log()) == 3


def test_a_source_face_that_is_not_the_consented_person_is_refused(lk):
    tmp, who = lk
    c = _consent(tmp)
    who["next"] = "other"
    j = _wait(jobs.submit({"mode": "faceswap", "consent_id": c["id"], "source": str(tmp / "p1.png"),
                           "target": str(tmp / "p2.png"), "purpose": "card"})["id"])
    assert j["status"] == "refused" and "does not match" in j["error"] and j["output"] is None


def test_switched_off_refuses(lk, monkeypatch):
    tmp, _ = lk
    c = _consent(tmp)
    monkeypatch.setattr(jobs, "settings", lambda: {"enabled": False, "max_seconds": 5})
    with pytest.raises(policy.Refused, match="switched off"):
        jobs.submit({"mode": "faceswap", "consent_id": c["id"], "source": "x", "target": "y", "purpose": "card"})


def test_recast_needs_a_video(lk):
    tmp, _ = lk
    c = _consent(tmp)
    with pytest.raises(ValueError, match="video"):
        jobs.submit({"mode": "recast", "consent_id": c["id"], "source": str(tmp / "p1.png"),
                     "target": str(tmp / "p2.png"), "purpose": "card"})


def test_character_and_lora_dataset(lk):
    tmp, _ = lk
    c = _consent(tmp)
    ch = jobs.create_character({"name": "Me", "consent_id": c["id"], "images": [str(tmp / "p1.png"), str(tmp / "p2.png")]})
    assert ch["trigger"] == "abpme" and len(ch["images"]) == 2
    info = jobs.build_dataset(ch["id"])
    folder = store.sub("datasets") / ch["id"] / "10_abpme"
    assert info["images"] == 2 and (folder / "000.txt").read_text() == "photo of abpme person"


# ---- provenance and the ONNX patch -------------------------------------------------------------------------------
def test_webm_manifest_round_trip(tmp_path):
    p = tmp_path / "x.webm"
    p.write_bytes(b"\x1aE\xdf\xa3fake")
    provenance.append_webm(p, {"ai_generated": True, "n": 1})
    assert provenance.read_embedded(p) == {"ai_generated": True, "n": 1}


def _tensor(dims, arr):
    f = [(1, 0, d) for d in dims] + [(2, 0, 1), (8, 2, b"emap"), (9, 2, arr.astype("<f4").tobytes())]
    return onnxfix._encode(f)


def test_onnx_patch_drops_the_resize_roi_and_reads_the_emap():
    node = onnxfix._encode([(1, 2, b"x"), (1, 2, b"roi"), (1, 2, b"scales"), (2, 2, b"y"), (4, 2, b"Resize")])
    other = onnxfix._encode([(1, 2, b"y"), (2, 2, b"z"), (4, 2, b"Relu")])
    emap = np.arange(512 * 512, dtype=np.float32).reshape(512, 512)
    graph = onnxfix._encode([(1, 2, node), (1, 2, other), (5, 2, _tensor((512, 512), emap))])
    model = onnxfix._encode([(1, 0, 8), (7, 2, graph)])
    data, got, changed = onnxfix.patch_inswapper(model)
    assert changed == 1 and np.array_equal(got, emap)
    g = dict((f, v) for f, _, v in onnxfix.fields(data))[7]
    n0 = onnxfix.fields(onnxfix.fields(g)[0][2])
    assert [bytes(v) for f, _, v in n0 if f == 1] == [b"x", b"scales"]


# ---- the API -----------------------------------------------------------------------------------------------------
def test_api_needs_the_token_and_switch_is_audited(lk, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from fastapi.testclient import TestClient

    from bot.config import config
    from bot.dashboard.server import build_app

    written = []
    monkeypatch.setattr(config, "set_values", lambda changes, actor="dashboard": written.append(dict(changes)))
    c, h = TestClient(build_app()), {"X-Dashboard-Token": "test-token"}
    assert c.get("/api/likeness").status_code in (401, 403)
    assert c.get("/api/likeness", headers=h).json()["settings"]["enabled"] is True
    assert c.put("/api/likeness/settings", headers=h, json={"enabled": False}).status_code == 200
    assert c.put("/api/likeness/settings", headers=h, json={"max_seconds": 30}).status_code == 200
    assert written == [{("likeness", "enabled"): False}, {("likeness", "max_seconds"): 30}]
    for bad in ({"enabled": "yes"}, {"max_seconds": 1}, {}):
        assert c.put("/api/likeness/settings", headers=h, json=bad).status_code == 400
    r = c.post("/api/likeness/jobs", headers=h, json={"mode": "faceswap", "consent_id": "nope", "purpose": "x"})
    assert r.status_code == 403 and "refused" in r.json()["detail"]
    up = c.post("/api/likeness/uploads", headers=h, files={"file": ("a.png", b"\x89PNG....", "image/png")})
    assert up.status_code == 200 and up.json()["id"].startswith("upload:")
    assert c.post("/api/likeness/uploads", headers=h, files={"file": ("a.exe", b"MZ", "application/octet-stream")}).status_code == 400
    assert c.get("/api/likeness/out/..%2Fconsents.json", headers=h).status_code == 404


def test_a_damaged_model_file_is_fetched_again(tmp_path, monkeypatch):
    """A hard crash once left arcface_w600k_r50.onnx as a right-sized file of zeros; size alone must not pass it."""
    from bot.likeness import models
    monkeypatch.setenv("ABP_LIKENESS_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(models, "_checked", set())
    f = models.File("t", "t.onnx", 4, __import__("hashlib").sha256(b"good").hexdigest(), "x")
    (tmp_path / "t.onnx").write_bytes(b"\0\0\0\0")
    got = []

    class R:
        def __init__(self, *a, **k): pass
        def __enter__(self): return __import__("io").BytesIO(b"good")
        def __exit__(self, *a): return False

    monkeypatch.setattr(models.urllib.request, "urlopen", lambda *a, **k: (got.append(1), R())[1])
    assert models._fetch(f).read_bytes() == b"good" and got == [1]
    assert models._fetch(f) and got == [1]                          # verified once, then trusted
    assert not models._derived_ok(tmp_path / "missing.onnx", tmp_path / "missing.npy")
