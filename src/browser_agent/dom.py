"""DOM state — clean HTML of the visible viewport."""

from dataclasses import dataclass


@dataclass
class DOMState:
    """Parsed DOM state from the browser extension."""

    html: str
    url: str
    title: str
    scroll_pct: int

    @classmethod
    def from_raw(cls, raw: dict) -> "DOMState":
        scroll = raw.get("scroll", {})
        total_h = scroll.get("height", 1)
        viewport = scroll.get("viewport", 0)
        scroll_top = scroll.get("top", 0)
        if total_h <= viewport:
            scroll_pct = 100
        else:
            scroll_pct = round(((scroll_top + viewport) / total_h) * 100)

        return cls(
            html=raw.get("html", ""),
            url=raw.get("url", ""),
            title=raw.get("title", ""),
            scroll_pct=scroll_pct,
        )

    def format_for_llm(self) -> str:
        """Page HTML with metadata header."""
        scroll_hint = "more content below" if self.scroll_pct < 95 else "near bottom"
        header = (
            f"Page: {self.title}\n"
            f"URL: {self.url}\n"
            f"Scroll: {self.scroll_pct}% ({scroll_hint})\n\n"
        )
        return header + self.html
