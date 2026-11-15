"""Generate 256px VRAM charts. Pillow is pinned and development-only.

Run ./scripts/uv run --no-sync python scripts/generate_slack_vram_charts.py
or append --check. Uses Pillow's embedded Aileron subset, never system fonts.
Font source: Pillow 12.0.0 src/PIL/ImageFont.py (MIT-CMU; Aileron metadata
states No Rights Reserved), https://dotcolon.net/fonts/aileron .
"""

import argparse
import hashlib
import math
import re
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src/aris/interfaces/slack/vram_charts.py"
DIRECTORY = MODULE.with_name("assets") / "vram_chart"
BARS = MODULE.with_name("assets") / "vram"
WIDTH, HEIGHT, BAR_Y, SCALE = 256, 46, 30, 4
PADDING, TEXT_Y = 8, 6


def label(text: str, *, bold: bool = False) -> Image.Image:
    canvas = Image.new("RGBA", (160 * SCALE, 24 * SCALE))
    ImageDraw.Draw(canvas).text(
        (4, 4),
        text,
        font=ImageFont.load_default(size=14 * SCALE),
        fill=(117, 117, 117, 255),
        stroke_width=1 if bold else 0,
    )
    cropped = canvas.crop(canvas.getbbox())
    resized = cropped.resize(
        (math.ceil(cropped.width / SCALE), math.ceil(cropped.height / SCALE)),
        Image.Resampling.LANCZOS,
    )
    return resized.crop(resized.getbbox())


def chart_bytes(percent: int | None) -> bytes:
    canvas = Image.new("RGBA", (WIDTH, HEIGHT))
    left = label("VRAM")
    right = label("Unknown" if percent is None else f"{percent}%", bold=True)
    canvas.alpha_composite(left, (PADDING, TEXT_Y))
    # Crop measured ink, not the font's advance width: the visible rightmost
    # percentage pixel and the PNG bar both end at x=247. Transparent safety
    # margins keep Slack's native rounded image border away from the glyphs.
    canvas.alpha_composite(right, (WIDTH - PADDING - right.width, TEXT_Y))
    name = "vram-unknown.png" if percent is None else f"vram-{percent:03d}.png"
    with Image.open(BARS / name) as bar:
        scaled = bar.convert("RGBA").resize((WIDTH - 2 * PADDING, 10), Image.Resampling.LANCZOS)
        canvas.alpha_composite(scaled, (PADDING, BAR_Y))
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    assets = {f"vram-{p:03d}.png": chart_bytes(p) for p in range(101)}
    assets["vram-unknown.png"] = chart_bytes(None)
    digest = hashlib.sha256()
    for name, data in sorted(assets.items()):
        digest.update(name.encode("ascii") + b"\0" + data)
    version = digest.hexdigest()[:12]
    original = MODULE.read_text()
    updated, count = re.subn(
        r'VRAM_CHART_SET = "[a-f0-9]{12}"', f'VRAM_CHART_SET = "{version}"', original
    )
    if count != 1:
        raise SystemExit("Expected one chart version constant")
    if args.check:
        if original != updated or {p.name for p in DIRECTORY.glob("*.png")} != set(assets):
            raise SystemExit("Chart set/version differs; regenerate charts")
        if any((DIRECTORY / name).read_bytes() != data for name, data in assets.items()):
            raise SystemExit("Chart bytes differ; regenerate charts")
    else:
        DIRECTORY.mkdir(parents=True, exist_ok=True)
        for name, data in assets.items():
            (DIRECTORY / name).write_bytes(data)
        MODULE.write_text(updated)
    print(f"{'Verified' if args.check else 'Generated'} 102 charts at {WIDTH}x{HEIGHT}; {version}")


if __name__ == "__main__":
    main()
