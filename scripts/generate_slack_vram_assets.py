"""Generate data-free PNG bars and their immutable content version, without dependencies.

Run with ./scripts/uv run --no-sync python scripts/generate_slack_vram_assets.py.
Add --check to verify the tracked assets and version without writing files.
Slack displays these images at their intrinsic size, so use actual CSS-sized
pixels rather than a 2x raster that would be clipped in a narrow GPU container.
"""

import argparse
import hashlib
import math
import re
import struct
import zlib
from pathlib import Path

WIDTH, HEIGHT, SAMPLES = 256, 10, 4
TRACK = (128, 128, 128, 56)
COLORS = {
    "green": (42, 156, 119, 255),
    "yellow": (196, 150, 38, 255),
    "red": (213, 91, 101, 255),
}
ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src/aris/interfaces/slack/vram_assets.py"
DIRECTORY = MODULE.with_name("assets") / "vram"


def inside_capsule(x: float, y: float, width: float) -> bool:
    radius = min(HEIGHT, width) / 2
    dx = max(radius - x, 0, x - (width - radius))
    dy = max(radius - y, 0, y - (HEIGHT - radius))
    return 0 <= x <= width and dx * dx + dy * dy <= radius * radius


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png_bytes(percent: int | None) -> bytes:
    """Antialiased RGBA capsule; unknown is only the neutral translucent track."""
    if percent is not None and not 0 <= percent <= 100:
        raise ValueError("Expected an integer percentage from 0 to 100, or None")
    fill_width = 0 if percent is None else math.floor(WIDTH * percent / 100 + 0.5)
    band = "red" if percent is not None and percent >= 90 else "yellow"
    if percent is None or percent < 70:
        band = "green"
    fill = COLORS[band]
    rows = bytearray()
    for y in range(HEIGHT):
        rows.append(0)  # PNG filter: None.
        for x in range(WIDTH):
            alpha = 0
            channels = [0, 0, 0]
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    px, py = x + (sx + 0.5) / SAMPLES, y + (sy + 0.5) / SAMPLES
                    color = None
                    if fill_width and inside_capsule(px, py, fill_width):
                        color = fill
                    elif inside_capsule(px, py, WIDTH):
                        color = TRACK
                    if color:
                        alpha += color[3]
                        for channel in range(3):
                            channels[channel] += color[channel] * color[3]
            rows.extend(round(value / alpha) if alpha else 0 for value in channels)
            rows.append(round(alpha / SAMPLES**2))
    header = struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + png_chunk(b"IEND", b"")
    )


def build_assets() -> dict[str, bytes]:
    return {f"vram-{percent:03d}.png": png_bytes(percent) for percent in range(101)} | {
        "vram-unknown.png": png_bytes(None)
    }


def asset_set_digest(assets: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, data in sorted(assets.items()):
        digest.update(name.encode("ascii") + b"\0" + data)
    return digest.hexdigest()[:12]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    assets = build_assets()
    version = asset_set_digest(assets)
    module = MODULE.read_text()
    updated, count = re.subn(
        r'VRAM_ASSET_SET = "[a-f0-9]{12}"', f'VRAM_ASSET_SET = "{version}"', module
    )
    if count != 1:
        raise SystemExit("Expected one VRAM asset version constant")
    if args.check:
        if module != updated or {p.name for p in DIRECTORY.glob("*.png")} != set(assets):
            raise SystemExit("VRAM asset set/version differs; regenerate assets")
        if any((DIRECTORY / name).read_bytes() != data for name, data in assets.items()):
            raise SystemExit("VRAM asset bytes differ; regenerate assets")
    else:
        DIRECTORY.mkdir(parents=True, exist_ok=True)
        for name, data in assets.items():
            (DIRECTORY / name).write_bytes(data)
        MODULE.write_text(updated)
    action = "Verified" if args.check else "Generated"
    print(f"{action} {len(assets)} PNGs at {WIDTH}x{HEIGHT}; asset set {version}")


if __name__ == "__main__":
    main()
