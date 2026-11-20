"""Browser URL layouts; deployment hostnames never belong in source code."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserPaths:
    account: str = "/internal/members"
    home: str = "/internal/members/ui"
    member_assets: str = "/internal/members/ui/assets"
    commands: str = "/internal/commands"
    cancel: str = "withdraw"
    usage: str = "/internal/usage"
    usage_page: str = "/internal/usage/ui"
    usage_assets: str = "/internal/usage/ui/assets"
    cookie: str = "/internal"

    def render(self, text: str) -> str:
        replacements = (
            ("/internal/members/ui/assets", self.member_assets),
            ("/internal/usage/ui/assets", self.usage_assets),
            ("/internal/members/ui", self.home),
            ("/internal/usage/ui", self.usage_page),
            ("/internal/members", self.account),
            ("/internal/commands", self.commands),
            ("/internal/usage", self.usage),
        )
        for before, after in replacements:
            text = text.replace(before, after)
        return text


INTERNAL_PATHS = BrowserPaths()
PUBLIC_PATHS = BrowserPaths(
    account="/account",
    home="/",
    member_assets="/assets",
    commands="/requests",
    cancel="cancel",
    usage="/usage",
    usage_page="/usage",
    usage_assets="/assets/usage",
    cookie="/",
)
