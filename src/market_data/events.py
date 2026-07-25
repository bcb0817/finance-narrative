from __future__ import annotations

import re


EVENT_PATTERNS = {
    "earnings": (r"\bearnings\b", r"\bquarter(?:ly)? results?\b", r"決算"),
    "partnership": (r"\bpartnership\b", r"\bcollaboration\b", r"提携"),
    "product": (r"\blaunch(?:es|ed)?\b", r"\bnew product\b", r"製品"),
    "management": (r"\bappoint(?:s|ed)?\b", r"\bceo\b", r"人事"),
    "guidance": (r"\bguidance\b", r"\boutlook\b", r"業績見通し"),
    "acquisition": (r"\bacqui(?:re|res|red|sition)\b", r"\bmerger\b", r"買収"),
    "regulation": (r"\bregulat", r"\blawsuit\b", r"\bsec\b", r"規制", r"訴訟"),
    "capital_allocation": (r"\bbuyback\b", r"\bdividend\b", r"自社株", r"配当"),
}


def classify_official_release(title: str, summary: str = "") -> str:
    value = f"{title} {summary}".lower()
    for category, patterns in EVENT_PATTERNS.items():
        if any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns):
            return category
    return "other"


def release_fact_record(
    *, symbol: str, title: str, url: str, published_at: str, summary: str = "",
) -> dict[str, str]:
    return {
        "symbol": symbol, "event_type": classify_official_release(title, summary),
        "title": title[:240], "url": url, "published_at": published_at,
        "summary": summary[:500], "source_type": "official_release",
    }
