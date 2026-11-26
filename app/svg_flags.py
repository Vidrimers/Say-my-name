"""Minimal SVG → PIL renderer (rect, circle, ellipse, line, path M/L) — no cairo."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw

_NS = "{http://www.w3.org/2000/svg}"


def _num(v: str, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _parse_style(el: ET.Element) -> dict:
    fill = el.get("fill", "#000000")
    stroke = el.get("stroke", "none")
    sw = _num(el.get("stroke-width", "1"))
    if fill.lower() == "none":
        fill = None
    if stroke.lower() == "none":
        stroke = None
    return {"fill": fill, "stroke": stroke, "sw": sw}


def _draw_path(d: str, draw: ImageDraw.ImageDraw, style: dict) -> None:
    # only M/L absolute/relative straight segments + implicit repeats
    tokens = re.findall(r"([MLHVZmlhvz])|(-?\d*\.?\d+)", d)
    cmds: list[tuple[str, list[float]]] = []
    i = 0
    cur_cmd = None
    nums: list[float] = []
    while i < len(tokens):
        tok, num = tokens[i]
        if tok:
            if cur_cmd and nums:
                cmds.append((cur_cmd, nums))
            cur_cmd = tok
            nums = []
        else:
            nums.append(_num(num))
        i += 1
    if cur_cmd and nums:
        cmds.append((cur_cmd, nums))

    pts: list[tuple[float, float]] = []
    cx = cy = 0.0
    for cmd, vals in cmds:
        if cmd in ("M", "L"):
            for j in range(0, len(vals) - 1, 2):
                cx, cy = vals[j], vals[j + 1]
                pts.append((cx, cy))
        elif cmd == "H":
            for x in vals:
                cx = x
                pts.append((cx, cy))
        elif cmd == "V":
            for y in vals:
                cy = y
                pts.append((cx, cy))
    if len(pts) >= 2 and style.get("stroke"):
        draw.line(pts, fill=style["stroke"], width=max(1, int(style["sw"])))


def svg_to_image(svg_path: Path, scale: int = 2) -> Image.Image:
    tree = ET.parse(svg_path)
    root = tree.getroot()
    w = int(_num(root.get("width", "48")))
    h = int(_num(root.get("height", "32")))
    img = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def S(*vals):
        return [v * scale for v in vals]

    for el in root:
        tag = el.tag.replace(_NS, "")
        st = _parse_style(el)
        if tag == "rect":
            x, y = _num(el.get("x")), _num(el.get("y"))
            rw, rh = _num(el.get("width")), _num(el.get("height"))
            box = S(x, y, x + rw, y + rh)
            if st["fill"]:
                draw.rectangle(box, fill=st["fill"])
            if st["stroke"]:
                draw.rectangle(box, outline=st["stroke"], width=max(1, int(st["sw"] * scale)))
        elif tag == "circle":
            cx, cy, r = _num(el.get("cx")), _num(el.get("cy")), _num(el.get("r"))
            box = S(cx - r, cy - r, cx + r, cy + r)
            if st["fill"]:
                draw.ellipse(box, fill=st["fill"])
            if st["stroke"]:
                draw.ellipse(box, outline=st["stroke"], width=max(1, int(st["sw"] * scale)))
        elif tag == "ellipse":
            cx = _num(el.get("cx"))
            cy = _num(el.get("cy"))
            rx, ry = _num(el.get("rx")), _num(el.get("ry"))
            box = S(cx - rx, cy - ry, cx + rx, cy + ry)
            if st["fill"]:
                draw.ellipse(box, fill=st["fill"])
            if st["stroke"]:
                draw.ellipse(box, outline=st["stroke"], width=max(1, int(st["sw"] * scale)))
        elif tag == "line":
            x1, y1, x2, y2 = _num(el.get("x1")), _num(el.get("y1")), _num(el.get("x2")), _num(el.get("y2"))
            if st["stroke"]:
                draw.line(S(x1, y1, x2, y2), fill=st["stroke"], width=max(1, int(st["sw"] * scale)))
        elif tag == "path":
            d = el.get("d", "")
            if st["stroke"]:
                # scale path numbers after parse — draw on scaled canvas via helper
                scale_pts(d, draw, st, scale)

    return img.resize((w, h), Image.Resampling.LANCZOS)


def scale_pts(d: str, draw: ImageDraw.ImageDraw, style: dict, scale: int) -> None:
    tokens = re.findall(r"([MLHVZmlhvz])|(-?\d*\.?\d+)", d)
    pts: list[tuple[float, float]] = []
    cx = cy = 0.0
    i = 0
    cmd = None
    while i < len(tokens):
        tok, num = tokens[i]
        if tok:
            cmd = tok
        else:
            v = _num(num)
            if cmd in ("M", "L"):
                # pair
                if i + 1 < len(tokens) and not tokens[i + 1][0]:
                    y = _num(tokens[i + 1][1])
                    cx, cy = v, y
                    pts.append((cx * scale, cy * scale))
                    i += 1
                else:
                    if cmd == "H":
                        cx = v
                        pts.append((cx * scale, cy * scale))
                    elif cmd == "V":
                        cy = v
                        pts.append((cx * scale, cy * scale))
            elif cmd == "H":
                cx = v
                pts.append((cx * scale, cy * scale))
            elif cmd == "V":
                cy = v
                pts.append((cx * scale, cy * scale))
        i += 1
    if len(pts) >= 2:
        draw.line(pts, fill=style["stroke"], width=max(1, int(style["sw"] * scale)))


def load_flag(name: str, size: tuple[int, int] = (24, 16)) -> Image.Image:
    svg = Path(__file__).parent / "assets" / "flags" / f"{name}.svg"
    if not svg.exists():
        raise FileNotFoundError(svg)
    img = svg_to_image(svg, scale=4)
    return img.resize(size, Image.Resampling.LANCZOS)


if __name__ == "__main__":
    out = Path(__file__).parent / "assets" / "flags"
    for n in ("ru", "gb", "auto"):
        im = load_flag(n, (48, 32))
        im.save(out / f"{n}.png")
        print(n, im.size, "ok")
