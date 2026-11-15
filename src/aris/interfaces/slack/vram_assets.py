"""Pre-rendered, data-free VRAM bars shared by all nodes and viewers."""

from pathlib import Path

VRAM_ASSET_SET = "5cdcfb515c70"
VRAM_ASSET_ROUTE = f"/slack/assets/vram/{VRAM_ASSET_SET}"
_DIRECTORY = Path(__file__).with_name("assets") / "vram"
_FILES = {f"vram-{percent:03d}.png" for percent in range(101)} | {"vram-unknown.png"}


def asset_name(percent: int | None) -> str:
    return "vram-unknown.png" if percent is None else f"vram-{percent:03d}.png"


def asset_file(filename: str) -> Path | None:
    if filename not in _FILES:
        return None
    path = _DIRECTORY / filename
    return path if path.is_file() and not path.is_symlink() else None
