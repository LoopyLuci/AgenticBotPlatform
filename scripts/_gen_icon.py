"""Generates a placeholder app icon (pure stdlib, no Pillow) so `cargo tauri
icon` has a source image to derive the full icon set from. Replace
icon-source.png with real artwork any time and re-run `cargo tauri icon`."""
import struct
import zlib

SIZE = 512
BG = (42, 120, 214)   # brand accent blue
FG = (255, 255, 255)


def chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def make_png(path: str) -> None:
    rows = []
    margin = SIZE // 5
    for y in range(SIZE):
        row = bytearray([0])  # filter type 0
        for x in range(SIZE):
            in_mark = margin < x < SIZE - margin and margin < y < SIZE - margin
            r, g, b = FG if in_mark and (x + y) % 37 < 20 else BG
            row += bytes((r, g, b, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)
    compressed = zlib.compress(raw, 9)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    png = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


if __name__ == "__main__":
    make_png(r"Z:\Projects\AgenticBotPlatform\desktop-app\src-tauri\icon-source.png")
    print("wrote icon-source.png")
