"""Making assets: icons and favicons, images and placeholders, conversions, colour palettes, charts, diagrams, QR codes,
badges, sprite sheets, sounds and text banners. Everything is written into the working folder.

Raster work uses Pillow; charts, badges and diagrams are generated as clean SVG (and rendered by Graphviz when it is
installed); sounds are synthesized straight to WAV.
"""
from __future__ import annotations

import colorsys
import html
import json
import math
import re
import struct
import wave
from pathlib import Path
from typing import Optional

from abp_toolkit.registry import ToolkitError, action, group
from abp_toolkit.util import inside, rel, run, which

group("asset", "Make assets: icons, favicons, images, conversions, palettes, charts, diagrams, QR codes, badges, sprites, sounds, banners")


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise ToolkitError("image work needs Pillow (pip install Pillow)") from None
    return Image, ImageDraw, ImageFont


def _out(workspace: Path, path: str, overwrite: bool) -> Path:
    p = inside(workspace, path)
    if p.exists() and not overwrite:
        raise ToolkitError(f"{path} already exists (overwrite=true replaces it)")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---- colour -------------------------------------------------------------------------------------------------------------
NAMED = {"black": "#000000", "white": "#ffffff", "red": "#ef4444", "orange": "#f97316", "amber": "#f59e0b",
         "yellow": "#eab308", "lime": "#84cc16", "green": "#22c55e", "emerald": "#10b981", "teal": "#14b8a6",
         "cyan": "#06b6d4", "sky": "#0ea5e9", "blue": "#3b82f6", "indigo": "#6366f1", "violet": "#8b5cf6",
         "purple": "#a855f7", "fuchsia": "#d946ef", "pink": "#ec4899", "rose": "#f43f5e", "slate": "#64748b",
         "gray": "#6b7280", "zinc": "#71717a", "stone": "#78716c", "transparent": "#00000000"}


def rgb(color: str) -> tuple[int, ...]:
    c = NAMED.get(color.lower(), color).lstrip("#")
    m = re.match(r"^rgba?\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\)$", color.strip())
    if m:
        a = m.group(4)
        return (int(m.group(1)), int(m.group(2)), int(m.group(3))) + ((round(float(a) * 255),) if a else ())
    if len(c) in (3, 4):
        c = "".join(ch * 2 for ch in c)
    if len(c) not in (6, 8) or not re.fullmatch(r"[0-9a-fA-F]+", c):
        raise ToolkitError(f"not a colour: {color!r} (use #rrggbb, rgb(r,g,b) or a name)")
    return tuple(int(c[i:i + 2], 16) for i in range(0, len(c), 2))


def hexc(t: tuple) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v))):02x}" for v in t[:3])


