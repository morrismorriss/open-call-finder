"""Turning a post into structured open-call details.

Two modes:
  * AI mode     - Google Gemini (free tier) reads the post and returns JSON.
  * Keyword mode - no AI; we keep the post's title/link and guess the deadline.
The AI step can never crash the run: any problem drops us into keyword mode.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date

import requests

from .fetch import Post
from .util import guess_deadline, parse_iso_date

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
FALLBACK_MODELS = ["gemini-2.5-flash-lite", "gemini-2.5-flash"]

TYPES = ["Residency", "Grant", "Fellowship", "Commission", "Submission / Script call",
         "Festival", "Job / Gig", "Workshop", "Other"]
DISCIPLINES = ["Playwriting", "Theater", "Devised", "Film", "Writing", "Performance",
               "Interdisciplinary", "Other"]


def passes_keywords(post: Post, keywords: list[str], exclude: list[str]) -> bool:
    """True if the post mentions any keyword. A keyword matches at the start of a
    word, so "grant" matches "grants" but "lab" doesn't match "collaborate".
    Excluded words only count in the title (posts mention them in passing)."""
    title = post.title.lower()
    if any(x.lower() in title for x in exclude or []):
        return False
    if not keywords:
        return True
    hay = f"{title} {post.text.lower()}"
    pattern = r"\b(?:" + "|".join(re.escape(k.lower().strip()) for k in keywords if k.strip()) + ")"
    return re.search(pattern, hay) is not None


PROMPT = """You read posts from newsletters and websites and pull out OPEN CALLS
for artists (residencies, grants, fellowships, commissions, script/submission
calls, festivals, paid jobs or gigs, workshops with an application).

Today is {today}. The reader is: {profile}

A single post may describe ONE call or be a ROUNDUP listing many. Return every
distinct call it describes (at most {max_calls}). If the post is not about any
open call (e.g. a review, an essay, an event ticket sale, a craft article),
return an empty list.

Reply with JSON only, exactly in this shape:
{{"calls": [{{
  "is_open_call": true,
  "title": "short name of the opportunity",
  "organization": "who runs it, or null",
  "type": one of {types},
  "deadline": "YYYY-MM-DD" or null if not stated (use the year implied by the post),
  "fee": "application fee as written, or \\"None\\" if explicitly free, or null if not stated",
  "pay": "pay / stipend / prize as written, or null",
  "location": "city/region/country, or null",
  "remote": true if it can be done/applied for remotely with no residency travel, else false,
  "eligibility": "who can apply, short, or null",
  "disciplines": list chosen from {disciplines},
  "relevant": true if it plausibly fits the reader described above, else false,
  "summary": "1-2 plain sentences",
  "apply_url": "direct link to apply or to the official call page if present in the post, else null"
}}]}}

