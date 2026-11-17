"""Open Call Finder: read sources, find open calls, add them to Notion.

Run it with:   python -m ocf.main            (normal run)
               python -m ocf.main --dry-run  (print only, touch nothing)
               python -m ocf.main --dry-run --source "NYC Playwrights"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .extract import Extractor, passes_keywords
from .fetch import PoliteClient, SkipSource, read_source
from .notion_db import Notion, dedupe_keys

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state" / "state.json"


def log(msg: str = ""):
    print(msg, flush=True)


def load_yaml(name: str) -> dict:
    path = ROOT / name
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


def fmt_call(c: dict) -> str:
    bits = [f"[{c.get('type') or 'Other'}]", c.get("title") or "(untitled)"]
    if c.get("organization"):
        bits.append(f"— {c['organization']}")
    lines = ["  + " + " ".join(bits)]
    detail = []
    if c.get("deadline"):
        detail.append(f"deadline {c['deadline']}")
    if c.get("fee"):
        detail.append(f"fee: {c['fee']}")
    if c.get("pay"):
        detail.append(f"pay: {c['pay']}")
    if c.get("location"):
        detail.append(c["location"] + (" (remote OK)" if c.get("remote") else ""))
    elif c.get("remote"):
        detail.append("remote")
    if c.get("disciplines"):
        detail.append("/".join(c["disciplines"]))
    if detail:
        lines.append("      " + " · ".join(detail))
    if c.get("summary"):
        lines.append("      " + c["summary"])
    if c.get("apply_url"):
        lines.append("      apply: " + c["apply_url"])
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Find open calls and add them to Notion.")
    ap.add_argument("--dry-run", action="store_true", help="print what would be added; change nothing")
    ap.add_argument("--source", action="append", help="only check this source (by name); repeatable")
    ap.add_argument("--days", type=int, help="look back this many days instead of 'since last run'")
    ap.add_argument("--no-ai", action="store_true", help="skip Gemini, use keyword-only mode")
    ap.add_argument("--max-posts", type=int, help="max new posts per source (default from config)")
    args = ap.parse_args(argv)

    cfg = load_yaml("config.yaml")
    sources = (load_yaml("sources.yaml").get("sources")) or []
    if args.no_ai:
        cfg.setdefault("ai", {})["enabled"] = False
    if args.source:
        wanted = {s.lower() for s in args.source}
        sources = [s for s in sources if s.get("name", "").lower() in wanted]
        if not sources:
            log(f"No source named {args.source} in sources.yaml")
            return 1
    else:
        sources = [s for s in sources if s.get("enabled", True)]

    state = load_state()
    seen: dict[str, str] = state.get("seen", {})
    now = datetime.now(timezone.utc)
    if args.days:
        since = now - timedelta(days=args.days)
    elif state.get("last_run"):
        # Overlap by a day so nothing slips through; "seen" stops repeats.
        since = datetime.fromisoformat(state["last_run"]) - timedelta(days=1)
    else:
        since = now - timedelta(days=int(cfg.get("first_run_days", 14)))
    max_posts = args.max_posts or int(cfg.get("max_posts_per_source", 25))
    keywords = cfg.get("keywords") or []
    exclude = cfg.get("exclude_keywords") or []
    skip_irrelevant = bool(cfg.get("skip_irrelevant", False))
    today = date.today()

    log(f"Open Call Finder — {'DRY RUN (nothing will be written)' if args.dry_run else 'live run'}")
    log(f"Looking at posts since {since:%Y-%m-%d %H:%M} UTC from {len(sources)} source(s)\n")

    notion = None
    existing: set[str] = set()
    if not args.dry_run:
        token = os.environ.get("NOTION_TOKEN", "").strip()
        if not token:
            log("ERROR: NOTION_TOKEN is not set. Add it as a GitHub secret (see README).")
            return 1
        try:
            notion = Notion(token, log)
            db_id = notion.connect(os.environ.get("NOTION_DATABASE_ID") or state.get("database_id"),
                                   os.environ.get("NOTION_PARENT_PAGE_ID"))
            state["database_id"] = db_id
            existing = notion.existing_keys()
            # Don't print the database ID: on a public repository, run logs are public.
            log(f"Connected to Notion ({len(existing)} existing entries indexed)\n")
        except Exception as e:  # noqa: BLE001
            log(f"ERROR: couldn't connect to Notion: {e}")
            log("Check that the page is shared with your integration (README step 1).")
            return 1

    extractor = Extractor(cfg, log)
    if not extractor.enabled:
        log(f"AI step off ({extractor.disabled_reason}) — using keyword-only mode.\n")

    client = PoliteClient(delay_seconds=float(cfg.get("delay_seconds", 2)))
    stats = {"sources": 0, "posts": 0, "matched": 0, "added": 0, "dupes": 0,
             "expired": 0, "not_calls": 0, "irrelevant": 0}
    errors: list[str] = []
    per_source: list[str] = []

    for src in sources:
        name = src.get("name", "?")
        stats["sources"] += 1
        log(f"== {name} ==")
        try:
            posts = read_source(client, src, since, set(seen), max_posts)
        except SkipSource as e:
            log(f"  skipped: {e}\n")
            per_source.append(f"{name}: skipped ({e})")
            continue
        except Exception as e:  # noqa: BLE001 - one bad source must not stop the rest
            msg = f"{type(e).__name__}: {e}"
            log(f"  ERROR: {msg}\n")
            errors.append(f"{name}: {msg}")
            per_source.append(f"{name}: error")
            continue

        added_here = 0
        stats["posts"] += len(posts)
        log(f"  {len(posts)} new post(s)")
        for post in posts:
            if not passes_keywords(post, keywords, exclude):
                seen[post.key] = today.isoformat()
                continue
            stats["matched"] += 1
            log(f"  • {post.title}\n    {post.url}")
            try:
                calls, mode = extractor.extract(post)
            except Exception as e:  # noqa: BLE001 - never fail on the AI step
                log(f"    (extraction crashed: {e}; using keyword mode)")
                from .extract import keyword_call
                calls, mode = [keyword_call(post)], "keyword"
            if not calls:
                stats["not_calls"] += 1
                log("    – not an open call")
                seen[post.key] = today.isoformat()
                continue
            post_ok = True
            for call in calls:
                if call.get("deadline") and call["deadline"] < today:
                    stats["expired"] += 1
                    continue
                if skip_irrelevant and not call.get("relevant", True):
                    stats["irrelevant"] += 1
                    continue
                keys = dedupe_keys(call, post.url)
                if keys & existing:
                    stats["dupes"] += 1
                    continue
                if not call.get("title"):
                    call["title"] = post.title
                log(fmt_call(call) + ("" if mode == "ai" else "   (keyword mode)"))
                if notion:
                    try:
                        notion.add(call, name, post.url)
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{name}: Notion write failed for {call['title']!r}: {e}")
                        log(f"    ERROR writing to Notion: {e}")
                        post_ok = False
                        continue
                existing |= keys
                stats["added"] += 1
                added_here += 1
            if post_ok:
                seen[post.key] = today.isoformat()
        per_source.append(f"{name}: {len(posts)} posts, {added_here} calls")
        log("")

    # ---- summary ----
    verb = "would add" if args.dry_run else "added"
    summary = [
        "## Open Call Finder run summary",
        f"- Sources checked: {stats['sources']}",
        f"- New posts read: {stats['posts']} (passed keyword filter: {stats['matched']})",
        f"- Calls {verb}: **{stats['added']}**",
        f"- Skipped: {stats['dupes']} duplicates, {stats['expired']} past deadline, "
        f"{stats['not_calls']} posts that weren't calls"
        + (f", {stats['irrelevant']} not relevant" if skip_irrelevant else ""),
        f"- AI: {'Gemini, ' + str(extractor.calls_made) + ' requests' if extractor.calls_made else 'off'}"
        + (f" (stopped: {extractor.disabled_reason})" if extractor.calls_made and extractor.disabled_reason else ""),
        "", "### By source", *[f"- {s}" for s in per_source],
    ]
    if errors:
        summary += ["", f"### Errors ({len(errors)})", *[f"- {e}" for e in errors]]
    text = "\n".join(summary)
    log(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")

    if not args.dry_run and not args.source:
        cutoff = (today - timedelta(days=180)).isoformat()
        state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
        state["last_run"] = now.isoformat()
        save_state(state)
    elif not args.dry_run:
        state["seen"] = seen
        save_state(state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # last-resort safety net: print, don't hide
        traceback.print_exc()
        sys.exit(1)
