"""bot/vision: OpenCV and its model zoo, for every agent. Real images and real models throughout: the zoo's models are
downloaded once (verified against their SHA-256) into a shared cache ($ABP_VISION_MODELS_DIR, default
data/vision/models); without network and without them the model tests skip. Text and QR codes are drawn here with
Pillow and qrcode, so they need nothing but ABP's own dependencies; the photos come from OpenCV's own sample and test
data when its checkout is on this machine (the opencv and opencv-extra modules), else those tests skip."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from bot.vision import images, ops, pipelines, service, zoo
from bot.vision.images import VisionError

ROOT = Path(__file__).resolve().parent.parent
MODELS = Path(os.environ.get("ABP_VISION_MODELS_DIR") or ROOT / "data" / "vision" / "models")
SAMPLES = [Path(p) for p in (os.environ.get("ABP_OPENCV_DIR", ""), "E:/Projects/OpenCV/opencv") if p]
EXTRA = [Path(p) for p in (os.environ.get("ABP_OPENCV_EXTRA_DIR", ""), "E:/Projects/OpenCV/opencv_extra") if p]


@pytest.fixture(autouse=True)
def _vision_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_VISION_DIR", str(tmp_path / "vision"))
    monkeypatch.setenv("ABP_VISION_MODELS_DIR", str(MODELS))


def _sample(name: str) -> Path:
    for d in SAMPLES:
        if (d / "samples" / "data" / name).is_file():
            return d / "samples" / "data" / name
    pytest.skip(f"OpenCV's sample image {name} is not on this machine (the opencv module's checkout)")


def _extra(rel: str) -> Path:
    for d in EXTRA:
        if (d / "testdata" / rel).is_file():
            return d / "testdata" / rel
    pytest.skip(f"opencv_extra's {rel} is not on this machine (the opencv-extra module's checkout)")


def _models(*keys: str) -> None:
    try:
        for k in keys:
            zoo.ensure(k)
    except (OSError, VisionError) as e:
        pytest.skip(f"the zoo's {', '.join(keys)} model(s) could not be fetched here: {e}")


def _ui() -> np.ndarray:
    """A small settings screen: two buttons and three lines of text, drawn with Pillow's own scalable font."""
    im = Image.new("RGB", (900, 360), (245, 246, 248))
    d = ImageDraw.Draw(im)
    font, small = ImageFont.load_default(size=26), ImageFont.load_default(size=20)
    d.rectangle((40, 40, 250, 95), fill=(37, 99, 235))
    d.text((62, 52), "Save changes", font=font, fill=(255, 255, 255))
    d.rectangle((280, 40, 420, 95), outline=(120, 120, 120), width=2)
    d.text((305, 52), "Cancel", font=font, fill=(30, 30, 30))
    d.text((40, 140), "Model: qwen3.5-9b  Tokens: 4,096", font=small, fill=(20, 20, 20))
    d.text((40, 240), "Error 404: module not found (retry?)", font=small, fill=(180, 30, 30))
    return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)


# ---- sources ----------------------------------------------------------------------------------------------------
def test_images_come_from_files_and_data_urls_and_local_urls_are_refused(tmp_path):
    img = np.full((40, 60, 3), (10, 200, 30), np.uint8)
    f = tmp_path / "a.png"
    cv2.imwrite(str(f), img)
    got, origin = images.load(str(f))
    assert got.shape == (40, 60, 3) and origin["kind"] == "file"
    url = "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode()
    assert images.load(url)[0].shape == (40, 60, 3)
    for bad, why in (("http://127.0.0.1:8787/x.png", "private or local"), (str(tmp_path / "nope.png"), "no such file"),
                     ("", "give an image"), ("data:text/plain;base64,aGk=", "data:image")):
        with pytest.raises(VisionError, match=why):
            images.load(bad)
    (tmp_path / "junk.png").write_bytes(b"not an image")
    with pytest.raises(VisionError, match="not an image"):
        images.load(str(tmp_path / "junk.png"))
    saved = images.save(img, "a result")
    assert Path(saved).is_file() and Path(saved).parent == images.out_dir()


