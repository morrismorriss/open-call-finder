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
from .util import guess_deadline, parse_iso_date, redact

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
  "remote": true ONLY if taking part needs no travel at all (e.g. online journal, virtual
            festival, remote job). Being able to apply or submit online does NOT make it remote;
            a play produced in a theater, an in-person residency or festival is false,
  "eligibility": "who can apply, short, or null",
  "disciplines": list chosen from {disciplines},
  "relevant": true if it plausibly fits the reader described above, else false,
  "summary": "1-2 plain sentences",
  "apply_url": "direct link to apply or to the organization's own call page, if the post contains one; never the URL of this post itself; else null"
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
        # Hard limit on time spent in the AI step per run; after that, keyword mode.
        self.stop_at = time.time() + 60 * float(ai.get("max_minutes_per_run", 15))
        self.request_timeout = float(ai.get("request_timeout_seconds", 60))
        self._timeouts: dict[str, int] = {}
        self._thinking_step: dict[str, int] = {}
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
                    found.append((1 if mt.group(2) else 0, float(mt.group(1)), name))
            # Flash-Lite first (fast, biggest free quota), newest first; then Flash.
            found.sort(key=lambda t: (t[0], t[1]), reverse=True)
            self._models = [n for *_, n in found] or FALLBACK_MODELS
        except requests.RequestException as e:
            self.log(f"  (couldn't list Gemini models: {redact(e)}; using defaults)")
            self._models = FALLBACK_MODELS
        self.log(f"  (Gemini models to try, in order: {', '.join(self._models[:4])})")
        return self._models

    @staticmethod
    def _thinking_options(model: str) -> list[dict | None]:
        """Ask for as little "thinking" as the model allows: it's slow and not needed
        for copying details out of a post. Older/newer models name this differently,
        so we try each form and fall back to sending nothing."""
        m = re.search(r"gemini-(\d+)", model)
        if m and int(m.group(1)) >= 3:
            return [{"thinkingLevel": "minimal"}, {"thinkingLevel": "low"}, None]
        return [{"thinkingBudget": 0}, None]

    def _drop(self, model: str):
        if model in self._models:
            self._models.remove(model)

    # ---------- the API call ----------
    def _call(self, prompt: str) -> dict | None:
        for model in list(self._pick_models()):
            for attempt in range(4):
                if time.time() > self.stop_at:
                    self.enabled = False
                    self.disabled_reason = "reached the AI time limit for this run (max_minutes_per_run)"
                    return None
                wait = self.min_gap - (time.time() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.time()
                options = self._thinking_options(model)
                step = self._thinking_step.get(model, 0)
                gen = {"temperature": 0.1, "responseMimeType": "application/json"}
                if step < len(options) and options[step]:
                    gen["thinkingConfig"] = options[step]
                try:
                    r = self.session.post(
                        f"{GEMINI_BASE}/models/{model}:generateContent",
                        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen},
                        timeout=self.request_timeout)
                except requests.Timeout:
                    n = self._timeouts[model] = self._timeouts.get(model, 0) + 1
                    self.log(f"  ({model} took too long" + ("; not using it again this run)" if n >= 2 else ")"))
                    if n >= 2:
                        self._drop(model)
                    break  # try the next model for this post
                except requests.RequestException as e:
                    self.log(f"  (Gemini network error: {redact(e)})")
                    time.sleep(5)
                    continue
                self.calls_made += 1
                if r.status_code == 200:
                    try:
                        parts = r.json()["candidates"][0]["content"]["parts"]
                        raw = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                        return json.loads(raw)
                    except (KeyError, IndexError, ValueError):
                        self.log("  (Gemini answer wasn't valid JSON; keyword mode for this post)")
                        return None
                if r.status_code == 400 and "thinking" in r.text.lower() and step + 1 < len(options):
                    self._thinking_step[model] = step + 1  # this model wants a different setting
                    continue
                if r.status_code == 429:
                    body = r.text
                    if "PerDay" in body or "per day" in body.lower():
                        # Daily free quota used up on this model; try the next model.
                        self.log(f"  (daily free quota reached for {model})")
                        self._drop(model)
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
                    self._drop(model)
                    break
                if r.status_code in (401, 403):
                    self.enabled, self.disabled_reason = False, "Gemini key rejected (check GEMINI_API_KEY)"
                    return None
                if r.status_code >= 500:
                    time.sleep(10)
                    continue
                self.log(f"  (Gemini error {r.status_code}: {redact(r.text[:300])})")
                return None
        if not self._models:
            self.enabled, self.disabled_reason = False, "no Gemini model available (free quota used up or too slow)"
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
