"""The parity array on real files: sync, scrub finding bitrot, rebuilding one lost disk (P, or Q alone), any two lost
disks (P + Q), files read from parity while their disk is gone, changes and deletions keeping parity exact, removing
a disk. Small blocks keep it quick; the arithmetic is the same at any size."""
from __future__ import annotations

import hashlib
import os
import random
import shutil
from pathlib import Path

import numpy as np
import pytest

from bot.fileserver import array, gf
from bot.fileserver.store import FsError


@pytest.fixture
def arr(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_FILESERVER_DIR", str(tmp_path / "state"))
    disks = []
    for i in range(3):
        d = tmp_path / f"disk{i + 1}"
        d.mkdir()
        disks.append(d)
    rnd = random.Random(7)
    files = {}
    for i, d in enumerate(disks):
        for j in range(6):
            rel = f"{'media/' if j % 2 else ''}f{i}{j}.bin"
            data = rnd.randbytes(rnd.choice([0, 1, 100, 64 * 1024, 64 * 1024 + 3, 200_000, 333_333]))
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_bytes(data)
            files[(f"d{i + 1}", rel)] = hashlib.sha256(data).hexdigest()
    array.configure([{"name": f"d{i + 1}", "path": str(d)} for i, d in enumerate(disks)],
                    [{"path": str(tmp_path / "p1")}, {"path": str(tmp_path / "p2")}], block_kib=64, allow_same_drive=True)
    return tmp_path, disks, files


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _check_all(disks, files):
    for (disk, rel), want in files.items():
        assert _digest(disks[int(disk[1]) - 1] / rel) == want, (disk, rel)


def test_galois_field_rebuilds_any_one_or_two_blocks():
    rng = np.random.default_rng(3)
    D = [rng.integers(0, 256, 1000, dtype=np.uint8) for _ in range(6)]
    idx = [0, 1, 2, 5, 9, 200]
    p, q = gf.parity(D, idx, True)
    for x in range(6):
        others = [D[i] for i in range(6) if i != x]
        oidx = [idx[i] for i in range(6) if i != x]
        assert np.array_equal(gf.recover_one_p(p, others), D[x])
        assert np.array_equal(gf.recover_one_q(q, others, oidx, idx[x]), D[x])
        for y in range(x + 1, 6):
            rest = [i for i in range(6) if i not in (x, y)]
            dx, dy = gf.recover_two(p, q, [D[i] for i in rest], [idx[i] for i in rest], idx[x], idx[y])
            assert np.array_equal(dx, D[x]) and np.array_equal(dy, D[y])


def test_sync_scrub_and_parity_stay_exact_through_changes(arr):
    tmp, disks, files = arr
    s = array.sync()
    assert s["added"] == 18 and s["stripes"] > 0
    assert array.scrub(100)["parity_errors"] == 0
    st = array.status(scan_changes=True)
    assert st["configured"] and st["dual_parity"] and all(d["unsynced"] == 0 for d in st["disks"]) and st["pending_parity"] == 0
    # change, grow, shrink, delete, add
    (disks[0] / "f00.bin").write_bytes(b"x" * 150_000)
    (disks[1] / "media/f11.bin").unlink()
    (disks[2] / "new.bin").write_bytes(os.urandom(90_000))
    (disks[2] / "f20.bin").write_bytes(b"")
    s = array.sync()
    assert (s["added"], s["changed"], s["removed"]) == (1, 2, 1)
    r = array.scrub(100)
    assert r["parity_errors"] == 0 and r["data_errors"] == 0
    assert array.sync()["stripes"] == 0                      # nothing changed: nothing to do


def test_bitrot_is_found_by_scrub_and_repaired_by_fix(arr):
    tmp, disks, files = arr
    array.sync()
    victim = max((f for f in disks[1].rglob("*") if f.is_file()), key=lambda f: f.stat().st_size)
    mtime = victim.stat().st_mtime_ns
    data = bytearray(victim.read_bytes())
    data[len(data) // 2] ^= 0xFF                                   # one flipped byte, timestamp unchanged: bitrot
    victim.write_bytes(bytes(data))
    os.utime(victim, ns=(mtime, mtime))
    r = array.scrub(100)
    assert r["data_errors"] == 1 and r["parity_errors"] == 0
    assert array.status()["errors"][0]["path"] == victim.relative_to(disks[1]).as_posix()
    out = array.fix()
    assert out["rebuilt"] == 1 and not out["failed"]
    _check_all(disks, files)
    assert array.scrub(100)["data_errors"] == 0 and array.status()["errors"] == []


def test_a_lost_disk_is_served_from_parity_then_rebuilt_on_a_new_drive(arr):
    tmp, disks, files = arr
    array.sync()
    shutil.rmtree(disks[1])                                  # the drive died
    st = array.status()
    assert [d["present"] for d in st["disks"]] == [True, False, True] and any("missing" in w for w in st["warnings"])
    with pytest.raises(FsError, match="cannot be found"):
        array.sync()
    got = b"".join(array.emulate("d2", "media/f13.bin"))
    assert hashlib.sha256(got).hexdigest() == files[("d2", "media/f13.bin")]
    new = tmp / "replacement"
    new.mkdir()
    out = array.fix(disk="d2", target=str(new))
    assert out["rebuilt"] == 6 and not out["failed"]
    disks[1] = new
    _check_all(disks, files)
    assert array.config()["disks"][1]["path"] == str(new)
    assert array.sync()["added"] == 0 and array.scrub(100)["parity_errors"] == 0


def test_two_lost_disks_rebuild_with_dual_parity_and_one_with_q_alone(arr):
    tmp, disks, files = arr
    array.sync()
    shutil.rmtree(disks[0])
    shutil.rmtree(disks[2])
    for i in (0, 2):
        disks[i].mkdir()
    assert array.fix()["rebuilt"] == 12
    _check_all(disks, files)
    # P lost as well as a data disk: Q alone rebuilds it
    (tmp / "p1" / array.PARITY_FILES["P"]).unlink()
    (disks[1] / "f10.bin").unlink()
    assert array.fix(disk="d2")["rebuilt"] == 1
    _check_all(disks, files)
    assert array.sync()["stripes"] > 0                       # the missing P file is rebuilt in full
    assert array.scrub(100)["parity_errors"] == 0


def test_more_losses_than_parity_are_refused_honestly(arr):
    tmp, disks, files = arr
    array.configure([{"name": f"d{i + 1}", "path": str(d)} for i, d in enumerate(disks)],
                    [{"path": str(tmp / "p1")}], block_kib=64, allow_same_drive=True)   # single parity now
    array.sync()
    for i in (0, 1):                                      # two disks lost, one parity: shared stripes cannot come back
        shutil.rmtree(disks[i])
        disks[i].mkdir()
    out = array.fix()
    assert out["failed"] and all("more than the parity can rebuild" in f["error"] for f in out["failed"])
    for f in out["failed"]:
        assert not (disks[int(f["disk"][1]) - 1] / f["path"]).exists()     # nothing half-written is left behind
    for (disk, rel), want in files.items():               # what could be rebuilt is exact
        f = disks[int(disk[1]) - 1] / rel
        if f.exists():
            assert _digest(f) == want


def test_configuration_is_checked_and_removing_a_disk_drops_it_from_parity(arr):
    tmp, disks, files = arr
    with pytest.raises(FsError, match="inside a data disk"):
        array.configure([{"name": "d1", "path": str(disks[0])}], [{"path": str(disks[0] / "par")}], block_kib=64, allow_same_drive=True)
    with pytest.raises(FsError, match="same drive"):
        array.configure([{"name": "d1", "path": str(disks[0])}], [{"path": str(tmp / "p1")}], block_kib=64)
    with pytest.raises(FsError, match="one parity disk"):
        array.configure([{"name": "d1", "path": str(disks[0])}], [], block_kib=64, allow_same_drive=True)
    array.sync()
    idx = {d["name"]: d["index"] for d in array.config()["disks"]}
    array.configure([{"name": "d1", "path": str(disks[0])}, {"name": "d3", "path": str(disks[2])}],
                    [{"path": str(tmp / "p1")}, {"path": str(tmp / "p2")}], block_kib=64, allow_same_drive=True)
    assert {d["name"]: d["index"] for d in array.config()["disks"]} == {"d1": idx["d1"], "d3": idx["d3"]}
    assert array.status()["pending_parity"] > 0
    array.sync()
    assert array.scrub(100)["parity_errors"] == 0
    shutil.rmtree(disks[2])
    disks[2].mkdir()
    assert array.fix()["rebuilt"] == 6
    for (disk, rel), want in files.items():
        if disk != "d2":
            assert _digest(disks[int(disk[1]) - 1] / rel) == want
