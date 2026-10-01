"""bot/fileserver beyond the parity array, on real files in temporary folders: user shares across disks and a cache,
allocation and split level, the mover, users/access/links, the REST API (resumable uploads, Range, thumbnails),
WebDAV, the content index (word and meaning search, exact and near-duplicate photos), the ransomware guard, encrypted
deduplicated backups, and transfer jobs (copy, mirror, two-way with conflicts)."""
from __future__ import annotations

import base64
import hashlib
import os
import time
from pathlib import Path

import numpy as np
import pytest
from starlette.testclient import TestClient

from bot.fileserver import array, backup, guard, index, mover, shares, transfer
from bot.fileserver.store import FsError


@pytest.fixture
def nas(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_FILESERVER_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ABP_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("ABP_EMBED_URL", "")
    monkeypatch.setenv("DASHBOARD_TOKEN", "unused-admin")
    index._backend_cache.clear()
    monkeypatch.setattr(index, "embed_backend", lambda: ("hash-tfidf", index.hash_vectors))
    disks = []
    for i in range(2):
        d = tmp_path / f"disk{i + 1}"
        d.mkdir()
        disks.append(d)
    array.configure([{"name": "d1", "path": str(disks[0])}, {"name": "d2", "path": str(disks[1])}],
                    [{"path": str(tmp_path / "parity")}], block_kib=64, allow_same_drive=True)
    cache = tmp_path / "cache"
    shares.set_pool("cache", str(cache))
    return tmp_path, disks, cache


def _client():
    from bot.fileserver.server import build_app
    return TestClient(build_app())


ADMIN = {"x-dashboard-token": "unused-admin"}


def test_user_shares_span_disks_and_follow_their_rules(nas):
    tmp, disks, cache = nas
    s = shares.create("media", {"cache": "yes", "access": "secure", "split_level": 1, "min_free_gb": 0})
    (disks[1] / "media" / "Films").mkdir(parents=True)
    (disks[1] / "media" / "Films" / "a.mkv").write_bytes(b"a" * 10)
    (disks[0] / "media").mkdir()
    (disks[0] / "media" / "readme.txt").write_text("hi")
    names = [e["name"] for e in shares.listing(s)]
    assert names == ["Films", "readme.txt"]                       # one tree from two disks
    assert shares.place(s, "new.bin").parent == cache / "media"   # cache: yes -> new files on the cache
    s2 = shares.edit("media", {"cache": "no"})
    assert shares.place(s2, "Films/b.mkv").parent == disks[1] / "media" / "Films"   # split level keeps Films together
    with pytest.raises(FsError, match="not a valid path"):
        shares.clean("../etc/passwd")
    with pytest.raises(FsError):
        shares.clean("x/.abp-recycle/y")
    # the mover takes cached files to the array once they settle
    shares.edit("media", {"cache": "yes"})
    f = cache / "media" / "Music" / "song.mp3"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"m" * 100)
    old = time.time() - 3600
    os.utime(f, (old, old))
    r = mover.run()
    assert r["moved"] == 1 and not f.exists()
    assert shares.locate(shares.get("media"), "Music/song.mp3")[0] in ("d1", "d2")
    # delete goes to the recycle bin on the same disk, and the bin is purged when old
    assert shares.remove_path(shares.get("media"), "readme.txt") == 1
    assert list((disks[0] / ".abp-recycle" / "media").rglob("readme.txt"))
    assert shares.recycle_purge(days=0) >= 1


def test_access_users_and_links(nas):
    s = shares.create("docs", {"cache": "no", "access": "private", "users": {"ann": "rw", "bob": "r"}})
    shares.set_user("ann", "correct horse")
    shares.set_user("bob", "battery staple")
    ann, bob = shares.check_user("ann", "correct horse"), shares.check_user("bob", "battery staple")
    assert shares.can(ann, s, "w") and shares.can(bob, s, "r") and not shares.can(bob, s, "w") and not shares.can(None, s, "r")
    assert shares.check_user("ann", "wrong") is None
    for _ in range(5):
        shares.check_user("bob", "nope")
    assert shares.check_user("bob", "battery staple") is None           # locked for a minute after five misses
    with pytest.raises(FsError, match="at least 8"):
        shares.set_user("x", "short")
    guard.freeze(["docs"])
    assert not shares.can(ann, s, "w") and shares.can({"admin": True}, s, "w")
    guard.unfreeze(["docs"])
    assert shares.can(ann, s, "w")


