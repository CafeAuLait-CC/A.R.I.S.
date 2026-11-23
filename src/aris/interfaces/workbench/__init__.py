"""Shared shell for the authenticated member and usage pages."""

from base64 import b64encode
from pathlib import Path

ASSETS = Path(__file__).with_name("static")


def page_shell(html: str, *, enabled: bool = True) -> str:
    opening = (ASSETS / "shell.html").read_text() if enabled else ""
    return html.replace("<!-- workbench-open -->", opening, 1).replace(
        "<!-- workbench-close -->", "</div></div>" if enabled else "", 1
    )


def stylesheet(*, portrait: bool = False) -> str:
    css = (ASSETS / "workbench.css").read_text()
    if portrait:
        image = b64encode((ASSETS / "aris-portrait.webp").read_bytes()).decode("ascii")
        css += f'\n:root {{ --aris-portrait: url("data:image/webp;base64,{image}"); }}\n'
    return css


def script() -> str:
    return (ASSETS / "workbench.js").read_text()