def luminance(t: tuple) -> float:
    def ch(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(t[0]) + 0.7152 * ch(t[1]) + 0.0722 * ch(t[2])


def contrast(a: tuple, b: tuple) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@action("asset.palette")
def palette(base: str, scheme: str = "analogous", count: int = 5) -> dict:
    """A colour palette from one colour: harmonies, a 50-950 shade scale, and WCAG contrast against white and black

    base: a colour (#3b82f6, rgb(59,130,246), or a name like blue)
    scheme: analogous, complementary, split_complementary, triadic, tetradic, monochrome
    count: colours in the harmony
    """
    r, g, b = rgb(base)[:3]
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    offsets = {"analogous": [i * 30 - 30 * (count // 2) for i in range(count)], "complementary": [0, 180],
               "split_complementary": [0, 150, 210], "triadic": [0, 120, 240], "tetradic": [0, 90, 180, 270],
               "monochrome": [0] * count}.get(scheme)
    if offsets is None:
        raise ToolkitError("scheme is analogous, complementary, split_complementary, triadic, tetradic or monochrome")
    harmony = []
    for i, off in enumerate(offsets):
        ll = l if scheme != "monochrome" else min(0.92, max(0.12, 0.2 + i * 0.65 / max(1, count - 1)))
        cr, cg, cb = colorsys.hls_to_rgb(((h * 360 + off) % 360) / 360, ll, s)
        harmony.append(hexc((cr * 255, cg * 255, cb * 255)))
    scale = {}
    for step, lightness in zip((50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950),
                               (0.97, 0.94, 0.86, 0.76, 0.64, 0.53, 0.45, 0.38, 0.31, 0.25, 0.15)):
        cr, cg, cb = colorsys.hls_to_rgb(h, lightness, min(1.0, s * (1.05 if lightness > 0.5 else 1.0)))
        scale[str(step)] = hexc((cr * 255, cg * 255, cb * 255))
    base_rgb = (r, g, b)
    return {"base": hexc(base_rgb), "hsl": [round(h * 360), round(s * 100), round(l * 100)], "harmony": harmony, "scale": scale,
            "contrast": {"on_white": round(contrast(base_rgb, (255, 255, 255)), 2), "on_black": round(contrast(base_rgb, (0, 0, 0)), 2),
                         "text_color": "#ffffff" if contrast(base_rgb, (255, 255, 255)) >= contrast(base_rgb, (0, 0, 0)) else "#000000"}}


@action("asset.contrast")
def contrast_check(foreground: str, background: str) -> dict:
    """WCAG contrast ratio of two colours, and which accessibility levels they pass

    foreground: text colour
    background: background colour
    """
    ratio = contrast(rgb(foreground)[:3], rgb(background)[:3])
    return {"ratio": round(ratio, 2), "AA_normal_text": ratio >= 4.5, "AA_large_text": ratio >= 3, "AAA_normal_text": ratio >= 7,
            "AAA_large_text": ratio >= 4.5, "ui_components": ratio >= 3}


# ---- icons, images -----------------------------------------------------------------------------------------------------
def _font(size: int):
    _Image, _Draw, ImageFont = _pil()
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def draw_icon(size: int, text: str, background: str, foreground: str, shape: str, gradient_to: str = ""):
    Image, ImageDraw, _F = _pil()
    scale = 4
    S = size * scale
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    fill = Image.new("RGBA", (S, S), rgb(background) + ((255,) if len(rgb(background)) == 3 else ()))
    if gradient_to:
        top, bottom = rgb(background)[:3], rgb(gradient_to)[:3]
        grad = Image.new("RGBA", (1, S))
        for y in range(S):
            t = y / max(1, S - 1)
            grad.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
        fill = grad.resize((S, S))
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    if shape == "circle":
        d.ellipse((0, 0, S - 1, S - 1), fill=255)
    elif shape == "rounded":
        d.rounded_rectangle((0, 0, S - 1, S - 1), radius=S // 5, fill=255)
    elif shape == "squircle":
        d.rounded_rectangle((0, 0, S - 1, S - 1), radius=S // 3, fill=255)
    elif shape == "hexagon":
        pts = [(S / 2 + S / 2 * math.cos(math.radians(60 * i - 30)), S / 2 + S / 2 * math.sin(math.radians(60 * i - 30))) for i in range(6)]
        d.polygon(pts, fill=255)
    elif shape == "square":
        d.rectangle((0, 0, S, S), fill=255)
    else:
        raise ToolkitError("shape is circle, rounded, squircle, hexagon or square")
    img.paste(fill, (0, 0), mask)
    if text:
        draw = ImageDraw.Draw(img)
        fsize = int(S * (0.56 if len(text) == 1 else 0.44 if len(text) == 2 else 0.32))
        font = _font(fsize)
        box = draw.textbbox((0, 0), text, font=font)
        w, h = box[2] - box[0], box[3] - box[1]
        draw.text(((S - w) / 2 - box[0], (S - h) / 2 - box[1]), text, font=font, fill=rgb(foreground)[:3] + (255,))
    return img.resize((size, size), Image.LANCZOS)


@action("asset.icon", writes=True)
def icon(workspace: Path, path: str, text: str = "", background: str = "#6366f1", foreground: str = "#ffffff",
         shape: str = "rounded", sizes: Optional[list[int]] = None, gradient_to: str = "", overwrite: bool = False) -> dict:
    """An app icon: a shape with a letter, initials or symbol, as PNG, ICO (multi-size) or SVG, chosen by the path's extension

    path: where to write it (.png, .ico or .svg) inside the working folder
    text: 1-3 characters (letters, digits or an emoji)
    background: fill colour
    foreground: text colour
    shape: circle, rounded, squircle, hexagon, square
    sizes: pixel sizes (ICO holds them all; PNG uses the largest)
    gradient_to: a second colour for a top-to-bottom gradient
    """
    out = _out(workspace, path, overwrite)
    sizes = sorted(set(sizes or ([16, 24, 32, 48, 64, 128, 256] if out.suffix.lower() == ".ico" else [512])))
    ext = out.suffix.lower()
    if ext == ".svg":
        s = 512
        shapes = {"circle": f'<circle cx="{s/2}" cy="{s/2}" r="{s/2}"/>', "rounded": f'<rect width="{s}" height="{s}" rx="{s/5}"/>',
                  "squircle": f'<rect width="{s}" height="{s}" rx="{s/3}"/>', "square": f'<rect width="{s}" height="{s}"/>',
                  "hexagon": '<polygon points="' + " ".join(f"{s/2 + s/2*math.cos(math.radians(60*i-30)):.1f},{s/2 + s/2*math.sin(math.radians(60*i-30)):.1f}" for i in range(6)) + '"/>'}
        if shape not in shapes:
            raise ToolkitError("shape is circle, rounded, squircle, hexagon or square")
        grad = (f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{background}"/>'
                f'<stop offset="1" stop-color="{gradient_to}"/></linearGradient></defs>') if gradient_to else ""
        fsize = s * (0.56 if len(text) == 1 else 0.44 if len(text) == 2 else 0.32)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s} {s}" width="{s}" height="{s}">{grad}'
               f'<g fill="{"url(#g)" if gradient_to else background}">{shapes[shape]}</g>'
               + (f'<text x="50%" y="50%" dominant-baseline="central" text-anchor="middle" font-family="Segoe UI, Helvetica, Arial, sans-serif" '
                  f'font-weight="700" font-size="{fsize:.0f}" fill="{foreground}">{html.escape(text)}</text>' if text else "") + "</svg>\n")
        out.write_text(svg, encoding="utf-8")
        return {"written": rel(workspace, out), "format": "svg"}
    images = [draw_icon(sz, text, background, foreground, shape, gradient_to) for sz in sizes]
    if ext == ".ico":
        images[-1].save(out, format="ICO", sizes=[(s, s) for s in sizes])
    elif ext in (".png", ".webp", ".bmp", ".gif", ".jpg", ".jpeg", ".icns"):
        img = images[-1]
        if ext in (".jpg", ".jpeg", ".bmp"):
            bg = _pil()[0].new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        img.save(out)
    else:
        raise ToolkitError("the path must end in .png, .ico, .svg, .webp, .icns, .jpg, .bmp or .gif")
    return {"written": rel(workspace, out), "sizes": sizes, "bytes": out.stat().st_size}


@action("asset.favicons", writes=True)
def favicons(workspace: Path, folder: str = "public", source: str = "", text: str = "", background: str = "#6366f1",
             foreground: str = "#ffffff", shape: str = "rounded", name: str = "App", overwrite: bool = False) -> dict:
    """A complete favicon set for a website: favicon.ico, 16/32 PNGs, apple-touch-icon, 192/512 PNGs, site.webmanifest, and the HTML to paste

    folder: where to write them inside the working folder
    source: an image to build them from (else a generated icon from text)
    name: the app's name for the manifest
    """
    Image, _D, _F = _pil()
    d = inside(workspace, folder)
    d.mkdir(parents=True, exist_ok=True)
    if source:
        base = Image.open(inside(workspace, source, must_exist=True)).convert("RGBA")
        side = min(base.size)
        base = base.crop(((base.width - side) // 2, (base.height - side) // 2, (base.width + side) // 2, (base.height + side) // 2))
        make = lambda s: base.resize((s, s), Image.LANCZOS)  # noqa: E731
    else:
        make = lambda s: draw_icon(s, text or name[:1].upper(), background, foreground, shape)  # noqa: E731
    files = {"favicon-16x16.png": 16, "favicon-32x32.png": 32, "apple-touch-icon.png": 180,
             "android-chrome-192x192.png": 192, "android-chrome-512x512.png": 512}
    written = []
    for fname, size in files.items():
        p = d / fname
        if p.exists() and not overwrite:
            raise ToolkitError(f"{rel(workspace, p)} already exists (overwrite=true replaces it)")
        img = make(size)
        if fname.startswith("apple"):
            bg = Image.new("RGBA", img.size, rgb(background)[:3] + (255,))
            bg.alpha_composite(img)
            img = bg
        img.save(p)
        written.append(rel(workspace, p))
    make(256).save(d / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    written.append(rel(workspace, d / "favicon.ico"))
    manifest = {"name": name, "short_name": name[:12], "icons": [
        {"src": "/android-chrome-192x192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/android-chrome-512x512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
        "theme_color": hexc(rgb(background)), "background_color": "#ffffff", "display": "standalone"}
    (d / "site.webmanifest").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    written.append(rel(workspace, d / "site.webmanifest"))
    snippet = ('<link rel="icon" href="/favicon.ico" sizes="any">\n'
               '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">\n'
               '<link rel="icon" type="image/png" sizes="16x16" href="/favicon-16x16.png">\n'
               '<link rel="apple-touch-icon" href="/apple-touch-icon.png">\n'
               f'<link rel="manifest" href="/site.webmanifest">\n<meta name="theme-color" content="{hexc(rgb(background))}">')
    return {"written": written, "html": snippet}


@action("asset.image", writes=True)
def image(workspace: Path, path: str, width: int = 1200, height: int = 630, background: str = "#1e293b",
          gradient_to: str = "", gradient: str = "vertical", text: str = "", text_color: str = "#ffffff",
          subtitle: str = "", noise: bool = False, overwrite: bool = False) -> dict:
    """An image: a placeholder, a social/OG card, a wallpaper or a gradient, with optional title and subtitle

    path: output file (.png, .jpg, .webp) inside the working folder
    width: pixels
    height: pixels
    background: colour
    gradient_to: second colour for a gradient
    gradient: vertical, horizontal, diagonal or radial
    text: a title (default: the size, for a placeholder)
    subtitle: a smaller second line
    noise: add a subtle grain
    """
    Image, ImageDraw, _F = _pil()
    if not (1 <= width <= 12000 and 1 <= height <= 12000):
        raise ToolkitError("width and height must be 1..12000")
    out = _out(workspace, path, overwrite)
    a = rgb(background)[:3]
    img = Image.new("RGB", (width, height), a)
    if gradient_to:
        b = rgb(gradient_to)[:3]
        small = Image.new("RGB", (256, 256))
        px = small.load()
        for y in range(256):
            for x in range(256):
                t = {"vertical": y / 255, "horizontal": x / 255, "diagonal": (x + y) / 510,
                     "radial": min(1.0, math.hypot(x - 127.5, y - 127.5) / 180)}.get(gradient)
                if t is None:
                    raise ToolkitError("gradient is vertical, horizontal, diagonal or radial")
                px[x, y] = tuple(round(c1 + (c2 - c1) * t) for c1, c2 in zip(a, b))
        img = small.resize((width, height), Image.BICUBIC)
    if noise:
        import random
        grain = Image.effect_noise((width, height), 18).convert("RGB")
        img = Image.blend(img, grain, 0.06)
        random.seed(0)
    draw = ImageDraw.Draw(img)
    title = text or (f"{width} × {height}" if not subtitle else "")
    if title:
        fsize = max(12, min(width // max(6, len(title) // 2 + 2), height // 4))
        font = _font(fsize)
        box = draw.textbbox((0, 0), title, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        y = (height - th) / 2 - (th * 0.45 if subtitle else 0)
        draw.text(((width - tw) / 2 - box[0], y - box[1]), title, font=font, fill=rgb(text_color)[:3])
        if subtitle:
            sfont = _font(max(10, fsize // 2))
            sb = draw.textbbox((0, 0), subtitle, font=sfont)
            draw.text(((width - (sb[2] - sb[0])) / 2 - sb[0], y + th * 1.35), subtitle, font=sfont,
                      fill=tuple(round(c * 0.8 + 255 * 0.2 * 0) for c in rgb(text_color)[:3]))
    img.save(out, quality=92) if out.suffix.lower() in (".jpg", ".jpeg", ".webp") else img.save(out)
    return {"written": rel(workspace, out), "width": width, "height": height, "bytes": out.stat().st_size}


@action("asset.convert", writes=True)
def convert(workspace: Path, source: str, path: str, width: int = 0, height: int = 0, fit: str = "contain",
            quality: int = 88, grayscale: bool = False, rotate: int = 0, flip: str = "", strip_metadata: bool = True,
            overwrite: bool = False) -> dict:
    """Convert, resize, crop, rotate or recolour an image (PNG, JPEG, WebP, GIF, BMP, TIFF, ICO), keeping transparency where the format allows

    source: the input image inside the working folder
    path: the output file (its extension picks the format)
    width: target width (0 = keep the ratio from height, or the original)
    height: target height
    fit: contain (fit inside), cover (fill and crop), stretch, or pad (fit and pad to exactly width x height)
    quality: JPEG/WebP quality 1-100
    rotate: degrees clockwise
    flip: horizontal or vertical
    """
    Image, _D, _F = _pil()
    src = inside(workspace, source, must_exist=True)
    out = _out(workspace, path, overwrite)
    img = Image.open(src)
    orig = img.size
    img = img.convert("RGBA") if img.mode in ("P", "LA", "RGBA") or "transparency" in img.info else img.convert("RGB")
    if rotate:
        img = img.rotate(-rotate, expand=True)
    if flip == "horizontal":
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    elif flip == "vertical":
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    if width or height:
        w = width or round(img.width * height / img.height)
        h = height or round(img.height * width / img.width)
        if fit == "stretch":
            img = img.resize((w, h), Image.LANCZOS)
        elif fit == "cover":
            scale = max(w / img.width, h / img.height)
            img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
            left, top = (img.width - w) // 2, (img.height - h) // 2
            img = img.crop((left, top, left + w, top + h))
        elif fit in ("contain", "pad"):
            img.thumbnail((w, h), Image.LANCZOS) if (img.width > w or img.height > h) else None
            if img.width < w and img.height < h and fit == "contain":
                scale = min(w / img.width, h / img.height)
                img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
            if fit == "pad":
                canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
                img = canvas
        else:
            raise ToolkitError("fit is contain, cover, stretch or pad")
    if grayscale:
        img = img.convert("LA" if img.mode == "RGBA" else "L")
    ext = out.suffix.lower()
    if ext in (".jpg", ".jpeg", ".bmp") and img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[3])
        img = bg
    kwargs = {"quality": quality} if ext in (".jpg", ".jpeg", ".webp") else {}
    if ext == ".ico":
        kwargs = {"sizes": [(s, s) for s in (16, 32, 48, 64, 128, 256) if s <= max(img.size)]}
    if ext == ".png":
        kwargs["optimize"] = True
    if not strip_metadata and "exif" in Image.open(src).info:
        kwargs["exif"] = Image.open(src).info["exif"]
    img.save(out, **kwargs)
    return {"written": rel(workspace, out), "from": list(orig), "to": list(img.size), "bytes": out.stat().st_size,
            "source_bytes": src.stat().st_size}


@action("asset.inspect")
def inspect_image(workspace: Path, path: str, palette_colors: int = 6) -> dict:
    """What an image is: format, size, mode, DPI, transparency, frames, EXIF, and its dominant colours

    path: an image inside the working folder
    palette_colors: how many dominant colours to extract
    """
    Image, _D, _F = _pil()
    p = inside(workspace, path, must_exist=True)
    img = Image.open(p)
    info = {"format": img.format, "width": img.width, "height": img.height, "mode": img.mode, "bytes": p.stat().st_size,
            "dpi": img.info.get("dpi"), "frames": getattr(img, "n_frames", 1),
            "has_alpha": img.mode in ("RGBA", "LA") or "transparency" in img.info}
    try:
        exif = img.getexif()
        from PIL.ExifTags import TAGS
        info["exif"] = {TAGS.get(k, str(k)): str(v)[:100] for k, v in exif.items()} if exif else {}
    except Exception:  # noqa: BLE001
        info["exif"] = {}
    small = img.convert("RGB").resize((96, 96))
    q = small.quantize(colors=max(1, min(palette_colors, 16)))
    pal = q.getpalette()
    counts = sorted(q.getcolors(), reverse=True)
    info["dominant_colors"] = [{"color": hexc(tuple(pal[i * 3:i * 3 + 3])), "share": round(c / (96 * 96), 3)} for c, i in counts]
    return info


# ---- SVG charts ----------------------------------------------------------------------------------------------------------
CHART_COLORS = ["#6366f1", "#22c55e", "#f59e0b", "#ef4444", "#06b6d4", "#a855f7", "#ec4899", "#84cc16", "#f97316", "#64748b"]


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * mag:
            return m * mag
    return 10 * mag


def _fmt(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 or v == int(v) else f"{v:.2g}"


@action("asset.chart", writes=True)
def chart(workspace: Path, path: str, kind: str, labels: list, series: dict, title: str = "", width: int = 800,
          height: int = 450, dark: bool = False, overwrite: bool = False) -> dict:
    """A chart as a clean, scalable SVG: bar, stacked_bar, horizontal_bar, line, area, pie, donut or scatter

    path: output .svg inside the working folder
    kind: bar, stacked_bar, horizontal_bar, line, area, pie, donut, scatter
    labels: the category (x) labels, or x values for scatter
    series: {name: [values]} (pie and donut use the first series)
    title: a title above the chart
    dark: dark background
    """
    out = _out(workspace, path, overwrite)
    if out.suffix.lower() != ".svg":
        raise ToolkitError("charts are written as .svg")
    fg, grid, bg = ("#e2e8f0", "#334155", "#0f172a") if dark else ("#1e293b", "#e2e8f0", "#ffffff")
    names = list(series)
    if not names:
        raise ToolkitError("series needs at least one {name: [values]}")
    vals = {k: [float(x) for x in v] for k, v in series.items()}
    W, H = width, height
    top = 50 if title else 20
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="Segoe UI, Helvetica, Arial, sans-serif">',
             f'<rect width="{W}" height="{H}" fill="{bg}"/>']
    if title:
        parts.append(f'<text x="{W/2}" y="30" text-anchor="middle" font-size="18" font-weight="600" fill="{fg}">{html.escape(title)}</text>')
    if kind in ("pie", "donut"):
        data = vals[names[0]]
        total = sum(data) or 1
        cx, cy, r = W * 0.38, (H + top) / 2, min(W * 0.3, (H - top) / 2 - 20)
        ang = -math.pi / 2
        for i, (lab, v) in enumerate(zip(labels, data)):
            a2 = ang + 2 * math.pi * v / total
            large = 1 if a2 - ang > math.pi else 0
            x1, y1, x2, y2 = cx + r * math.cos(ang), cy + r * math.sin(ang), cx + r * math.cos(a2), cy + r * math.sin(a2)
            color = CHART_COLORS[i % len(CHART_COLORS)]
            if v >= total:
                parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>')
            else:
                parts.append(f'<path d="M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} A{r:.1f},{r:.1f} 0 {large} 1 {x2:.1f},{y2:.1f} Z" fill="{color}" stroke="{bg}" stroke-width="2"/>')
            ly = top + 20 + i * 24
            parts.append(f'<rect x="{W*0.72}" y="{ly-11}" width="14" height="14" rx="3" fill="{color}"/>'
                         f'<text x="{W*0.72+22}" y="{ly}" font-size="13" fill="{fg}">{html.escape(str(lab))} — {_fmt(v)} ({100*v/total:.1f}%)</text>')
            ang = a2
        if kind == "donut":
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r*0.55}" fill="{bg}"/><text x="{cx}" y="{cy+6}" text-anchor="middle" font-size="18" font-weight="600" fill="{fg}">{_fmt(total)}</text>')
    else:
        left, right, bottom = 64, 20 + (120 if len(names) > 1 else 0), 50
        pw, ph = W - left - right, H - top - bottom
        n = len(labels)
        if kind == "stacked_bar":
            vmax = _nice_max(max(sum(vals[k][i] for k in names if i < len(vals[k])) for i in range(n)))
            vmin = 0.0
        elif kind == "scatter":
            vmax = _nice_max(max(max(v) for v in vals.values()))
            vmin = min(0.0, min(min(v) for v in vals.values()))
        else:
            vmax = _nice_max(max(max(v) for v in vals.values()))
            vmin = min(0.0, min(min(v) for v in vals.values()))
        horizontal = kind == "horizontal_bar"
        for t in range(6):
            v = vmin + (vmax - vmin) * t / 5
            if horizontal:
                x = left + pw * t / 5
                parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top+ph}" stroke="{grid}"/><text x="{x:.1f}" y="{top+ph+18}" text-anchor="middle" font-size="11" fill="{fg}">{_fmt(v)}</text>')
            else:
                y = top + ph - ph * t / 5
                parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left+pw}" y2="{y:.1f}" stroke="{grid}"/><text x="{left-8}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="{fg}">{_fmt(v)}</text>')
        ymap = lambda v: top + ph - ph * (v - vmin) / ((vmax - vmin) or 1)  # noqa: E731
        if kind == "scatter":
            xs = [float(x) for x in labels]
            xmin, xmax = min(xs), max(xs)
            xmap = lambda v: left + pw * (v - xmin) / ((xmax - xmin) or 1)  # noqa: E731
            for si, name in enumerate(names):
                for x, y in zip(xs, vals[name]):
                    parts.append(f'<circle cx="{xmap(x):.1f}" cy="{ymap(y):.1f}" r="4" fill="{CHART_COLORS[si % 10]}" fill-opacity=".8"/>')
            for t in range(6):
                v = xmin + (xmax - xmin) * t / 5
                parts.append(f'<text x="{xmap(v):.1f}" y="{top+ph+18}" text-anchor="middle" font-size="11" fill="{fg}">{_fmt(v)}</text>')
        else:
            band = (ph if horizontal else pw) / max(1, n)
            for i, lab in enumerate(labels):
                if horizontal:
                    parts.append(f'<text x="{left-8}" y="{top + band*i + band/2 + 4:.1f}" text-anchor="end" font-size="11" fill="{fg}">{html.escape(str(lab))[:18]}</text>')
                else:
                    parts.append(f'<text x="{left + band*i + band/2:.1f}" y="{top+ph+18}" text-anchor="middle" font-size="11" fill="{fg}">{html.escape(str(lab))[:14]}</text>')
            if kind in ("bar", "horizontal_bar"):
                bw = band * 0.8 / len(names)
                for si, name in enumerate(names):
                    for i, v in enumerate(vals[name]):
                        if horizontal:
                            y = top + band * i + band * 0.1 + bw * si
                            parts.append(f'<rect x="{left}" y="{y:.1f}" width="{pw * (v - vmin) / ((vmax - vmin) or 1):.1f}" height="{bw:.1f}" rx="3" fill="{CHART_COLORS[si % 10]}"/>')
                        else:
                            x = left + band * i + band * 0.1 + bw * si
                            parts.append(f'<rect x="{x:.1f}" y="{ymap(max(v, 0)):.1f}" width="{bw:.1f}" height="{abs(ymap(v) - ymap(0)):.1f}" rx="3" fill="{CHART_COLORS[si % 10]}"><title>{html.escape(name)}: {_fmt(v)}</title></rect>')
            elif kind == "stacked_bar":
                bw = band * 0.7
                base = [0.0] * n
                for si, name in enumerate(names):
                    for i, v in enumerate(vals[name]):
                        x = left + band * i + band * 0.15
                        parts.append(f'<rect x="{x:.1f}" y="{ymap(base[i] + v):.1f}" width="{bw:.1f}" height="{ymap(base[i]) - ymap(base[i] + v):.1f}" fill="{CHART_COLORS[si % 10]}"/>')
                        base[i] += v
            elif kind in ("line", "area"):
                for si, name in enumerate(names):
                    pts = [(left + band * i + band / 2, ymap(v)) for i, v in enumerate(vals[name])]
                    path_d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
                    color = CHART_COLORS[si % 10]
                    if kind == "area" and pts:
                        parts.append(f'<path d="{path_d} L{pts[-1][0]:.1f},{ymap(max(0, vmin)):.1f} L{pts[0][0]:.1f},{ymap(max(0, vmin)):.1f} Z" fill="{color}" fill-opacity=".18"/>')
                    parts.append(f'<path d="{path_d}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round"/>')
                    parts.extend(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>' for x, y in pts)
            else:
                raise ToolkitError("kind is bar, stacked_bar, horizontal_bar, line, area, pie, donut or scatter")
        if len(names) > 1:
            for si, name in enumerate(names):
                ly = top + 10 + si * 22
                parts.append(f'<rect x="{W-right+16}" y="{ly-10}" width="12" height="12" rx="3" fill="{CHART_COLORS[si % 10]}"/><text x="{W-right+34}" y="{ly}" font-size="12" fill="{fg}">{html.escape(name)[:14]}</text>')
    parts.append("</svg>\n")
    out.write_text("".join(parts), encoding="utf-8")
    return {"written": rel(workspace, out), "kind": kind, "series": len(names), "points": len(labels)}


# ---- diagrams ------------------------------------------------------------------------------------------------------------
@action("asset.diagram", writes=True, needs=["dot"])
def diagram(workspace: Path, path: str, edges: list, direction: str = "LR", title: str = "", node_labels: Optional[dict] = None,
            overwrite: bool = False) -> dict:
    """A box-and-arrow diagram from edges: SVG or PNG through Graphviz when installed, a built-in layered SVG otherwise; also returns Mermaid and DOT text

    path: output .svg, .png or .dot inside the working folder
    edges: [[from, to], ...] or [[from, to, label], ...]
    direction: LR (left to right) or TB (top to bottom)
    node_labels: {node: "display label"}
    """
    out = _out(workspace, path, overwrite)
    labels = node_labels or {}
    nodes: list[str] = []
    for e in edges:
        for n in (str(e[0]), str(e[1])):
            if n not in nodes:
                nodes.append(n)
    q = lambda s: '"' + str(s).replace('"', '\\"') + '"'  # noqa: E731
    dot = ["digraph G {", f"  rankdir={direction};", '  node [shape=box, style="rounded,filled", fillcolor="#eef2ff", color="#6366f1", fontname="Helvetica"];',
           '  edge [color="#64748b", fontname="Helvetica", fontsize=10];']
    if title:
        dot.append(f"  labelloc=t; label={q(title)};")
    dot += [f"  {q(n)} [label={q(labels.get(n, n))}];" for n in nodes]
    dot += [f"  {q(e[0])} -> {q(e[1])}" + (f" [label={q(e[2])}]" if len(e) > 2 else "") + ";" for e in edges]
    dot.append("}")
    dot_text = "\n".join(dot)
    mid = {n: f"n{i}" for i, n in enumerate(nodes)}
    mermaid = "flowchart " + direction + "\n" + "\n".join(f"  {mid[n]}[\"{labels.get(n, n)}\"]" for n in nodes) + "\n" + "\n".join(
        f"  {mid[str(e[0])]} -->" + (f"|{e[2]}|" if len(e) > 2 else "") + f" {mid[str(e[1])]}" for e in edges)
    ext = out.suffix.lower()
    if ext == ".dot":
        out.write_text(dot_text, encoding="utf-8")
        return {"written": rel(workspace, out), "mermaid": mermaid}
    if which("dot"):
        r = run([which("dot"), f"-T{ext[1:]}", "-o", out], input=dot_text, timeout=60)
        if r.ok:
            return {"written": rel(workspace, out), "renderer": "graphviz", "mermaid": mermaid, "dot": dot_text}
    if ext != ".svg":
        raise ToolkitError("PNG diagrams need Graphviz (winget install graphviz); use .svg for the built-in renderer")
    # Built-in: layers by longest path from the roots, boxes placed evenly in each layer.
    preds = {n: [str(e[0]) for e in edges if str(e[1]) == n] for n in nodes}
    layer: dict[str, int] = {}

    def depth(n, seen=()):
        if n in layer:
            return layer[n]
        if n in seen:
            return 0
        layer[n] = 1 + max((depth(p, seen + (n,)) for p in preds[n]), default=-1)
        return layer[n]
    for n in nodes:
        depth(n)
    layers: dict[int, list[str]] = {}
    for n in nodes:
        layers.setdefault(layer[n], []).append(n)
    bw, bh, gx, gy = 150, 44, 70, 30
    horiz = direction == "LR"
    maxn = max(len(v) for v in layers.values())
    W = (len(layers) * (bw + gx) + 40) if horiz else (maxn * (bw + gx) + 40)
    H = (maxn * (bh + gy) + 60) if horiz else (len(layers) * (bh + gy * 2) + 60)
    pos = {}
    for li, ns in layers.items():
        for i, n in enumerate(ns):
            off = (maxn - len(ns)) / 2
            pos[n] = (20 + li * (bw + gx), 40 + (i + off) * (bh + gy)) if horiz else (20 + (i + off) * (bw + gx), 40 + li * (bh + gy * 2))
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="Segoe UI, Helvetica, Arial, sans-serif">',
           '<defs><marker id="a" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#64748b"/></marker></defs>',
           '<rect width="100%" height="100%" fill="#ffffff"/>']
    if title:
        svg.append(f'<text x="{W/2}" y="24" text-anchor="middle" font-size="16" font-weight="600" fill="#1e293b">{html.escape(title)}</text>')
    for e in edges:
        (x1, y1), (x2, y2) = pos[str(e[0])], pos[str(e[1])]
        a = (x1 + bw, y1 + bh / 2) if horiz else (x1 + bw / 2, y1 + bh)
        b = (x2, y2 + bh / 2) if horiz else (x2 + bw / 2, y2)
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        c = f"C{mx:.1f},{a[1]:.1f} {mx:.1f},{b[1]:.1f}" if horiz else f"C{a[0]:.1f},{my:.1f} {b[0]:.1f},{my:.1f}"
        svg.append(f'<path d="M{a[0]:.1f},{a[1]:.1f} {c} {b[0]:.1f},{b[1]:.1f}" fill="none" stroke="#64748b" stroke-width="1.5" marker-end="url(#a)"/>')
        if len(e) > 2:
            svg.append(f'<text x="{mx:.1f}" y="{my - 4:.1f}" text-anchor="middle" font-size="10" fill="#475569">{html.escape(str(e[2]))}</text>')
    for n, (x, y) in pos.items():
        svg.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" rx="8" fill="#eef2ff" stroke="#6366f1"/>'
                   f'<text x="{x + bw/2}" y="{y + bh/2 + 5}" text-anchor="middle" font-size="13" fill="#1e293b">{html.escape(str(labels.get(n, n)))[:22]}</text>')
    svg.append("</svg>\n")
    out.write_text("".join(svg), encoding="utf-8")
    return {"written": rel(workspace, out), "renderer": "builtin", "mermaid": mermaid, "dot": dot_text}


# ---- QR, badges, sprites, banners ----------------------------------------------------------------------------------------
@action("asset.qr", writes=True)
def qr(workspace: Path, path: str, data: str, error_correction: str = "M", box_size: int = 10, border: int = 4,
       foreground: str = "#000000", background: str = "#ffffff", overwrite: bool = False) -> dict:
    """A QR code (PNG or SVG) for a URL, text, Wi-Fi login (WIFI:T:WPA;S:name;P:pass;;), contact card...

    path: output .png or .svg inside the working folder
    data: what it encodes
    error_correction: L, M, Q or H (H survives a logo over the middle)
    """
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_H, ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q
    except ImportError:
        raise ToolkitError("QR codes need the qrcode package (pip install qrcode)") from None
    out = _out(workspace, path, overwrite)
    level = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "Q": ERROR_CORRECT_Q, "H": ERROR_CORRECT_H}.get(error_correction.upper())
    if level is None:
        raise ToolkitError("error_correction is L, M, Q or H")
    q = qrcode.QRCode(error_correction=level, box_size=box_size, border=border)
    q.add_data(data)
    q.make(fit=True)
    matrix = q.get_matrix()
    if out.suffix.lower() == ".svg":
        n = len(matrix)
        rects = "".join(f'<rect x="{x}" y="{y}" width="1" height="1"/>' for y, row in enumerate(matrix) for x, v in enumerate(row) if v)
        out.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n*box_size}" height="{n*box_size}" shape-rendering="crispEdges">'
                       f'<rect width="{n}" height="{n}" fill="{background}"/><g fill="{foreground}">{rects}</g></svg>\n', encoding="utf-8")
    else:
        q.make_image(fill_color=hexc(rgb(foreground)), back_color=hexc(rgb(background))).save(out)
    return {"written": rel(workspace, out), "version": q.version, "modules": len(matrix)}


@action("asset.badge", writes=True)
def badge(workspace: Path, path: str, label: str, message: str, color: str = "#22c55e", label_color: str = "#555555",
          overwrite: bool = False) -> dict:
    """A README badge (shields.io style) as SVG: label | message

    path: output .svg inside the working folder
    label: the left part (build, coverage, version...)
    message: the right part (passing, 92%, v1.2.0...)
    color: the right part's colour
    """
    out = _out(workspace, path, overwrite)
    cw = lambda s: 6.5 * len(s) + 10  # noqa: E731
    lw, mw = cw(label), cw(message)
    w = lw + mw
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="20" role="img" aria-label="{html.escape(label)}: {html.escape(message)}">'
           f'<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>'
           f'<clipPath id="r"><rect width="{w:.0f}" height="20" rx="3" fill="#fff"/></clipPath><g clip-path="url(#r)">'
           f'<rect width="{lw:.0f}" height="20" fill="{label_color}"/><rect x="{lw:.0f}" width="{mw:.0f}" height="20" fill="{color}"/>'
           f'<rect width="{w:.0f}" height="20" fill="url(#s)"/></g><g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
           f'<text x="{lw/2:.1f}" y="15" fill="#010101" fill-opacity=".3">{html.escape(label)}</text><text x="{lw/2:.1f}" y="14">{html.escape(label)}</text>'
           f'<text x="{lw + mw/2:.1f}" y="15" fill="#010101" fill-opacity=".3">{html.escape(message)}</text><text x="{lw + mw/2:.1f}" y="14">{html.escape(message)}</text></g></svg>\n')
    out.write_text(svg, encoding="utf-8")
    return {"written": rel(workspace, out), "markdown": f"![{label}]({rel(workspace, out)})"}


@action("asset.sprite_sheet", writes=True)
def sprite_sheet(workspace: Path, images: list[str], path: str, columns: int = 0, padding: int = 2, overwrite: bool = False) -> dict:
    """Pack images into one sprite sheet (PNG) and return each one's rectangle, plus ready-to-use CSS

    images: image files inside the working folder
    path: the output .png
    columns: images per row (0 = square-ish)
    padding: pixels between images
    """
    Image, _D, _F = _pil()
    if not images:
        raise ToolkitError("give at least one image")
    imgs = [(Path(i).stem, Image.open(inside(workspace, i, must_exist=True)).convert("RGBA")) for i in images]
    cols = columns or math.ceil(math.sqrt(len(imgs)))
    cw = max(i.width for _, i in imgs) + padding
    ch = max(i.height for _, i in imgs) + padding
    rows = math.ceil(len(imgs) / cols)
    sheet = Image.new("RGBA", (cols * cw, rows * ch), (0, 0, 0, 0))
    frames = {}
    css = []
    for n, (name, im) in enumerate(imgs):
        x, y = (n % cols) * cw, (n // cols) * ch
        sheet.paste(im, (x, y))
        frames[name] = {"x": x, "y": y, "w": im.width, "h": im.height}
        css.append(f".sprite-{re.sub(r'[^a-zA-Z0-9_-]', '-', name)} {{ width: {im.width}px; height: {im.height}px; background-position: -{x}px -{y}px; }}")
    out = _out(workspace, path, overwrite)
    sheet.save(out, optimize=True)
    css.insert(0, f"[class^=\"sprite-\"] {{ background-image: url({out.name}); background-repeat: no-repeat; display: inline-block; }}")
    return {"written": rel(workspace, out), "size": list(sheet.size), "frames": frames, "css": "\n".join(css)}


@action("asset.sound", writes=True)
def sound(workspace: Path, path: str, notes: Optional[list] = None, waveform: str = "sine", duration: float = 0.5,
          frequency: float = 440.0, volume: float = 0.6, sample_rate: int = 44100, attack: float = 0.01,
          release: float = 0.08, noise: bool = False, overwrite: bool = False) -> dict:
    """A sound effect or melody as a WAV file: a tone, a sequence of notes, or noise, with a simple envelope

    path: output .wav inside the working folder
    notes: a sequence like ["C5:0.1", "E5:0.1", "G5:0.2", "rest:0.1"] (note or Hz, then seconds)
    waveform: sine, square, sawtooth or triangle
    duration: seconds, for a single tone
    frequency: Hz, for a single tone
    volume: 0-1
    noise: white noise instead of a tone
    """
    out = _out(workspace, path, overwrite)
    if out.suffix.lower() != ".wav":
        raise ToolkitError("sounds are written as .wav")
    if waveform not in ("sine", "square", "sawtooth", "triangle"):
        raise ToolkitError("waveform is sine, square, sawtooth or triangle")
    import random
    names = {"C": -9, "D": -7, "E": -5, "F": -4, "G": -2, "A": 0, "B": 2}

    def hz(tok: str) -> float:
        if tok == "rest":
            return 0.0
        m = re.fullmatch(r"([A-Ga-g])([#b]?)(\d)", tok)
        if m:
            semis = names[m.group(1).upper()] + {"#": 1, "b": -1, "": 0}[m.group(2)] + (int(m.group(3)) - 4) * 12
            return 440.0 * 2 ** (semis / 12)
        return float(tok)
    seq = [(hz(n.split(":")[0]), float(n.split(":")[1]) if ":" in n else 0.2) for n in notes] if notes else [(frequency, duration)]
    if sum(d for _, d in seq) > 300:
        raise ToolkitError("at most 5 minutes of sound")
    frames = bytearray()
    for f, d in seq:
        count = int(sample_rate * d)
        for i in range(count):
            t = i / sample_rate
            if noise:
                v = random.uniform(-1, 1)
            elif f == 0:
                v = 0.0
            else:
                ph = (t * f) % 1.0
                v = {"sine": math.sin(2 * math.pi * ph), "square": 1.0 if ph < 0.5 else -1.0,
                     "sawtooth": 2 * ph - 1, "triangle": 4 * abs(ph - 0.5) - 1}[waveform]
            env = min(1.0, t / attack if attack else 1.0, (d - t) / release if release else 1.0)
            frames += struct.pack("<h", int(max(-1, min(1, v * env * volume)) * 32767))
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))
    return {"written": rel(workspace, out), "seconds": round(sum(d for _, d in seq), 3), "bytes": out.stat().st_size}


@action("asset.banner")
def banner(text: str, width: int = 0, fill: str = "█") -> dict:
    """Big text made of characters, for CLI splash screens and comments

    text: the text (short)
    width: character columns (0 = automatic)
    fill: the character to draw with
    """
    Image, ImageDraw, _F = _pil()
    font = _font(40)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    box = probe.textbbox((0, 0), text, font=font)
    img = Image.new("L", (box[2] - box[0] + 4, box[3] - box[1] + 4), 0)
    ImageDraw.Draw(img).text((2 - box[0], 2 - box[1]), text, font=font, fill=255)
    cols = width or min(120, img.width // 3)
    rows = max(3, round(img.height * cols / img.width / 2))
    small = img.resize((cols, rows))
    lines = ["".join(fill if small.getpixel((x, y)) > 110 else " " for x in range(cols)).rstrip() for y in range(rows)]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return {"banner": "\n".join(lines)}