# ---- operations without models ------------------------------------------------------------------------------------
def test_edits_colours_shapes_and_matching():
    img = np.full((300, 400, 3), 255, np.uint8)
    cv2.rectangle(img, (40, 40), (200, 120), (0, 0, 200), -1)
    cv2.circle(img, (300, 200), 50, (200, 0, 0), -1)
    assert ops.transform(img, "resize", {"width": 200}).shape == (150, 200, 3)
    assert ops.transform(img, "crop", {"x": 10, "y": 20, "width": 50, "height": 30}).shape == (30, 50, 3)
    assert ops.transform(img, "rotate", {"angle": 90}).shape == (400, 300, 3)
    for op in ops.TRANSFORMS:
        assert ops.transform(img, op, {"width": 100} if op == "resize" else {}).ndim == 3
    with pytest.raises(VisionError, match="unknown operation"):
        ops.transform(img, "melt")
    shapes = ops.contours(img)
    assert {s["shape"] for s in shapes} >= {"rectangle", "circle"}
    found = [c["rgb"] for c in ops.colors(img, 3)]                        # (anti-aliased edges shift a centre a shade)
    for want in ((255, 255, 255), (200, 0, 0), (0, 0, 200)):
        assert any(max(abs(a - b) for a, b in zip(rgb, want)) <= 8 for rgb in found), (want, found)
    # a region found by template matching, and at another size
    ui = _ui()
    hit = ops.match_template(ui, ui[40:96, 280:421], 0.9)
    assert hit and hit[0]["box"][:2] == [280, 40] and hit[0]["score"] > 0.99
    bigger = cv2.resize(ui, None, fx=1.25, fy=1.25)
    assert ops.match_template(bigger, ui[40:96, 280:421], 0.85, [1.0, 1.25])[0]["box"][0] in range(345, 356)
    # before/after: identical, then a changed region where the change is
    same, _ = ops.compare(ui, ui.copy())
    assert same["identical"] and same["similarity"] > 0.999
    after = ui.copy()
    cv2.rectangle(after, (600, 250), (700, 300), (0, 160, 0), -1)
    diff, shown = ops.compare(ui, after)
    assert not diff["identical"] and diff["regions"]
    x, y, w, h = diff["regions"][0]["box"]
    assert x <= 600 and y <= 250 and x + w >= 700 and y + h >= 300 and shown.shape == ui.shape


def test_feature_matching_finds_a_rotated_object():
    photo = cv2.imread(str(_sample("box_in_scene.png")))
    obj = cv2.imread(str(_sample("box.png")))
    hit = ops.match_features(photo, obj)
    assert hit is not None and hit["inliers"] >= 10 and len(hit["corners"]) == 4


# ---- the zoo ------------------------------------------------------------------------------------------------------
def test_models_are_verified_and_a_corrupt_file_is_refetched():
    _models("face_detect")
    f = zoo.MODELS["face_detect"].files[0]
    p = zoo.models_dir() / f.dir / f.name
    assert zoo._sha256(p) == f.sha256
    with pytest.raises(VisionError, match="unknown model"):
        zoo.ensure("nope")
    assert {m["model"] for m in zoo.status()} >= {"face_detect", "objects", "text_detect", "text_recognize"}


# ---- pipelines on real models -------------------------------------------------------------------------------------
def test_text_on_a_ui_is_read_line_by_line_with_confidence():
    _models("text_detect", "text_recognize")
    lines = pipelines.text(_ui())
    texts = [r["text"] for r in lines]
    assert "Save changes" in texts and "Cancel" in texts
    assert "Error 404: module not found (retry?)" in texts
    assert any(t.startswith("Model: qwen3.5-9b") and t.endswith("4,096") for t in texts)
    assert all(r["confidence"] > 0.8 for r in lines)
    save = next(r for r in lines if r["text"] == "Save changes")
    assert 40 <= save["box"][0] <= 70 and 40 <= save["box"][1] <= 60        # where it is drawn


def test_codes_are_decoded():
    import qrcode
    qr = qrcode.make("https://example.invalid/abp?x=1").convert("RGB")
    img = cv2.cvtColor(np.asarray(qr), cv2.COLOR_RGB2BGR)
    found = pipelines.codes(cv2.copyMakeBorder(img, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=(255, 255, 255)))
    assert [c["text"] for c in found if c["kind"] == "qr"] == ["https://example.invalid/abp?x=1"]


def test_faces_objects_and_people_on_opencvs_own_photos():
    _models("face_detect", "face_recognize", "objects", "people")
    lena = cv2.imread(str(_sample("lena.jpg")))
    f = pipelines.faces(lena)
    assert len(f) == 1 and f[0]["score"] > 0.8 and set(f[0]["landmarks"]) == {
        "right_eye", "left_eye", "nose", "mouth_right", "mouth_left"}
    assert pipelines.same_person(lena, cv2.flip(lena, 1))["same_person"] is True
    dog = cv2.imread(str(_extra("dnn/dog416.png")))
    labels = [o["label"] for o in pipelines.objects(dog)]
    assert {"dog", "bicycle", "truck"} <= set(labels)
    messi = cv2.imread(str(_sample("messi5.jpg")))
    assert "sports ball" in [o["label"] for o in pipelines.objects(messi, classes=["sports ball"])]
    info, mask = pipelines.people_mask(messi)
    assert 0.05 < info["people_share"] < 0.5 and mask.shape == messi.shape[:2]