def test_rest_api_uploads_resume_and_links(nas):
    tmp, disks, cache = nas
    shares.create("pub", {"cache": "no", "access": "public"})
    shares.create("priv", {"cache": "no", "access": "private", "users": {"ann": "rw"}})
    shares.set_user("ann", "correct horse")
    c = _client()
    assert [s["name"] for s in c.get("/api/shares").json()] == ["pub"]
    assert c.get("/api/list", params={"share": "priv"}).status_code == 401
    auth = {"authorization": "Basic " + base64.b64encode(b"ann:correct horse").decode()}
    data = os.urandom(300_000)
    sha = hashlib.sha256(data).hexdigest()
    up = c.post("/api/uploads", json={"share": "priv", "path": "big/data.bin", "size": len(data), "sha256": sha}, headers=auth).json()
    assert c.patch(f"/api/uploads/{up['id']}", content=data[:100_000], headers={**auth, "Upload-Offset": "0"}).json() == {"offset": 100_000, "done": False}
    bad = c.patch(f"/api/uploads/{up['id']}", content=data[:10], headers={**auth, "Upload-Offset": "5"})
    assert bad.status_code == 409 and bad.json()["offset"] == 100_000           # the client resumes from here
    assert c.get(f"/api/uploads/{up['id']}", headers=auth).json()["offset"] == 100_000
    assert c.patch(f"/api/uploads/{up['id']}", content=data[100_000:], headers={**auth, "Upload-Offset": "100000"}).json()["done"]
    r = c.get("/api/file", params={"share": "priv", "path": "big/data.bin"}, headers={**auth, "Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == data[10:20]
    # a damaged upload (wrong checksum) is refused
    up2 = c.post("/api/uploads", json={"share": "priv", "path": "x.bin", "size": 4, "sha256": "0" * 64}, headers=auth).json()
    assert c.patch(f"/api/uploads/{up2['id']}", content=b"abcd", headers={**auth, "Upload-Offset": "0"}).status_code == 422
    # sign-in cookie, mkdir, move, copy, delete
    assert c.post("/login", json={"user": "ann", "password": "correct horse"}).status_code == 200
    assert c.post("/api/mkdir", json={"share": "priv", "path": "folder"}).status_code == 201
    assert c.post("/api/move", json={"share": "priv", "from": "big/data.bin", "to": "folder/d.bin"}).json() == {"moved": True}
    assert c.post("/api/copy", json={"share": "priv", "from": "folder", "to": "pubcopy", "to_share": "pub"}).json() == {"copied": 1}
    assert [e["name"] for e in TestClient(c.app).get("/api/list", params={"share": "pub", "path": "pubcopy"}).json()["entries"]] == ["d.bin"]
    names = [e["name"] for e in c.get("/api/list", params={"share": "priv", "path": "folder"}).json()["entries"]]
    assert names == ["d.bin"]
    # links: password, download counting, expiry
    link = c.post("/api/links", json={"share": "priv", "path": "folder/d.bin", "password": "pw", "max_downloads": 1}).json()
    anon = TestClient(c.app)
    assert anon.get(f"/s/{link['token']}").status_code == 401
    assert anon.get(f"/s/{link['token']}", params={"pw": "pw"}).content == data
    assert anon.get(f"/s/{link['token']}", params={"pw": "pw"}).status_code == 410            # used up
    folder = c.post("/api/links", json={"share": "priv", "path": "folder", "allow_upload": True}).json()
    assert anon.put(f"/s/{folder['token']}/dropped.txt", content=b"hello").status_code == 201
    assert "dropped.txt" in anon.get(f"/s/{folder['token']}").text
    assert c.delete("/api/file", params={"share": "priv", "path": "folder/dropped.txt"}).json() == {"removed": 1}


def test_webdav_does_what_clients_need(nas):
    shares.create("files", {"cache": "no", "access": "private", "users": {"ann": "rw"}})
    shares.set_user("ann", "correct horse")
    c = _client()
    auth = {"authorization": "Basic " + base64.b64encode(b"ann:correct horse").decode()}
    assert "2" in c.options("/dav/files/").headers["dav"]
    assert c.request("PROPFIND", "/dav/files/", headers=auth).status_code == 207
    assert c.request("MKCOL", "/dav/files/Folder", headers=auth).status_code == 201
    assert c.put("/dav/files/Folder/a.txt", content=b"one", headers=auth).status_code == 201
    assert c.put("/dav/files/Folder/a.txt", content=b"two", headers=auth).status_code == 204
    r = c.request("PROPFIND", "/dav/files/Folder/", headers={**auth, "Depth": "1"})
    assert r.status_code == 207 and b"a.txt" in r.content and b"getcontentlength" in r.content
    lock = c.request("LOCK", "/dav/files/Folder/a.txt", headers={**auth, "Timeout": "Second-60"},
                     content=b'<?xml version="1.0"?><D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope>'
                             b'<D:locktype><D:write/></D:locktype><D:owner>me</D:owner></D:lockinfo>')
    tok = lock.headers["lock-token"]
    assert c.put("/dav/files/Folder/a.txt", content=b"x", headers=auth).status_code == 423
    assert c.put("/dav/files/Folder/a.txt", content=b"three", headers={**auth, "If": f"({tok})"}).status_code == 204
    assert c.request("UNLOCK", "/dav/files/Folder/a.txt", headers={**auth, "Lock-Token": tok}).status_code == 204
    assert c.request("MOVE", "/dav/files/Folder/a.txt", headers={**auth, "Destination": "http://testserver/dav/files/b.txt"}).status_code == 201
    assert c.request("COPY", "/dav/files/b.txt", headers={**auth, "Destination": "http://testserver/dav/files/c.txt"}).status_code == 201
    assert c.get("/dav/files/c.txt", headers=auth).content == b"three"
    assert c.delete("/dav/files/b.txt", headers=auth).status_code == 204
    assert c.request("PROPFIND", "/dav/files/", headers={"Depth": "1"}).status_code == 401


def _img(path: Path, seed: int, size=(240, 180)):
    import cv2
    rng = np.random.default_rng(seed)
    img = np.zeros((size[1], size[0], 3), np.uint8)
    for _ in range(12):
        x, y = rng.integers(0, size[0]), rng.integers(0, size[1])
        cv2.circle(img, (int(x), int(y)), int(rng.integers(10, 50)), [int(v) for v in rng.integers(0, 255, 3)], -1)
    cv2.imwrite(str(path), img)
    return img


def test_index_search_and_duplicates(nas):
    import cv2
    tmp, disks, cache = nas
    shares.create("stuff", {"cache": "no", "access": "public"})
    base = disks[0] / "stuff"
    base.mkdir()
    (base / "budget-2026.md").write_text("Quarterly budget: travel expenses and the hardware refresh plan.")
    (base / "recipe.txt").write_text("Slow-cooked tomato soup with basil and garlic.")
    (base / "notes.py").write_text("def parity_rebuild(disk):\n    return reconstruct(disk)\n")
    (base / "copy-of-recipe.txt").write_text("Slow-cooked tomato soup with basil and garlic.")
    img = _img(base / "photo.png", 1)
    cv2.imwrite(str(base / "photo-small.jpg"), cv2.resize(img, (120, 90)), [cv2.IMWRITE_JPEG_QUALITY, 70])
    _img(base / "other.png", 2)
    r = index.index_share("stuff", describe_images=False)
    assert r["indexed"] == 7 and r["errors"] == 0
    hits = index.search("tomato soup", mode="words")
    assert {h["path"] for h in hits} == {"recipe.txt", "copy-of-recipe.txt"} and "[" in hits[0]["snippet"]
    assert index.search("expenses", mode="meaning")[0]["path"] == "budget-2026.md"      # word forms: expense(s)
    assert index.search("rebuild parity", mode="auto")[0]["path"] == "notes.py"
    assert index.search("garlic", shares=["other"]) == []
    d = index.duplicates()
    kinds = {g["kind"]: g for g in d["groups"]}
    assert {f["path"] for f in kinds["exact"]["files"]} == {"recipe.txt", "copy-of-recipe.txt"}
    assert {f["path"] for f in kinds["similar images"]["files"]} == {"photo.png", "photo-small.jpg"}
    (base / "recipe.txt").unlink()
    assert index.index_share("stuff", describe_images=False)["removed"] == 1
    st = index.stats()
    assert st["shares"]["stuff"]["files"] == 6 and st["shares"]["stuff"]["kinds"]["image"]["files"] == 3


def test_the_guard_catches_a_burst_of_encryption(nas):
    tmp, disks, cache = nas
    shares.create("work", {"cache": "no", "access": "public"})
    base = disks[0] / "work"
    base.mkdir()
    for i in range(30):
        (base / f"doc{i}.txt").write_text(("Meeting notes and plans. " * 400) + str(i))
    assert guard.check() == []                                   # first look: learns what is there
    for _ in range(6):                                           # calm checks teach it the normal rate
        guard.check()
    guard.update("guard", {}, lambda g: g.update(auto_freeze=True))
    for i in range(30):                                          # what ransomware does: encrypt, rename, leave a note
        p = base / f"doc{i}.txt"
        p.write_bytes(os.urandom(p.stat().st_size))
        p.rename(base / f"doc{i}.txt.locked")
    (base / "HOW_TO_RECOVER_FILES.txt").write_text("pay")
    alerts = guard.check()
    assert alerts and alerts[0]["share"] == "work" and alerts[0]["score"] >= 0.6 and alerts[0]["frozen"]
    assert "work" in guard.frozen() and guard.recent_alerts()[0]["share"] == "work"
    guard.unfreeze()
    assert guard.frozen() == set()


def test_backups_are_deduplicated_encrypted_and_restorable(nas, tmp_path):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    big = os.urandom(5 << 20)
    (src / "a" / "big.bin").write_bytes(big)
    (src / "a" / "big-copy.bin").write_bytes(big)               # same content: stored once
    (src / "note.txt").write_text("secret plans")
    repo = backup.Repo.create(tmp_path / "repo", "a long password!")
    r1 = backup.backup(repo, [str(src)])
    assert r1["files"] == 3 and r1["new_chunks"] == 3            # 2 chunks of big + 1 note
    assert not any(b"secret plans" in f.read_bytes() for f in (tmp_path / "repo").rglob("*") if f.is_file())
    with pytest.raises(FsError, match="wrong repository password"):
        backup.Repo(tmp_path / "repo", "wrong password!!")
    (src / "note.txt").write_text("changed plans")
    r2 = backup.backup(repo, [str(src)])
    assert r2["unchanged_files"] == 2 and r2["new_chunks"] == 1
    out = tmp_path / "restored"
    backup.restore(repo, r1["snapshot"], str(out))
    assert (out / "note.txt").read_text() == "secret plans" and (out / "a" / "big.bin").read_bytes() == big
    assert backup.check(repo, 100)["ok"]
    assert backup.forget(repo, keep_last=1)["forgotten"] == [r1["snapshot"]]
    assert backup.prune(repo)["removed_chunks"] == 1             # the old note's chunk
    victim = next(f for f in (tmp_path / "repo" / "data").rglob("*") if f.is_file())
    victim.write_bytes(victim.read_bytes()[:-1] + b"\0")
    assert not backup.check(repo, 100)["ok"]


def test_transfer_jobs_copy_mirror_and_two_way(nas, tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    (a / "x.txt").write_text("x1")
    (a / "sub").mkdir()
    (a / "sub" / "y.bin").write_bytes(os.urandom(20_000))
    (b / "stale.txt").write_text("old")
    transfer.set_job("m", {"source": {"path": str(a)}, "dest": {"path": str(b)}, "mode": "mirror"})
    r = transfer.run("m")
    assert r["done"] == 3 and not r["failed"] and not (b / "stale.txt").exists()
    assert (b / "sub" / "y.bin").read_bytes() == (a / "sub" / "y.bin").read_bytes()
    assert transfer.run("m")["planned"] == 0                      # in step: nothing to do
    transfer.set_job("t", {"source": {"path": str(a)}, "dest": {"path": str(b)}, "mode": "two-way"})
    transfer.run("t")
    (b / "fromB.txt").write_text("b")
    (a / "x.txt").unlink()
    time.sleep(0.05)
    (a / "sub" / "y.bin").write_bytes(b"A side")
    (b / "sub" / "y.bin").write_bytes(b"B side!")                 # changed on both sides: a conflict
    r = transfer.run("t")
    assert not r["failed"] and r["conflicts"] == 1
    assert (a / "fromB.txt").read_text() == "b" and not (b / "x.txt").exists()
    assert (a / "sub" / "y.bin").read_bytes() == (b / "sub" / "y.bin").read_bytes() == b"A side"
    kept = [p.name for p in (a / "sub").iterdir() if "conflict" in p.name]
    assert kept and (a / "sub" / kept[0]).read_bytes() == b"B side!"
    # resume: a partial file is continued, not restarted
    (b / "big.bin").unlink(missing_ok=True)
    data = os.urandom(3 << 20)
    (a / "big.bin").write_bytes(data)
    (b / "big.bin.abp-part").write_bytes(data[:1 << 20])
    transfer.set_job("c", {"source": {"path": str(a)}, "dest": {"path": str(b)}, "mode": "copy"})
    r = transfer.run("c")
    assert (b / "big.bin").read_bytes() == data and r["bytes"] < len(data) + 100


def test_dashboard_api_end_to_end(nas, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as FTC

    from bot.dashboard import fileserver_api
    app = FastAPI()
    fileserver_api.register(app, lambda: None)
    c = FTC(app)
    o = c.get("/api/fileserver").json()
    assert o["array"]["configured"] and o["server"]["running"] is False
    assert c.post("/api/fileserver/shares", json={"name": "tv", "settings": {"cache": "no", "access": "secure"}}).status_code == 200
    assert c.post("/api/fileserver/shares", json={"name": "bad name!", "settings": {}}).status_code == 400
    assert c.post("/api/fileserver/users", json={"name": "ann", "password": "correct horse"}).status_code == 200
    assert [u["name"] for u in c.get("/api/fileserver/users").json()] == ["ann"]
    run = c.post("/api/fileserver/array/sync").json()
    for _ in range(100):
        got = c.get(f"/api/fileserver/runs/{run['run']}").json()
        if got["done"]:
            break
        time.sleep(0.05)
    assert got["done"] and got["error"] is None
    assert c.get("/api/fileserver/apps").json()[0]["id"]
    smb = c.get("/api/fileserver/shares/tv/smb", params={"platform": "windows"}).json()
    assert smb["commands"][0].startswith("New-SmbShare -Name 'tv'") and "-ReadAccess 'Everyone'" in smb["commands"][0]
    assert c.get("/api/fileserver/stats").json()["cpus"] >= 1


ROOT = Path(__file__).resolve().parent.parent


def test_the_storage_page_is_the_same_in_both_uis_and_wired():
    a = (ROOT / "bot/dashboard/static/storage-panel.js").read_text(encoding="utf-8")
    b = (ROOT / "desktop-app/ui/storage-panel.js").read_text(encoding="utf-8")
    assert a == b, "desktop-app/ui/storage-panel.js differs from bot/dashboard/static/storage-panel.js: copy it over"
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="storage"' in html and 'id="stp-root"' in html and "storage-panel.js" in html and 'href="#storage"' in html


def test_the_agents_tools_ask_before_changing_storage():
    from bot.agent_runtime import toolspec
    from bot.fileserver import tools  # noqa: F401
    for n in ("nas_share", "nas_array", "nas_job", "nas_link", "nas_guard"):
        assert toolspec.registered_dangerous(n), n
    for n in ("nas_status", "nas_disks", "nas_search", "nas_duplicates", "nas_locate"):
        assert toolspec.spec_for(n).origin == "registered" and not toolspec.registered_dangerous(n), n


def test_the_cli_parses_nas_commands():
    from abp_cli.__main__ import _parser
    p = _parser()
    for argv in (["nas", "status"], ["nas", "array", "set", "--disk", "d1=E:/d1", "--parity", "F:/p"], ["nas", "search", "tax", "2025"],
                 ["nas", "transfer", "set", "j", "--from", "C:/a", "--to", "remote:nas2/media", "--mode", "two-way"],
                 ["nas", "backup", "restore", "b", "snap", "C:/r"], ["nas", "app", "install", "jellyfin", "jf", "--mount", "media=share:media"]):
        assert p.parse_args(argv).nas_cmd == argv[1]
