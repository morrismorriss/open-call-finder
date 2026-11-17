# Open Call Finder

A small, free tool for artists. Every morning (about **6:07 AM US Eastern** by default) it:

1. reads the newsletters, feeds and websites listed in `sources.yaml`,
2. drops posts that obviously aren't open calls (keyword filter),
3. asks Google's free Gemini AI to pull out the details (deadline, fee, pay, location…),
4. skips anything already past its deadline or already in your database,
5. adds the rest as rows in an **Open Calls** database on a Notion page you choose.

"Open calls" means residencies, grants, fellowships, commissions, script and submission
calls, festivals, workshops, and jobs or gigs for artists.

It costs $0. It runs on GitHub's free computers (GitHub Actions), and uses the free tiers of
Notion's API and Google Gemini. If Gemini is missing or out of free quota, it still adds the
post's title, link and source (plus a deadline if it can spot one), and leaves the rest blank.

You don't need to know how to code. The steps below are all clicking and pasting.

The included `sources.yaml` lists some theater and writing sources to get you started.
Replace them with your own.

---

## One-time setup (about 20 minutes)

You'll collect three secret codes along the way. Paste them into a private note as you go.
**Never put them in any file in the project.** Your copy of the project may be public, and
GitHub keeps secrets separately, where nobody else can see them.

### Step 1: Create a Notion "integration" and connect it to a page

An integration is like a robot user that's allowed to edit one page.

1. In Notion, create (or pick) the page where you want the database. Any name works, for
   example "Open Calls Home".
2. Go to **https://www.notion.so/profile/integrations** (log in if asked).
3. Click **New integration** (or **+ New integration**).
4. Name: `Open Call Finder`. Workspace: the one with your page. Type: **Internal**.
   Click **Save**.
5. On the next screen, find **Internal Integration Secret**. Click **Show**, then **Copy**.
   → This is your **NOTION_TOKEN** (it starts with `ntn_` or `secret_`).
