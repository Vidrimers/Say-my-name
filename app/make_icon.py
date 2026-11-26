"""Generate app icon: side head, open mouth, sound waves → app.ico"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def make_icon(path: Path, size: int = 256) -> None:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256.0

    # Background circle
    bg = (30, 30, 46, 255)
    accent = (137, 180, 250, 255)  # waves
    skin = (205, 214, 244, 255)
    mouth = (243, 139, 168, 255)

    pad = 12 * s
    d.ellipse([pad, pad, size - pad, size - pad], fill=bg)

    # Side-profile head (facing right), simplified
    # Head outline as polygon/ellipse combo
    cx, cy = 110 * s, 128 * s
    # skull
    d.ellipse([50 * s, 40 * s, 150 * s, 170 * s], fill=skin)
    # face bump (nose area) — right side
    d.polygon(
        [
            (130 * s, 90 * s),
            (165 * s, 120 * s),
            (135 * s, 130 * s),
        ],
        fill=skin,
    )
    # chin / jaw
    d.ellipse([70 * s, 120 * s, 150 * s, 190 * s], fill=skin)
    # cut left side flat (profile look) — redraw bg on left
    d.rectangle([40 * s, 40 * s, 78 * s, 200 * s], fill=(0, 0, 0, 0))
    # re-fill circle left edge carefully: simpler head as rounded shape
    # Rebuild cleaner profile:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([pad, pad, size - pad, size - pad], fill=bg)

    # Profile head path (facing right)
    head = [
        (70 * s, 50 * s),   # top
        (130 * s, 55 * s),
        (155 * s, 95 * s),  # forehead
        (170 * s, 115 * s),  # nose tip
        (150 * s, 125 * s),  # under nose
        (155 * s, 140 * s),  # lips area
        (145 * s, 155 * s),  # chin
        (120 * s, 175 * s),
        (85 * s, 180 * s),   # jaw back
        (65 * s, 150 * s),
        (60 * s, 100 * s),
        (70 * s, 50 * s),
    ]
    d.polygon(head, fill=skin)

    # Open mouth (side view) — dark red ellipse on face
    d.ellipse([138 * s, 128 * s, 162 * s, 152 * s], fill=mouth)
    # inner mouth darker
    d.ellipse([143 * s, 133 * s, 158 * s, 147 * s], fill=(180, 60, 90, 255))
    # tongue hint
    d.ellipse([147 * s, 140 * s, 157 * s, 150 * s], fill=(250, 140, 150, 255))

    # Eye
    d.ellipse([115 * s, 95 * s, 128 * s, 108 * s], fill=(30, 30, 46, 255))
    d.ellipse([119 * s, 99 * s, 125 * s, 105 * s], fill=(205, 214, 244, 255))

    # Sound waves to the right of mouth
    wave_color = accent
    for i, r in enumerate((14, 24, 34)):
        bbox = [
            165 * s - r * s * 0.3,
            140 * s - r * s,
            165 * s + r * s,
            140 * s + r * s,
        ]
        # arc on the right side (from -60 to 60 deg approx)
        d.arc(bbox, start=-55, end=55, fill=wave_color, width=max(2, int(4 * s)))

    # ensure icon size
    if path.parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    # multi-size ico
    img.save(path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    out = Path(__file__).parent / "app.ico"
    make_icon(out)
    print(f"wrote {out}")