POST TITLE: {title}
POST URL: {url}
POST TEXT:
{text}
"""


class Extractor:
    def __init__(self, cfg: dict, log):
        ai = cfg.get("ai", {})
        self.log = log
        self.key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.enabled = bool(self.key) and ai.get("enabled", True)
        self.min_gap = float(ai.get("seconds_between_calls", 8))
        self.max_calls = int(ai.get("max_calls_per_run", 80))
        self.max_chars = int(ai.get("max_chars_per_post", 15000))
        self.max_calls_per_post = int(ai.get("max_calls_per_post", 40))
        self.profile = os.environ.get("OCF_PROFILE", "").strip() or cfg.get("profile", "an artist")
        self.calls_made = 0
        self.failures_in_a_row = 0
        # Total seconds we're willing to sit waiting on rate limits in one run.
        self.wait_budget = float(ai.get("max_rate_limit_wait_seconds", 240))
        self._last = 0.0
        self._models = None
        self._configured_model = ai.get("model", "auto")
        self.disabled_reason = None if self.enabled else (
            "no GEMINI_API_KEY set" if not self.key else "AI turned off in config.yaml")
        self.session = requests.Session()
        self.session.headers["x-goog-api-key"] = self.key

    # ---------- model selection ----------
    def _pick_models(self) -> list[str]:
        if self._models is not None:
            return self._models
        if self._configured_model and self._configured_model != "auto":
            self._models = [self._configured_model]
            return self._models
        try:
            r = self.session.get(f"{GEMINI_BASE}/models", params={"pageSize": 200}, timeout=20)
            r.raise_for_status()
            found = []
            for m in r.json().get("models", []):
                name = m.get("name", "").removeprefix("models/")
                if "generateContent" not in m.get("supportedGenerationMethods", []):
                    continue
                mt = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash(-lite)?", name)
                if mt:  # stable Flash / Flash-Lite only (no preview, image, tts...)
                    found.append((float(mt.group(1)), 1 if mt.group(2) else 0, name))
            # Newest version first; within a version prefer Flash-Lite (bigger free quota).
            found.sort(key=lambda t: (t[0], t[1]), reverse=True)
            self._models = [n for *_, n in found] or FALLBACK_MODELS
        except requests.RequestException as e:
            self.log(f"  (couldn't list Gemini models: {e}; using defaults)")
            self._models = FALLBACK_MODELS
        return self._models

    # ---------- the API call ----------
    def _call(self, prompt: str) -> dict | None:
        for model in list(self._pick_models()):
            for attempt in range(3):
                wait = self.min_gap - (time.time() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.time()
                try:
                    r = self.session.post(
                        f"{GEMINI_BASE}/models/{model}:generateContent",
                        json={"contents": [{"parts": [{"text": prompt}]}],
                              "generationConfig": {"temperature": 0.1,
                                                   "responseMimeType": "application/json"}},
                        timeout=90)
                except requests.RequestException as e:
                    self.log(f"  (Gemini network error: {e})")
                    time.sleep(5)
                    continue
                self.calls_made += 1
                if r.status_code == 200:
                    try:
                        parts = r.json()["candidates"][0]["content"]["parts"]
                        raw = "".join(p.get("text", "") for p in parts)
                        return json.loads(raw)
                    except (KeyError, IndexError, ValueError):
                        self.log("  (Gemini answer wasn't valid JSON; keyword mode for this post)")
                        return None
                if r.status_code == 429:
                    body = r.text
                    if "PerDay" in body or "per day" in body.lower():
                        # Daily free quota used up on this model; try the next model.
                        self.log(f"  (daily free quota reached for {model})")
                        self._models.remove(model)
                        break
                    delay = 20
                    m = re.search(r'"retryDelay":\s*"(\d+)', body)
                    if m:
                        delay = min(int(m.group(1)) + 2, 60)
                    if delay > self.wait_budget:
                        self.enabled = False
                        self.disabled_reason = "Gemini kept rate-limiting; out of waiting time for this run"
                        return None
                    self.wait_budget -= delay
                    self.log(f"  (Gemini rate limit; waiting {delay}s)")
                    time.sleep(delay)
                    continue
                if r.status_code in (400, 404) and "not found" in r.text.lower():
                    self._models.remove(model)
                    break
                if r.status_code in (401, 403):
                    self.enabled, self.disabled_reason = False, "Gemini key rejected (check GEMINI_API_KEY)"
                    return None
                if r.status_code >= 500:
                    time.sleep(10)
                    continue
                self.log(f"  (Gemini error {r.status_code}: {r.text[:200]})")
                return None
        if not self._models:
            self.enabled, self.disabled_reason = False, "Gemini free quota used up for today"
        return None

    # ---------- public ----------
    def extract(self, post: Post) -> tuple[list[dict], str]:
        """Return (calls, mode). mode is 'ai' or 'keyword'."""
        if self.enabled and self.calls_made >= self.max_calls:
            self.enabled, self.disabled_reason = False, f"hit max_calls_per_run ({self.max_calls})"
        if self.enabled:
            prompt = PROMPT.format(
                today=date.today().isoformat(), profile=self.profile,
                max_calls=self.max_calls_per_post, types=TYPES, disciplines=DISCIPLINES,
                title=post.title, url=post.url, text=post.text[: self.max_chars])
            data = self._call(prompt)
            if data is None:
                self.failures_in_a_row += 1
                if self.failures_in_a_row >= 3 and self.enabled:
                    self.enabled, self.disabled_reason = False, "Gemini failed 3 posts in a row"
            else:
                self.failures_in_a_row = 0
                calls = data if isinstance(data, list) else (data.get("calls") or [])
                return [clean(c) for c in calls if isinstance(c, dict) and c.get("is_open_call", True)], "ai"
        return [keyword_call(post)], "keyword"


def clean(c: dict) -> dict:
    t = c.get("type") or "Other"
    if t not in TYPES:
        low = t.lower()
        t = next((x for x in TYPES if x.split()[0].lower() in low), "Other")
    disc = [d for d in (c.get("disciplines") or []) if d in DISCIPLINES]
    return {
        "title": (c.get("title") or "").strip() or None,
        "organization": c.get("organization") or None,
        "type": t,
        "deadline": parse_iso_date(c.get("deadline")),
        "fee": c.get("fee"),
        "pay": c.get("pay"),
        "location": c.get("location"),
        "remote": bool(c.get("remote")),
        "eligibility": c.get("eligibility"),
        "disciplines": disc,
        "relevant": c.get("relevant", True) is not False,
        "summary": c.get("summary"),
        "apply_url": c.get("apply_url") if str(c.get("apply_url") or "").startswith("http") else None,
    }


def keyword_call(post: Post) -> dict:
    return {
        "title": post.title, "organization": None, "type": "Other",
        "deadline": guess_deadline(post.text), "fee": None, "pay": None, "location": None,
        "remote": False, "eligibility": None, "disciplines": [], "relevant": True,
        "summary": None, "apply_url": None,
    }