6. Open your page in Notion. Click the **•••** menu at the top right → **Connections**
   (sometimes called *Add connections*) → search for `Open Call Finder` → click it →
   **Confirm**.
   *(Another way: on the integration's settings page, open the **Access** tab and pick your page.)*
7. On your page, click **Share** → **Copy link**. Paste the link into your note.
   → This link is your **NOTION_PARENT_PAGE_ID**. The tool finds the ID inside the link for you.

### Step 2: Get a free Gemini key (optional, but recommended)

Without it, you'll still get rows, just with fewer details filled in.

1. Go to **https://aistudio.google.com/** and sign in with a Google account.
2. Click **Get API key** (left side or top right) → **Create API key**. If it asks for a
   project, let it create a new one.
3. Copy the key (a long string of letters and numbers; it may start with `AIza` or `AQ.`). → This is your **GEMINI_API_KEY**. Never paste it into a chat, email or file; it goes only into GitHub Secrets (Step 4).
4. **Don't add billing or a credit card** to that Google project. Without billing, Google can
   never charge you. You just hit a daily limit, and the tool switches to keyword-only mode
   for the rest of that day.

### Step 3: Get your own copy on GitHub

1. Make a free account at **https://github.com** if you don't have one, and log in.
2. On this project's GitHub page, click **Fork** (top right) → **Create fork**.
   Your copy has the same files, and none of anyone else's secrets or data.
3. In **your** copy, click the **Actions** tab. Click
   **I understand my workflows, go ahead and enable them**. GitHub turns schedules off in
   copies until you do this.

Public or private? Either works. Your keys are safe in both, because they live in GitHub
Secrets (Step 4), not in the files. Public repositories get unlimited free GitHub Actions
time. Private ones get 2,000 free minutes a month, which is still plenty (this uses about
3–10 minutes a day).

### Step 4: Add your secret codes (and, optionally, a sentence about you)

1. In your copy, click **Settings** (top bar) → **Secrets and variables** (left side) →
   **Actions**.
2. On the **Secrets** tab, click **New repository secret** and add each of these. The Name
   must be exactly as shown; the Secret is the value from your note:

   | Name | Value |
   |---|---|
   | `NOTION_TOKEN` | the integration secret from Step 1 |
   | `NOTION_PARENT_PAGE_ID` | the page link from Step 1 |
   | `GEMINI_API_KEY` | the key from Step 2 (skip if you didn't get one) |

3. **Optional, but it makes the AI's "relevant" judgment much better.** Switch to the
   **Variables** tab → **New repository variable**:
   - Name: `OCF_PROFILE`
   - Value: one sentence about you and your work, for example
     *"a Chicago-based choreographer and dance-theater maker who also writes."*

   It stays in your settings, not in the public files. It replaces the generic `profile` line
   in `config.yaml`.

You don't need `NOTION_DATABASE_ID`. The tool finds its database on your page by itself. Add it
only if you move the database to a different page (use the database's link).

### Step 5: Do a test run, then a real one

1. In your copy, click **Actions** → **Open Call Finder** (left side) → **Run workflow**
   (right side).
2. Tick **Dry run**, then click the green **Run workflow** button.
3. After a few seconds a run appears. Click it, then **find-calls**, then open
   **Find open calls** to read the log. You'll see every call it *would* add, and a summary
   at the bottom.
4. If that looks right, click **Run workflow** again, this time **without** Dry run. When it
   finishes, refresh your Notion page: there will be a new **Open Calls** database full of rows.

### Step 6: The daily schedule

Nothing to do. You'll see a run in the **Actions** tab every morning.

- GitHub can start scheduled runs 5–30 minutes late. That's normal.
- You'll see **two** runs a morning: one does the work, and the other stops right away with
  "Skipping: this is the other daylight-saving slot". That's how it stays at 6 AM Eastern
  through daylight saving time changes.
- If a run fails, GitHub emails you.
- **Public repositories only:** GitHub pauses schedules in a public repository after 60 days
  with no changes to it, and emails you first. To keep it going, click **Enable workflow** in
  the Actions tab, or make any small edit (even to this README).
- The tool remembers which posts it has already read in GitHub's private **Actions cache**,
  not in your files. If that cache is ever cleared, nothing breaks: the next run looks back
  a couple of weeks, and the duplicate check against Notion stops repeats.

---

## Everyday use

### Add a new source

1. In your copy, click `sources.yaml` → the **pencil icon** (Edit).
2. Copy one of the examples at the bottom and fill it in. For example:

   ```yaml
     - name: Theater Newsletter
       type: substack
       url: https://someone.substack.com/
   ```

   - **Substack:** use `type: substack` and the plain newsletter address.
   - **A blog or site with a feed:** use `type: rss`. Most WordPress sites have one at
     `/feed/`. Blogger sites have one at `/feeds/posts/default`.
   - **A page with no feed:** use `type: webpage`. You'll probably also need a
     `link_selector` that tells the tool which links on the page are posts. If you're not
     sure, ask an AI assistant to work it out for that page.
   - Keep the two spaces at the start of each line, like the entries above it. Don't use
     commas in `name`.
3. Click **Commit changes**. Test it from **Actions → Run workflow**: tick **Dry run** and
   type the source's name in the **source** box.

To pause a source without deleting it, add `enabled: false` under it.

### Change other settings

`config.yaml` (edit it the same way) controls:
- `skip_irrelevant`: set this to `true` to leave out calls the AI thinks don't fit your
  profile, like copywriting jobs from freelance newsletters. It starts as `false`.
- `keywords` / `exclude_keywords`: the first filter. Add words if real calls are being missed.
- the AI speed and limits (the defaults are already well under the free limits).

To change the time of day, edit the two `cron:` lines in `.github/workflows/daily.yml`, and the
two matching lines further down in the "Is it 6 AM in New York?" step. The format is
`minute hour * * *` in UTC: US Eastern is UTC−4 in summer and UTC−5 in winter. For another
time zone, also change `America/New_York` in that step and the `-4` / `-5` offsets.

---

## Saved views to set up in Notion (by hand, once)

Open the **Open Calls** database. Click the view name at the top left → **+ Add view**, then:

1. **Due in next 30 days**: Table view. **Filter** → Deadline → *is within* → *Next month*
   (or *is on or before* a relative date of "30 days from today"). **Sort** → Deadline → Ascending.
2. **No fee**: Table view. **Filter** → Fee → *contains* → `None`. Add a second rule, **or**
   Fee → *is empty*, if you also want calls whose fee wasn't stated.
3. **New this week**: Table view. **Filter** → Found on → *is within* → *Past week*.
   **Sort** → Found on → Descending.
4. **Calendar**: choose **Calendar** as the view type → **Show calendar by** → Deadline.

Also handy: a **Board** view grouped by **Type**, and a filter **Remote** → *is checked*.

---

## Running it on your own computer (optional)

You don't need this, because GitHub runs it for you. But to test locally:

```bash
pip install -r requirements.txt
```

```bash
python -m ocf.main --dry-run --source "NYC Playwrights" --days 7
```

- `--dry-run`: print what would be added; don't touch Notion or the saved state.
- `--source NAME`: check only that source (you can repeat it for several sources).
- `--days N`: look back N days instead of "since the last run".
- `--no-ai`: skip Gemini.
- `--max-posts N`: read at most N new posts per source.

For a real (non-dry) local run, set `NOTION_TOKEN`, `NOTION_PARENT_PAGE_ID` and, if you have
them, `GEMINI_API_KEY` and `OCF_PROFILE` as environment variables first.

---

## Limits: what isn't free or fully reliable

- **Members-only and paywalled content isn't read.** The tool never logs in anywhere.
  Automating a member login would likely break a site's terms. Locked newsletter posts are
  skipped. In the starter list, Playwrights' Center is switched off for this reason.
- **Pages without feeds** (`type: webpage`) depend on the site's layout. If a site is
  redesigned, that source logs an error until its `link_selector` is updated. The other
  sources keep working.
- **Some sites block automated visitors.** The tool identifies itself honestly, follows
  robots.txt and waits between requests. It never pretends to be a browser, so a site that
  blocks bots just shows up as an error for that source.
- **Roundup posts:** newsletters that list 20–30 jobs at once become 20–30 rows when Gemini is
  on, and one row when it's off. Use `skip_irrelevant: true` if that's too much.
- **Gemini's free tier:** Google changes the free models and limits from time to time, and
  doesn't promise them. The tool picks the newest free Flash model on its own, waits 8
  seconds between requests, stops at 80 requests a run, and falls back to keyword mode if
  the quota runs out. Google may use free-tier inputs to improve its products. The tool only
  sends public posts (plus your `OCF_PROFILE` sentence, if you set one).
- **AI accuracy:** extracted details (especially deadlines, fees and "remote") are usually
  right but sometimes wrong. Always check the official call before applying.
- **Duplicates across sources** are caught when two posts share the same apply link, or the
  same organization + deadline + type. A call reposted with a different link and no clear
  deadline can still show up twice.
- **Keyword mode** (no AI) guesses deadlines only when the post says something like
  "Deadline: December 1, 2026". Type is set to "Other", and other details are left blank.
- **Public repositories:** run logs can be read by anyone. They list the calls found (all
  public information) but never your keys, profile or Notion IDs. Schedules also pause after
  60 days without changes (see Step 6).
- **GitHub scheduling** can be late, and GitHub very occasionally skips a run during outages.

---

## Troubleshooting

| In the log | What to do |
|---|---|
| `NOTION_TOKEN is not set` | Step 4: check the secret name is exactly `NOTION_TOKEN`. |
| `couldn't connect to Notion … Could not find block` | The page isn't connected to the integration (Step 1, part 6). |
| `AI step off (no GEMINI_API_KEY set)` | Fine if you meant it; otherwise add the secret. |
| `Gemini key rejected` | Make a new key at aistudio.google.com and update the secret. |
| `no links matched selector` | That website changed layout; the `link_selector` needs updating. |
| `robots.txt asks bots not to read…` | The site has asked bots to stay away, so the tool respects that. Remove the source. |
| No runs appear in the morning | Actions tab → enable workflows (Step 3), or re-enable after 60 quiet days (Step 6). |

---

## What's in the folder

```
sources.yaml                 ← where to look (edit this)
config.yaml                  ← keywords, AI settings (edit this)
.github/workflows/daily.yml  ← the daily schedule
ocf/                         ← the program (you shouldn't need to touch it)
  main.py      runs everything and prints the summary
  fetch.py     reads feeds and web pages politely (robots.txt, delays, paywall skip)
  extract.py   keyword filter + Gemini + keyword-only fallback
  notion_db.py creates/reads/writes the Notion database
  util.py      link cleanup, date parsing
requirements.txt             ← Python libraries used
```

## License

MIT. Copy it, change it, share it.
