"""Glossy circular button sprites for compact mode."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def _sphere(size: int, base: tuple[int, int, int], glow: tuple[int, int, int]) -> Image.Image:
    """Vertical-ish glossy sphere with highlight and soft shadow rim."""
    # supersample for smooth edges
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    margin = ss
    box = [margin, margin, s - margin - 1, s - margin - 1]

    # outer soft shadow ring
    shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse(box, fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=2 * ss))
    img = Image.alpha_composite(img, shadow)
    d = ImageDraw.Draw(img)

    # main body — radial-ish via stacked ellipses (lighter top-left)
    d.ellipse(box, fill=base + (255,))

    # darker bottom rim
    rim = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rim)
    rd.ellipse(box, outline=tuple(max(0, c - 50) for c in base) + (200,), width=2 * ss)
    img = Image.alpha_composite(img, rim)
    d = ImageDraw.Draw(img)

    # top-left gloss highlight
    hx0 = int(s * 0.18)
    hy0 = int(s * 0.12)
    hx1 = int(s * 0.72)
    hy1 = int(s * 0.48)
    highlight = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    hd.ellipse([hx0, hy0, hx1, hy1], fill=glow + (160,))
    highlight = highlight.filter(ImageFilter.GaussianBlur(radius=1.5 * ss))
    # clip highlight to circle
    mask = Image.new("L", (s, s), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse(box, fill=255)
    highlight.putalpha(Image.composite(highlight.split()[3], Image.new("L", (s, s), 0), mask))
    img = Image.alpha_composite(img, highlight)

    # small sharp specular dot
    d = ImageDraw.Draw(img)
    cx, cy = int(s * 0.34), int(s * 0.28)
    r = int(s * 0.08)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 210))

    # bottom inner glow (bounce light)
    bounce = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bounce)
    bd.ellipse(
        [int(s * 0.25), int(s * 0.62), int(s * 0.75), int(s * 0.88)],
        fill=tuple(min(255, c + 40) for c in base) + (90,),
    )
    bounce = bounce.filter(ImageFilter.GaussianBlur(radius=2 * ss))
    mask = Image.new("L", (s, s), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse(box, fill=255)
    bounce.putalpha(Image.composite(bounce.split()[3], Image.new("L", (s, s), 0), mask))
    img = Image.alpha_composite(img, bounce)

    return img.resize((size, size), Image.Resampling.LANCZOS)


def make_sprites(out_dir: Path, size: int = 96) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}

    idle = _sphere(size, base=(230, 69, 83), glow=(255, 180, 190))
    p_idle = out_dir / "btn_idle.png"
    idle.save(p_idle)
    paths["idle"] = p_idle

    rec = _sphere(size, base=(255, 90, 110), glow=(255, 210, 220))
    p_rec = out_dir / "btn_rec.png"
    rec.save(p_rec)
    paths["rec"] = p_rec

    hot = _sphere(size, base=(255, 120, 140), glow=(255, 230, 235))
    p_hot = out_dir / "btn_hot.png"
    hot.save(p_hot)
    paths["hot"] = p_hot

    return paths


if __name__ == "__main__":
    root = Path(__file__).parent / "assets"
    out = make_sprites(root)
    for k, v in out.items():
        print(k, v)
