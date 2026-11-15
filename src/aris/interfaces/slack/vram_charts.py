"""Fixed, data-free VRAM label/percentage/bar charts; old bar URLs stay intact."""

from pathlib import Path

VRAM_CHART_SET = "627144a68df0"
VRAM_CHART_ROUTE = f"/slack/assets/vram-chart/{VRAM_CHART_SET}"
_DIRECTORY = Path(__file__).with_name("assets") / "vram_chart"
_FILES = {f"vram-{percent:03d}.png" for percent in range(101)} | {"vram-unknown.png"}


def chart_file(filename: str) -> Path | None:
    if filename not in _FILES:
        return None
    path = _DIRECTORY / filename
    return path if path.is_file() and not path.is_symlink() else None
