"""Parity arithmetic for the array, on numpy byte arrays: P = XOR of the data blocks; Q = sum of g^d * D_d in GF(2^8)
(polynomial 0x11d, generator 2 — the RAID-6 code). With P and Q any two lost blocks of a stripe can be rebuilt."""
from __future__ import annotations

import numpy as np

EXP = np.zeros(512, dtype=np.uint8)
LOG = np.zeros(256, dtype=np.int32)
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
EXP[255:510] = EXP[0:255]


def mul_scalar(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return int(EXP[(LOG[a] + LOG[b]) % 255])


def inv(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("0 has no inverse in GF(256)")
    return int(EXP[(255 - LOG[a]) % 255])


def gpow(d: int) -> int:
    """g^d."""
    return int(EXP[d % 255])


def mul(vec: np.ndarray, c: int) -> np.ndarray:
    """Every byte of vec times the constant c."""
    if c == 0:
        return np.zeros_like(vec)
    if c == 1:
        return vec.copy()
    out = EXP[LOG[vec] + int(LOG[c])]
    out[vec == 0] = 0
    return out


def parity(blocks: list[np.ndarray], indexes: list[int], want_q: bool) -> tuple[np.ndarray, np.ndarray | None]:
    """P (and Q) over the blocks of one stripe (or many stripes at once: blocks may be 2-D); indexes = each block's
    disk index (its exponent in Q)."""
    p = np.zeros_like(blocks[0])
    q = np.zeros_like(blocks[0]) if want_q else None
    for b, d in zip(blocks, indexes):
        p ^= b
        if want_q:
            q ^= mul(b, gpow(d))
    return p, q


def recover_one_p(p: np.ndarray, others: list[np.ndarray]) -> np.ndarray:
    out = p.copy()
    for b in others:
        out ^= b
    return out


def recover_one_q(q: np.ndarray, others: list[np.ndarray], other_idx: list[int], lost: int) -> np.ndarray:
    acc = q.copy()
    for b, d in zip(others, other_idx):
        acc ^= mul(b, gpow(d))
    return mul(acc, inv(gpow(lost)))


def recover_two(p: np.ndarray, q: np.ndarray, others: list[np.ndarray], other_idx: list[int], x: int, y: int
                ) -> tuple[np.ndarray, np.ndarray]:
    """Two lost data blocks x != y from P, Q and the rest:  Pxy = Dx ^ Dy,  Qxy = g^x Dx ^ g^y Dy
    => Dx = (Qxy ^ g^y Pxy) / (g^x ^ g^y),  Dy = Pxy ^ Dx."""
    pxy, qxy = p.copy(), q.copy()
    for b, d in zip(others, other_idx):
        pxy ^= b
        qxy ^= mul(b, gpow(d))
    gx, gy = gpow(x), gpow(y)
    dx = mul(qxy ^ mul(pxy, gy), inv(gx ^ gy))
    return dx, pxy ^ dx
