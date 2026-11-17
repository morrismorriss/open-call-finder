"""Reading sources: RSS/Atom feeds, Substack newsletters and plain web pages."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup

from .util import html_to_text, normalize_url

USER_AGENT = "OpenCallFinder/1.0 (personal digest of artist open calls; visits once a day)"

# Words that show up when a post is cut off for non-subscribers.
PAYWALL_MARKERS = (
    "this post is for paid subscribers",
    "this post is for paying subscribers",
    "subscribe to keep reading",
    "upgrade to paid to read",
    "become a paid subscriber to read",
    "this content is for members only",
    "you must be logged in to view",
    "log in to view this content",
    "members only content",
)


@dataclass
class Post:
    source: str
    title: str
    url: str
    published: datetime | None
    text: str
    tags: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return normalize_url(self.url) or self.url


class SkipSource(Exception):
    """Raised when a whole source is deliberately skipped (robots.txt, login wall)."""


class PoliteClient:
    """requests wrapper: honest user agent, robots.txt checks, delay per host."""

    def __init__(self, delay_seconds: float = 2.0, timeout: int = 25):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.delay = delay_seconds
        self.timeout = timeout
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}

    def _robots_for(self, url: str):
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=self.timeout)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                elif r.status_code in (401, 403):
                    rp.disallow_all = True
                else:  # 404 etc. means "no rules"
                    rp.parse([])
            except requests.RequestException:
                rp.parse([])  # can't read robots.txt: treat as no rules
            self._robots[base] = rp
        return self._robots[base]

    def allowed(self, url: str) -> bool:
        return self._robots_for(url).can_fetch(USER_AGENT, url)

    def get(self, url: str) -> requests.Response:
        if not self.allowed(url):
            raise SkipSource(f"robots.txt asks bots not to read {url}")
        host = urlsplit(url).netloc
        # Honour the site's own Crawl-delay if it asks for more (capped at 60s).
        delay = max(self.delay, min(float(self._robots_for(url).crawl_delay(USER_AGENT) or 0), 60))
        wait = delay - (time.time() - self._last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        last_err = None
        for attempt in range(3):
            try:
                r = self.session.get(url, timeout=self.timeout)
                self._last_hit[host] = time.time()
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                if r.status_code in (401, 403):
                    raise SkipSource(f"{url} needs a login or blocks bots (HTTP {r.status_code})")
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                last_err = e
                time.sleep(3 * (attempt + 1))
        raise last_err  # type: ignore[misc]


def looks_paywalled(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in PAYWALL_MARKERS)


def feed_url_for(src: dict) -> str:
    url = src["url"].rstrip("/")
    if src.get("type") == "substack" and not url.endswith("/feed"):
        return url + "/feed"
    return src.get("feed_url") or url


def _entry_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def read_feed(client: PoliteClient, src: dict, since: datetime,
              seen: set[str], max_posts: int) -> list[Post]:
    resp = client.get(feed_url_for(src))
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"not a readable feed: {feed.bozo_exception}")
    posts = []
    for e in feed.entries:
        published = _entry_date(e)
        if published and published < since and not src.get("ignore_dates"):
            continue
        html = ""
        if e.get("content"):
            html = " ".join(c.get("value", "") for c in e.content)
        html = html or e.get("summary", "")
        text = html_to_text(html)
        # Substack marks paid-only posts; skip them.
        if src.get("type") == "substack" and (
            e.get("audience") in ("only_paid", "founding") or looks_paywalled(text)
        ):
            continue
        if looks_paywalled(text) and len(text) < 1500:
            continue
        link = e.get("link", "")
        title = html_to_text(e.get("title", ""))
        title_kw = [k.lower() for k in (src.get("title_keywords") or [])]
        if title_kw and not any(k in title.lower() for k in title_kw):
            continue  # don't even open posts the title filter would drop
        if src.get("fetch_full_post") and link:
            # The feed only has a teaser: open the post itself (new posts only).
            if (normalize_url(link) or link) in seen:
                continue
            if len(posts) >= max_posts:
                break
            try:
                page_title, full = _page_text(client, link, src)
            except SkipSource:
                continue
            if full is None:
                continue
            text, title = full, title or page_title
        posts.append(Post(
            source=src["name"], title=title or "(untitled)",
            url=link, published=published, text=text,
            tags=list(src.get("tags") or []),
        ))
    return posts


def _page_text(client: PoliteClient, href: str, src: dict) -> tuple[str | None, str | None]:
    """Open one post page. Returns (title, text); text is None if locked/empty."""
    page = client.get(href)
    psoup = BeautifulSoup(page.text, "html.parser")
    og = psoup.find("meta", attrs={"property": "og:title"})
    title = og.get("content") if og else None
    content_sel = src.get("content_selector")
    if content_sel:
        node = psoup.select_one(content_sel)
        if node is None:
            # The usual body is missing: almost always a locked/paywalled post.
            return title, None
    else:
        node = psoup.find("article") or psoup.find("main") or psoup.body or psoup
    text = html_to_text(str(node))
    if looks_paywalled(text) or len(text) < int(src.get("min_text_chars", 200)):
        return title, None
    return title, text


def _published_from_html(soup: BeautifulSoup) -> datetime | None:
    for attrs in ({"property": "article:published_time"}, {"name": "date"},
                  {"itemprop": "datePublished"}):
        m = soup.find("meta", attrs=attrs)
        if m and m.get("content"):
            try:
                return datetime.fromisoformat(m["content"].replace("Z", "+00:00"))
            except ValueError:
                pass
    t = soup.find("time", attrs={"datetime": True})
    if t:
        try:
            return datetime.fromisoformat(t["datetime"].replace("Z", "+00:00"))
        except ValueError:
            pass
    return None


def read_webpage(client: PoliteClient, src: dict, since: datetime,
                 seen: set[str], max_posts: int) -> list[Post]:
    """Scrape a listing page for post links, then open each new post."""
    resp = client.get(src["url"])
    soup = BeautifulSoup(resp.text, "html.parser")
    selector = src.get("link_selector") or "article a[href], h2 a[href], h3 a[href]"
    pattern = re.compile(src["link_pattern"]) if src.get("link_pattern") else None
    links, order = {}, []
    for a in soup.select(selector):
        if pattern and not pattern.search(a.get("href", "")):
            continue
        href = urljoin(src["url"], a.get("href", ""))
        if not href.startswith("http"):
            continue
        key = normalize_url(href)
        if key and key not in links:
            links[key] = (href, a.get("aria-label") or a.get_text(" ", strip=True))
            order.append(key)
    if not order:
        raise ValueError(f"no links matched selector {selector!r} — the site layout may have changed")

    posts = []
    for key in order:
        if key in seen:
            continue
        if len(posts) >= max_posts:
            break
        href, link_text = links[key]
        try:
            page = client.get(href)
        except SkipSource:
            continue
        psoup = BeautifulSoup(page.text, "html.parser")
        published = _published_from_html(psoup)
        if published and published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if published and not src.get("ignore_dates") and published < since:
            continue
        content_sel = src.get("content_selector")
        if content_sel:
            node = psoup.select_one(content_sel)
            if node is None:
                # The usual body is missing: almost always a locked/paywalled post.
                continue
        else:
            node = psoup.find("article") or psoup.find("main") or psoup.body or psoup
        text = html_to_text(str(node))
        if looks_paywalled(text) or len(text) < int(src.get("min_text_chars", 200)):
            continue
        og = psoup.find("meta", attrs={"property": "og:title"})
        title = (og.get("content") if og else None) or link_text or href
        posts.append(Post(source=src["name"], title=title.strip(), url=href,
                          published=published, text=text, tags=list(src.get("tags") or [])))
    return posts


def read_source(client: PoliteClient, src: dict, since: datetime,
                seen: set[str], max_posts: int) -> list[Post]:
    if src.get("members_only"):
        raise SkipSource("marked members_only in sources.yaml — logins aren't supported")
    max_posts = min(int(src.get("max_posts", max_posts)), max_posts)
    kind = (src.get("type") or "rss").lower()
    if kind in ("rss", "substack", "atom", "feed"):
        posts = read_feed(client, src, since, seen, max_posts)
    elif kind == "webpage":
        posts = read_webpage(client, src, since, seen, max_posts)
    else:
        raise ValueError(f"unknown source type {kind!r} (use rss, substack or webpage)")
    posts = [p for p in posts if p.key not in seen]
    return posts[:max_posts]


def default_since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)
