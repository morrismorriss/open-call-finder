# AGENTS.md

Repository knowledge for Open Call Finder (`ocf`). See `README.md` for the user-facing guide.

## What this is
A small Python tool that reads artist open-call sources (RSS/Atom, Substack, scraped
webpages), filters them, optionally extracts details with Google Gemini, and writes rows
into a Notion database. It runs daily via GitHub Actions.

## Layout
- `ocf/main.py` – CLI + orchestration + run summary.
- `ocf/fetch.py` – polite HTTP client (robots.txt, delays), feed and webpage readers.
- `ocf/extract.py` – keyword filter, Gemini extraction, keyword-only fallback.
- `ocf/notion_db.py` – Notion schema, dedupe keys, read/write rows.
- `ocf/util.py` – URL normalisation, date parsing, log redaction.
- `config.yaml`, `sources.yaml` – settings and sources (no code changes needed to add sources).
- `tests/` – stdlib `unittest` regression tests.

## Running
```bash
pip install -r requirements.txt
python -m unittest discover -s tests          # tests (no network/credentials needed)
python -m ocf.main --dry-run                   # print only, touch nothing
python -m ocf.main --dry-run --no-ai --source "NYC Playwrights" --days 7
```

## Conventions and gotchas
- No third-party test runner is used; tests are plain `unittest` so CI needs no extra deps.
  Tests stub network and Notion (`_RecordingNotion`, fake extractors) and run the real
  `main()` orchestration.
- **Defer logic** (`main.py`): posts are only deferred to the next run when the AI *started*
  enabled and then ran out mid-run (`defer = extractor.enabled and ...` captured before the
  loop). A deliberate `--no-ai` / `ai.enabled: false` run must still add in keyword mode.
- **Dedupe keys** (`notion_db.dedupe_keys`): the `post:` key includes the call title, so the
  title must be filled *before* `dedupe_keys` is called (see `main.py`). Untitled calls in a
  roundup get distinct `"<post title> (n)"` titles so they don't collapse into one row.
- **SSRF guard** (`fetch._is_public_http_url`): page/feed-derived links are only followed if
  the scheme is http(s) and every resolved address is global. The configured source URL is
  trusted and not passed through the guard.
- Logs may be public (public repo runs), so anything printed goes through `redact()`; never
  print tokens, database IDs or API keys.
- GitHub Actions are pinned to commit SHAs (they handle secrets). Keep them pinned when bumping.
