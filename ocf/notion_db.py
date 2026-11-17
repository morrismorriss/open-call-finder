"""Talking to Notion: find or create the database, read existing rows, add new ones."""
from __future__ import annotations

import re
import time
from datetime import date

import requests

from .extract import DISCIPLINES, TYPES
from .util import clip, normalize_url, redact

API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
DB_TITLE = "Open Calls"


def page_id_from(value: str) -> str:
    """Accept a raw ID or a full Notion link and return the 32-character ID."""
    value = (value or "").strip().split("?")[0].split("#")[0]
    # The ID is always the last 32 hex characters (links look like .../Writer-World-<id>).
    m = re.search(r"([0-9a-f]{32})$", value.rstrip("/").replace("-", ""), re.I)
    if not m:
        raise ValueError(f"couldn't find a Notion page ID in {value!r}")
    return m.group(1).lower()


def schema() -> dict:
    sel = lambda names: {"options": [{"name": n} for n in names]}  # noqa: E731
    return {
        "Name": {"title": {}},
        "Organization": {"rich_text": {}},
        "Type": {"select": sel(TYPES)},
        "Deadline": {"date": {}},
        "Fee": {"rich_text": {}},
        "Pay / Stipend": {"rich_text": {}},
        "Location": {"rich_text": {}},
        "Remote": {"checkbox": {}},
        "Disciplines": {"multi_select": sel(DISCIPLINES)},
        "Eligibility": {"rich_text": {}},
        "Summary": {"rich_text": {}},
        "Apply link": {"url": {}},
        "Source": {"select": {}},
        "Source post": {"url": {}},
        "Found on": {"date": {}},
    }


def _norm_text(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def dedupe_keys(call: dict, source_post: str | None) -> set[str]:
    keys = set()
    if call.get("apply_url"):
        keys.add("apply:" + normalize_url(call["apply_url"]))
    if source_post:
        keys.add("post:" + normalize_url(source_post) + "|" + _norm_text(call.get("title")))
    org, dl = _norm_text(call.get("organization")), call.get("deadline")
    if org and dl:
        keys.add(f"orgdl:{org}|{dl}|{call.get('type') or ''}")
    return keys


class Notion:
    def __init__(self, token: str, log):
        self.log = log
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self.database_id: str | None = None
        self.data_source_id: str | None = None

    def _req(self, method: str, path: str, **kw) -> dict:
        for attempt in range(5):
            r = self.s.request(method, API + path, timeout=30, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After", 2 * (attempt + 1))))
                continue
            if r.status_code >= 400:
                msg = r.json().get("message", r.text) if r.headers.get(
                    "content-type", "").startswith("application/json") else r.text
                raise RuntimeError(f"Notion said ({r.status_code}): {redact(msg)}")
            time.sleep(0.35)  # Notion allows ~3 requests/second
            return r.json()
        raise RuntimeError("Notion kept rate-limiting us; try again later")

    # ---------- setup ----------
    def connect(self, database_id: str | None, parent_page: str | None) -> str:
        """Find the database (or create it) and return its ID."""
        if database_id:
            self._use_database(page_id_from(database_id))
            return self.database_id
        if not parent_page:
            raise RuntimeError("Set NOTION_PARENT_PAGE_ID (or NOTION_DATABASE_ID) as a secret")
        parent = page_id_from(parent_page)
        # Look for an existing "Open Calls" database on the page before creating one.
        cursor = None
        while True:
            params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            res = self._req("GET", f"/blocks/{parent}/children", params=params)
            for b in res.get("results", []):
                if b.get("type") == "child_database" and \
                        b["child_database"].get("title", "").strip() == DB_TITLE:
                    self._use_database(b["id"].replace("-", ""))
                    return self.database_id
            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")
        self.log(f"Creating the '{DB_TITLE}' database in Notion…")
        res = self._req("POST", "/databases", json={
            "parent": {"type": "page_id", "page_id": parent},
            "title": [{"type": "text", "text": {"content": DB_TITLE}}],
            "initial_data_source": {"properties": schema()},
        })
        self._use_database(res["id"].replace("-", ""))
        return self.database_id

    def _use_database(self, db_id: str):
        res = self._req("GET", f"/databases/{db_id}")
        sources = res.get("data_sources") or []
        if not sources:
            raise RuntimeError("That Notion database has no data source (is it a linked view?)")
        self.database_id = db_id
        self.data_source_id = sources[0]["id"]
        # Put back any column that was deleted by hand, so writes don't fail.
        ds = self._req("GET", f"/data_sources/{self.data_source_id}")
        missing = {k: v for k, v in schema().items() if k not in ds.get("properties", {})}
        missing.pop("Name", None)
        if missing:
            self.log(f"Adding missing Notion columns: {', '.join(missing)}")
            self._req("PATCH", f"/data_sources/{self.data_source_id}",
                      json={"properties": missing})

    # ---------- reading ----------
    def existing_keys(self) -> set[str]:
        keys: set[str] = set()
        cursor = None
        while True:
            body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            res = self._req("POST", f"/data_sources/{self.data_source_id}/query", json=body)
            for page in res.get("results", []):
                p = page.get("properties", {})
                text = lambda name: "".join(  # noqa: E731
                    t.get("plain_text", "") for t in (p.get(name, {}).get("rich_text")
                                                      or p.get(name, {}).get("title") or []))
                call = {
                    "title": text("Name"),
                    "organization": text("Organization"),
                    "deadline": ((p.get("Deadline", {}).get("date") or {}).get("start") or "")[:10] or None,
                    "type": (p.get("Type", {}).get("select") or {}).get("name"),
                    "apply_url": p.get("Apply link", {}).get("url"),
                }
                post_url = p.get("Source post", {}).get("url")
                keys |= dedupe_keys(call, post_url)
                if post_url:
                    # Lets a single-call post be recognised even if the AI names it
                    # differently on a later run.
                    keys.add("postonly:" + normalize_url(post_url))
            if not res.get("has_more"):
                return keys
            cursor = res.get("next_cursor")

    # ---------- writing ----------
    def add(self, call: dict, source: str, source_post: str):
        rt = lambda s: {"rich_text": [{"text": {"content": clip(s, 1990)}}] if s else []}  # noqa: E731
        props = {
            "Name": {"title": [{"text": {"content": clip(call.get("title") or "(untitled)", 1990)}}]},
            "Organization": rt(call.get("organization")),
            "Type": {"select": {"name": call.get("type") or "Other"}},
            "Fee": rt(call.get("fee")),
            "Pay / Stipend": rt(call.get("pay")),
            "Location": rt(call.get("location")),
            "Remote": {"checkbox": bool(call.get("remote"))},
            "Disciplines": {"multi_select": [{"name": d} for d in call.get("disciplines") or []]},
            "Eligibility": rt(call.get("eligibility")),
            "Summary": rt(call.get("summary")),
            "Apply link": {"url": call.get("apply_url") or None},
            # Notion select options can't contain commas.
            "Source": {"select": {"name": re.sub(r"\s*,\s*", " ", source)[:100]}},
            "Source post": {"url": source_post or None},
            "Found on": {"date": {"start": date.today().isoformat()}},
        }
        if call.get("deadline"):
            props["Deadline"] = {"date": {"start": str(call["deadline"])}}
        self._req("POST", "/pages", json={
            "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
            "properties": props,
        })
