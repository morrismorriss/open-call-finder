"""Small helpers shared by the rest of the code."""
from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

# Query parameters that only exist for tracking and never change the page.
TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid",
    "ref", "ref_src", "r", "triedredirect", "publication_id",
    "post_id", "isfreemail", "_hsenc", "_hsmi", "mkt_tok", "last_resource_guid",
}


def normalize_url(url: str | None) -> str | None:
    """Make two links to the same page look identical.

    Lowercases the host, forces https, drops "www.", fragments, trailing
    slashes and tracking parameters (utm_*, fbclid, Substack's ?r=, ...).
    """
    if not url:
        return None
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        return url
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "form"]):
        tag.decompose()
    # Keep link addresses (as "text (url)") so the AI can find the real apply link.
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("http") and href not in a.get_text():
            a.append(f" ({href})")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def parse_iso_date(value) -> date | None:
    """Accept '2026-12-01', '2026-12-01T00:00:00Z', etc. Return None if unparseable."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|"
    "november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_DEADLINE_RE = re.compile(
    r"(?:deadline|due|submit by|apply by|closes|closing date)[^.]{0,40}?"
    rf"(({_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|\d{{1,2}}/\d{{1,2}}/\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})",
    re.I,
)


def guess_deadline(text: str) -> date | None:
    """Best-effort deadline finder used only when the AI is unavailable."""
    m = _DEADLINE_RE.search(text or "")
    if not m:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", m.group(1)):
        return parse_iso_date(m.group(1))
    raw = re.sub(r"(\d)(st|nd|rd|th)", r"\1", m.group(1)).replace(",", "").replace(".", "")
    raw = raw.replace("Sept ", "Sep ").replace("sept ", "sep ")
    for fmt in ("%B %d %Y", "%b %d %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw.title() if "/" not in raw else raw, fmt).date()
        except ValueError:
            continue
    return None


def clip(text: str | None, limit: int) -> str:
    """Notion text fields max out at 2000 characters."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


_ID_RE = re.compile(
    r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}"  # Notion IDs
    r"|projects/\d+"                                                       # Google project numbers
    r"|AIza[0-9A-Za-z_-]{20,}|AQ\.[0-9A-Za-z_.-]{20,}"                     # Google API keys
    r"|(?:ntn|secret)_[0-9A-Za-z]{20,}",                                     # Notion tokens
    re.I,
)


def redact(text) -> str:
    """Hide IDs and keys before anything goes into a (possibly public) log."""
    return _ID_RE.sub("[hidden]", str(text))