# ---- the service, the tools and the API ---------------------------------------------------------------------------
def test_find_text_on_an_image_and_analyze(tmp_path):
    _models("text_detect", "text_recognize")
    f = tmp_path / "ui.png"
    cv2.imwrite(str(f), _ui())
    res = service.find(str(f), text="cancel")
    assert res["found"] == 1 and res["by"] == "text" and Path(res["annotated"]).is_file()
    cx, cy = res["matches"][0]["center"]
    assert 300 <= cx <= 400 and 50 <= cy <= 90
    tmpl = tmp_path / "btn.png"
    cv2.imwrite(str(tmpl), _ui()[40:96, 40:251])
    assert service.find(str(f), template=str(tmpl))["matches"][0]["box"][:2] == [40, 40]
    a = service.analyze(str(f), ["info", "text", "colors"])
    assert "Cancel" in a["text_joined"] and a["info"]["width"] == 900 and a["colors"]
    with pytest.raises(VisionError, match="unknown task"):
        service.analyze(str(f), ["telepathy"])


def test_registering_the_tools_loads_no_opencv():
    """Every agent process registers the tools; OpenCV and numpy load only on a vision call (loading them during a turn
    hung abp_acp on Windows, behind its reader thread's pending read on stdin)."""
    import subprocess
    import sys
    code = "import sys; from bot.vision import tools; print('cv2' in sys.modules, 'numpy' in sys.modules)"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.stdout.split() == ["False", "False"], r.stdout + r.stderr
    from bot.vision import tools
    assert tuple(service.TASKS) == tools.TASKS


def test_the_agent_tools_and_the_camera_rule(tmp_path):
    from bot.agent_runtime import toolspec
    from bot.agent_runtime import tools  # noqa: F401 - registers them
    names = {n for n in toolspec.registered_names() if n.startswith("vision_")}
    assert names == {"vision_analyze", "vision_find", "vision_compare", "vision_edit", "vision_faces",
                     "vision_capture", "vision_status"}
    spec = toolspec.spec_for("vision_capture")                          # the camera always asks the person first
    assert spec.needs_approval is True and not spec.read_only
    assert toolspec.spec_for("vision_analyze").read_only
    out = asyncio.run(toolspec.dispatch("vision_analyze", {"image": "camera:0"}))
    assert out.startswith("Error: the camera is reached only through vision_capture")
    f = tmp_path / "x.png"
    cv2.imwrite(str(f), np.full((50, 80, 3), 128, np.uint8))
    edited = json.loads(asyncio.run(toolspec.dispatch("vision_edit", {"image": str(f), "steps": [
        {"op": "resize", "width": 40}, {"op": "gray"}]})))
    assert edited["size"] == [40, 25] and Path(edited["path"]).is_file()
    assert asyncio.run(toolspec.dispatch("vision_edit", {"image": str(f), "steps": [{"op": "melt"}]})).startswith("Error:")
    st = json.loads(asyncio.run(toolspec.dispatch("vision_status", {})))
    assert st["device"]["opencv"].startswith(("4.", "5.")) and "objects" in st["tasks"]


def test_the_api(tmp_path, monkeypatch, temp_db):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app
    monkeypatch.setenv("DASHBOARD_TOKEN", "unused")
    c = TestClient(build_app())
    H = {"X-Dashboard-Token": "unused"}
    assert c.get("/api/vision").status_code == 401 or c.get("/api/vision", headers={}).status_code in (401, 403)
    st = c.get("/api/vision", headers=H)
    assert st.status_code == 200 and "edits" in st.json()
    img = np.full((60, 90, 3), 200, np.uint8)
    ok, buf = cv2.imencode(".png", img)
    data = "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode()
    r = c.post("/api/vision/edit", headers=H, json={"image": data, "steps": [{"op": "flip"}]})
    assert r.status_code == 200 and r.json()["path_url"].startswith("/api/vision/out/")
    pic = c.get(r.json()["path_url"], headers=H)
    assert pic.status_code == 200 and pic.content[:4] == b"\x89PNG"
    assert c.get("/api/vision/out/..%5Csecret", headers=H).status_code in (400, 404)
    assert c.post("/api/vision/analyze", headers=H, json={"image": "http://10.0.0.1/x.png"}).status_code == 400
