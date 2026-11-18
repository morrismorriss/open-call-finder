"""Regression tests for Open Call Finder.

Run with:  python -m unittest discover -s tests

Network and Notion are stubbed (there is no test server and no credentials in
CI); everything else - the real orchestration in main(), URL normalisation,
dedupe keys, date parsing and the SSRF guard - runs as shipped.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocf import main as m  # noqa: E402
from ocf.fetch import Post, _is_public_http_url, read_webpage  # noqa: E402
from ocf.notion_db import dedupe_keys, page_id_from  # noqa: E402


class _RecordingNotion:
    """Captures rows that would be written, so tests need no Notion access."""

    instances: list["_RecordingNotion"] = []

    def __init__(self, token, log):
        self.rows = []
        _RecordingNotion.instances.append(self)

    def connect(self, database_id, parent_page):
        return "test-db"

    def existing_keys(self):
        return set()

    def add(self, call, source, source_post):
        self.rows.append({"call": call, "source": source, "post": source_post})


class _FakeExtractor:
    """Stands in for the Gemini extractor: returns three untitled calls."""

    def __init__(self, cfg, log):
        self.key = ""
        self.enabled = True
        self.disabled_reason = None
        self.calls_made = 0

    def extract(self, post):
        return ([{"title": None, "organization": None, "deadline": None,
                  "type": t, "apply_url": None, "relevant": True}
                 for t in ("Grant", "Residency", "Festival")], "ai")


class _RunsOutOnSecond:
    """AI starts on, then reports itself exhausted after the second post."""

    def __init__(self, cfg, log):
        self.key = "AIzaFAKEKEYFAKEKEYFAKEKEYFAKE"
        self.enabled = True
        self.disabled_reason = None
        self.calls_made = 0
        self.n = 0

    def extract(self, post):
        self.n += 1
        if self.n >= 2:
            self.enabled = False
            self.disabled_reason = "quota"
        return ([{"title": post.title, "organization": None, "deadline": None,
                  "type": "Grant", "apply_url": None, "relevant": True}], "ai")


def _post(title="Grant: open call", url="https://example.com/p1", text="open call"):
    return Post(source="Test Source", title=title, url=url,
                published=datetime.now(timezone.utc), text=text)


class MainRunTests(unittest.TestCase):
    """A run with the AI deliberately off must still add posts (bug #1)."""

    def _run(self, argv, extractor=None, key="AIzaFAKEKEYFAKEKEYFAKEKEYFAKE"):
        _RecordingNotion.instances.clear()
        env = {"NOTION_TOKEN": "ntn_faketoken000000000000000000"}
        if key:
            env["GEMINI_API_KEY"] = key
        else:
            os.environ.pop("GEMINI_API_KEY", None)
        patches = [
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch("ocf.main.Notion", _RecordingNotion),
            mock.patch("ocf.main.load_state", lambda: {}),
            mock.patch("ocf.main.save_state", lambda state: None),
            mock.patch("ocf.main.read_source",
                       lambda client, src, since, seen, max_posts: [_post()]),
        ]
        if extractor is not None:
            patches.append(mock.patch("ocf.main.Extractor", extractor))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return m.main(argv)

    def test_no_ai_with_key_present_still_adds(self):
        rc = self._run(["--no-ai", "--source", "NYC Playwrights"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(_RecordingNotion.instances[0].rows), 1,
                         "AI disabled should add in keyword mode, not defer everything")

    def test_ai_enabled_path_still_adds(self):
        rc = self._run(["--source", "NYC Playwrights"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(_RecordingNotion.instances[0].rows), 1)

    def test_no_ai_without_key_still_adds(self):
        rc = self._run(["--no-ai", "--source", "NYC Playwrights"], key=None)
        self.assertEqual(rc, 0)
        self.assertEqual(len(_RecordingNotion.instances[0].rows), 1)

    def test_untitled_roundup_calls_do_not_collapse(self):
        rc = self._run(["--source", "NYC Playwrights"], extractor=_FakeExtractor, key=None)
        self.assertEqual(rc, 0)
        rows = _RecordingNotion.instances[0].rows
        self.assertEqual(len(rows), 3, "three distinct calls should produce three rows")
        self.assertEqual(len({r["call"]["title"] for r in rows}), 3,
                         "each untitled call needs its own title")

    def test_posts_after_ai_runs_out_are_deferred(self):
        _RecordingNotion.instances.clear()
        os.environ["GEMINI_API_KEY"] = "AIzaFAKEKEYFAKEKEYFAKEKEYFAKE"
        os.environ["NOTION_TOKEN"] = "ntn_faketoken000000000000000000"
        posts = [_post(title=f"p{i}", url=f"https://example.com/{i}") for i in (1, 2, 3)]
        with mock.patch("ocf.main.Notion", _RecordingNotion), \
                mock.patch("ocf.main.load_state", lambda: {}), \
                mock.patch("ocf.main.save_state", lambda state: None), \
                mock.patch("ocf.main.Extractor", _RunsOutOnSecond), \
                mock.patch("ocf.main.read_source",
                           lambda client, src, since, seen, max_posts: posts):
            m.main(["--source", "NYC Playwrights"])
        titles = [r["call"]["title"] for r in _RecordingNotion.instances[0].rows]
        self.assertEqual(titles, ["p1", "p2"],
                         "a post left after the AI runs out must wait for the next run")


class DedupeKeyTests(unittest.TestCase):
    def test_title_is_part_of_the_post_key(self):
        titled = dedupe_keys({"title": "My Grant", "organization": None,
                              "deadline": None, "type": "Other", "apply_url": None},
                             "https://example.com/post")
        untitled = dedupe_keys({"title": None, "organization": None,
                                "deadline": None, "type": "Other", "apply_url": None},
                               "https://example.com/post")
        self.assertNotEqual(titled, untitled)

    def test_apply_url_is_deduped(self):
        keys = dedupe_keys(
            {"title": "x", "apply_url": "https://Apply.Example.com/A/?utm_source=q"}, None)
        self.assertIn("apply:https://apply.example.com/A", keys)


class PublicUrlTests(unittest.TestCase):
    def test_rejects_internal_and_non_http(self):
        for bad in ("http://127.0.0.1:8080/admin",
                    "http://169.254.169.254/latest/meta-data/",
                    "http://10.0.0.5/", "http://192.168.1.1/",
                    "file:///etc/passwd", "ftp://example.com/x",
                    "http://[::1]/"):
            self.assertFalse(_is_public_http_url(bad), bad)

    def test_accepts_public_http(self):
        for ok in ("https://example.com/post", "http://93.184.216.34/x"):
            self.assertTrue(_is_public_http_url(ok), ok)

    def test_read_webpage_does_not_follow_internal_links(self):
        listing = ("<html><body>"
                   '<a href="/post/1">Post One</a>'
                   '<a href="http://169.254.169.254/latest/meta-data/">m</a>'
                   '<a href="http://127.0.0.1:8080/admin">a</a>'
                   "</body></html>")
        body = "<article>" + ("open call deadline text " * 30) + "</article>"
        page = ('<html><head><meta property="og:title" content="Post One">'
                f"</head><body>{body}</body></html>")

        class _Resp:
            def __init__(self, text):
                self.text = text
                self.status_code = 200
                self.content = text.encode()

            def raise_for_status(self):
                pass

        fetched = []

        class _Client:
            def get(self, url):
                fetched.append(url)
                return _Resp(listing if url.endswith("/list") else page)

        src = {"name": "Test", "type": "webpage", "url": "https://public.example.org/list",
               "link_selector": "a[href]", "ignore_dates": True}
        read_webpage(_Client(), src, datetime.now(timezone.utc), set(), 10)
        self.assertEqual(fetched,
                         ["https://public.example.org/list", "https://public.example.org/post/1"])


class PageIdTests(unittest.TestCase):
    def test_accepts_link_and_raw_id(self):
        pid = "12345678123412341234123456789012"
        self.assertEqual(page_id_from(f"https://www.notion.so/My-Page-{pid}"), pid)
        self.assertEqual(page_id_from(pid), pid)
        self.assertEqual(page_id_from("12345678-1234-1234-1234-123456789012"), pid)

    def test_rejects_bare_over_long_hex(self):
        with self.assertRaises(ValueError):
            page_id_from("deadbeefdeadbeefdeadbeefdeadbeefdead")


if __name__ == "__main__":
    unittest.main()
