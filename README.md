# CUBE Consulting — Project Acquisition Automation

Automates CUBE's weekday client outreach: sources fresh leads, drafts personalized cold emails, writes them to a Google Sheet for review, and sends the ones you approve via Gmail — then emails you a short summary. Afterwards it reads the mailbox (read-only) to record who replied, who bounced, and who was out of office, and turns all of it into a metrics dashboard.

## Current campaign: Spring 2027

Outreach runs a semester ahead — the Fall 2026 cycle is underway, so these emails
source projects for **Spring 2027**. Two things define the campaign, both set in
the workflow env (no code change needed to roll to the next term):

| Setting | Value | Effect |
|---|---|---|
| `TARGET_TERM` | `Spring 2027` | Named in the subject line and twice in the body. Substituted in Python, never by the model, so it cannot be paraphrased away. |
| `CAMPAIGN_START` | `2026-08-18` | Anyone first emailed on/after this date counts toward the Spring 2027 numbers on the dashboard. |
| `ALUMNI_TARGET_SHARE` | `0.35` | 35% of each batch to UIUC alumni, **65% to everyone else**. |
| `AUTO_APPROVE` | `1` | Drafts are written pre-approved; `send` mails them unattended. |
| `COMPANY_DEDUPE` | on | Never email two people at the same company. |
| `PACKET_URL` | *(not set — code default)* | Info-packet link in every first email. Deliberately **not** a GitHub secret: it is a public URL that appears in every email we send. The `fall2026` slug is intentional — the packet's contents are unchanged for Spring 2027, and the tinyurl is a redirect the team owns, so re-pointing it updates emails already sent. |

**Sending is unattended.** `prepare` writes drafts already marked approved and
`send` mails them the same morning, capped at `DAILY_SEND_CAP`. The suppression
list and the already-contacted dedupe still apply, so auto-approval cannot cause
a re-email. To put a human back in the loop, drop `AUTO_APPROVE` from
`.github/workflows/prepare.yml`.

### Reaching beyond UIUC alumni

This was broken, silently, for the whole Fall cycle. Selection used a single
alumni-first sort:

```python
filtered.sort(key=lambda x: (x.is_uiuc_alum, x.score), reverse=True)   # every alum outranks every non-alum
```

with a hard stop at `DAILY_PREPARE_TARGET`. Any day the Alumni tab held 15+
people — nearly every day — all 15 slots went to alumni and Apollo discovery
contributed **zero**. The result over two months: 444 alumni vs 62 non-alumni,
and the non-alumni only got through on the handful of days the alumni bench ran
dry. Nothing was failing in CI; the queue was simply never reached.

Selection now fills **two independent quotas** (`_Selector` in `src/main.py`),
so discovery gets guaranteed slots every day. Whichever pool comes up short
hands its slots to the other, so total volume never drops. The non-alumni half
comes from these Apollo profiles (`config/search_profiles.yaml`), three searched
per run on a daily rotation:

- `chicago_businesses` — Chicago-area owners and founders, 11–500 employees
- `startup_founders` — early-stage founders nationally
- `tech_founders` — software/tech founders and execs
- `big_tech` — product/eng/strategy leaders at 1,000+ employee tech companies
- `big_consulting` — practice leaders at consulting and professional-services firms
- `illinois_executives` — statewide Illinois decision-makers

The dashboard's Spring 2027 section tracks the non-alumni share against the 65%
target so this cannot silently regress again.

### One company, one conversation

Dedupe used to be per-person (email address + LinkedIn URL), so nothing stopped
the pipeline from working through **eleven people at PwC**, seven at Deloitte and
five each at Microsoft, RSM and United Airlines. Across the Fall cycle, 83 of 497
emails (17%) went to a company already pitched. That reads as spam from the
prospect's side and burns Apollo credits and send slots on an account we had
already contacted.

`src/companies.py` now tracks two identifiers, because neither alone is enough:

- **Normalized name** — works *before* an Apollo reveal, so a known company is
  dropped without spending a credit. Legal suffixes, region tags and generic
  descriptors are stripped, so `RSM US LLP`, `Deloitte Consulting LLP` and
  `Huron Consulting Group` collapse onto `rsm`, `deloitte`, `huron`. Descriptor
  stripping is guarded by `MIN_CORE_LEN`, so `Apex Systems` and `Apex Capital`
  stay distinct rather than both becoming `apex`.
- **Email domain** — only available after the reveal, but catches what a name
  never will: `PwC` and `PricewaterhouseCoopers` both land on `pwc.com`. Free
  providers (gmail, outlook, …) are ignored — they identify a person, not an
  employer.

The running list lives in the Sheet's **`Companies`** tab, seeded with all 420
companies contacted to date. It is auditable and hand-editable: **add a row by
hand and that company is permanently blocked.** Dedupe applies at three points —
the pre-reveal pool filter, within each reveal batch (two founders at the same
new company no longer both cost a credit), and at selection once the domain is
known. Set `COMPANY_DEDUPE=0` to disable.

The dashboard tracks `people per company`, whose target is 1.00.

### Gemini quota (why drafting is batched)

The free tier caps both requests/minute and requests/day, and the daily cap is
the binding one. One Gemini call per lead exhausted it mid-batch: a 15-lead run
lost 11 drafts to 429s **after** their Apollo credits had been spent, and spent
18 minutes asleep in retry backoff — past the workflow's old 15-minute timeout.

Three changes make the daily run fit:

- **Batched drafting** (`DRAFT_BATCH_SIZE`, default 5) — one call drafts five
  contacts, cutting daily requests ~5x. Anything the batch omits is retried
  individually, so a malformed entry costs one email, not the batch.
- **Proactive pacing** (`GEMINI_MIN_INTERVAL_SECONDS`, default 12.5) — stay under
  the per-minute limit instead of tripping it and eating a 60s backoff.
- **`timeout-minutes: 45`** on `prepare`, so a slow run finishes instead of dying.

A drafting shortfall now logs at ERROR and is recorded in the `Runs` tab's
`drafts_failed` column, because every lost draft is a wasted Apollo credit.

### Settings, secrets, and the empty-string trap

Only genuine credentials belong in GitHub secrets: `APOLLO_API_KEY`,
`GEMINI_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GMAIL_APP_PASSWORD`,
`GMAIL_ADDRESS`, `SHEET_ID`. Tuning knobs (`TARGET_TERM`, `ALUMNI_TARGET_SHARE`,
`AUTO_APPROVE`, …) are plain literals in the workflow so they are reviewable in
a diff, and non-secret values like `PACKET_URL` just use the code default.

This matters more than it looks. **A missing GitHub secret is exported as an
empty string, not as "unset"** — so `${{ secrets.NOPE }}` gives `NOPE=""`, and
`os.environ.get("NOPE", default)` returns `""` because the key does exist. The
damage ranged from loud to silent:

| Setting unset | Old behaviour |
|---|---|
| `DAILY_PREPARE_TARGET` | `int("")` → **ValueError, `prepare` dies on startup** |
| `DAILY_SEND_CAP` | `int("")` → **ValueError, `send` dies on startup** |
| `SENDER_NAME` / `SENDER_PHONE` | outreach signed by nobody, "reach me at ." |
| `ORG_NAME` / `ORG_PHYSICAL_ADDRESS` / `UNSUBSCRIBE_MAILTO` | broken CAN-SPAM footer on real mail |
| `PACKET_URL` | "take a look:" followed by nothing |

`src/env.py` (`env_str` / `env_int` / `env_float` / `env_flag`) treats blank as
absent, so every one of those now falls back to its documented default. Anything
a workflow might pass should be read through those helpers, not
`os.environ.get`.

The only recurring human action required is **keeping the Alumni tab stocked** —
everything else runs unattended.

## Metrics dashboard

```bash
python -m src.main replies      # scan the mailbox for replies + bounces (read-only)
python -m src.main report       # build dashboard/index.html
open dashboard/index.html
```

`report` writes a **single self-contained HTML file** — no server, no CDN, works
offline — so it can be emailed, dropped in Slack, or published to GitHub Pages
straight from `dashboard/`. It contains aggregate numbers and company names
only: no contact names, no email addresses. `dashboard/data.json` holds the same
figures for anything else you want to build.

It answers the questions worth asking about the pipeline:

| Metric | What it means |
|---|---|
| **Reply rate** | People who typed a real answer back, over **delivered** mail. Bounced addresses never reached a human, so counting them would understate the rate. |
| **Interested replies** | Replies Gemini labelled positive — wants a call, asks a qualifying question, or names the right person. The front of the project pipeline. |
| **Delivered / bounce rate** | How many sourced addresses actually exist. This is the accuracy half of lead sourcing, and it doubles as a sender-reputation warning: sustained bounce rates above ~5% get a sender throttled. |
| **Apollo email find rate** | How often a lookup returns an address at all, and — multiplied by the delivered rate — the share of Apollo credits that reach a real person. |
| **Funnel** | Sourced → drafted → sent → delivered → replied, with the conversion at each step. |
| **Alumni bench / runway** | How many people are left on the Alumni tab and how many business days that lasts at the current pace. |

Where the numbers come from: `replies` scans the sending mailbox over IMAP
(read-only — it cannot delete, move, or mark anything), matches each message
back to a lead by threading headers, subject, or sender, and sorts it into
`bounce` / `auto_reply` / `human`. Results land in the **`Replies`** tab and are
mirrored onto `Leads.status` / `Leads.replied_at`. Human replies are labelled
positive / neutral / negative / unsubscribe by Gemini. The `send` job runs this
automatically, so the numbers stay current without anyone remembering to.

A plain-text version of the same figures is written to the **`Dashboard`** tab
inside the Sheet (`python -m src.main stats`).

## How it works

Two GitHub Actions cron jobs run every weekday:

| Job | Time (CT) | Does |
|---|---|---|
| `prepare` | 06:00 M–F | Sources leads from Apollo discovery + the Alumni/`Prospects` Sheets → dedupes → scores → fills the alumni and non-alumni quotas → drafts 15 personalized emails via Gemini → writes them to `Drafts`, pre-approved |
| `send` | 10:00 M–F | Mails every approved, unsent `Drafts` row (up to `DAILY_SEND_CAP`, throttled 1 every 30s) via Gmail SMTP → marks them sent → scans the mailbox for replies/bounces → emails a short summary |
| `dashboard` | 08:00 Mon | Rescans the mailbox, rebuilds `dashboard/index.html`, and commits it — the weekly metrics refresh |

### The `approved` column

`prepare` writes each draft as a row in the **`Drafts`** tab. With `AUTO_APPROVE=1`
(the current setting) those rows arrive already ticked and the 10am `send` job
mails them. Without it, a human sets **`approved`** to `yes`/`TRUE` and only
those rows go out. Either way `send` mails exactly the rows that are approved
and unsent — clearing a checkbox before 10am pulls that email. The Sheet is the single source of truth. Sending is one-way
(SMTP); the only inbox access anywhere in the pipeline is the read-only IMAP scan
that records what came back, and it never approves or sends anything.

## Repository layout

```
src/
  main.py               # CLI: prepare / send / replies / report / stats / bootstrap
  models.py             # Pydantic: Lead, Draft, Reply, TemplateType
  metrics.py            # Every dashboard number, computed in one place
  replies.py            # Read-only IMAP scan: replies / bounces / auto-replies
  report.py             # Builds the standalone HTML dashboard
  report_template.html  # That dashboard's markup, CSS and charts
  dashboard.py          # Plain-text metrics into the Sheet's `Dashboard` tab
  env.py                # Env readers that treat a blank value as unset
  companies.py          # Company-level dedupe: normalization + the running list
  templates.py          # 4 outreach templates copied from the docx
  past_projects.py      # Loads + matches past CUBE projects (credibility line)
  scoring.py            # Weighted lead scoring + hard filters
  template.py           # Industry → template router
  draft.py              # Gemini personalization (fills {term} in Python)
  sheets.py             # Google Sheets data layer
  gmail_send.py         # Gmail SMTP send (App Password, send-only)
  follow_up.py          # 3-business-day follow-up drafter
  summary.py            # Daily summary email
  sourcing/
    apollo.py           # Apollo People Search wrapper (lead discovery)
    cube_alumni.py      # Read CUBE alumni Sheet
config/
  scoring.yaml          # Tune lead scoring weights here
  industry_template_map.yaml  # Map industry → template
  search_profiles.yaml  # Apollo search profiles (UIUC daily + rotated breadth)
data/
  past_projects.json    # 102 past projects parsed from Past Projects.docx
dashboard/
  index.html            # Built by `report` — the shareable metrics page
  data.json             # The same metrics as JSON
.github/workflows/
  prepare.yml           # Cron 06:00 CT M-F (source + draft, auto-approved)
  send.yml              # Cron 10:00 CT M-F (send + inbox scan)
  dashboard.yml         # Cron 08:00 CT Mon (weekly metrics refresh + commit)
```

### Sheet tabs

`Leads`, `Drafts`, `Alumni`, `Prospects`, `Suppression`, `Hot Leads`,
`Approvals`, plus three the metrics rely on:

- **`Replies`** — one row per person from the inbox scan: category (`human` /
  `auto_reply` / `bounce`), sentiment, subject, snippet, bounce reason. Rebuilt
  on every scan, so it is safe to delete.
- **`Runs`** — one row per `prepare`: lookups attempted vs emails found. This is
  what makes the Apollo find rate measured rather than inferred.
- **`Dashboard`** — the plain-text metrics summary.

## One-time setup

### 1. Apollo API key

Lead discovery runs on [Apollo](https://docs.apollo.io/reference/people-search).
The pipeline searches Apollo for UIUC alumni in decision-maker roles first (our
highest-converting segment, run every day), plus one rotated breadth profile.

1. In Apollo: Settings → Integrations → API → create a key, and **enable "Set as
   master key"** — the People Search endpoint requires a master API key.
2. **Plan note:** API access (incl. search) is on *all paid plans*; only rate
   limits/credits scale by tier. The **Free** plan returns `403 API_INACCESSIBLE`
   for search, so a paid plan is required. **Basic** (~$49/yr-billed, 2,500
   credits/mo) is the cheapest and is enough — search costs no credits; you only
   spend 1 credit per email revealed (~300/mo here, via bulk enrichment 10/call).
3. Save the key for the `APOLLO_API_KEY` secret below

If `APOLLO_API_KEY` is unset, the pipeline still runs and sources from the free
`Prospects` tab / CUBE alumni Sheet only (no discovery).

### Free lead source: the `Prospects` tab

`bootstrap` creates a **`Prospects`** tab in the outreach Sheet. Paste prospective
clients there — one row each — and `prepare` reads them like any other lead.
Columns: `name`, `title`, `company`, `email`, `linkedin`, `industry`, `location`,
`is_uiuc_alum`. Only `name` and `email` are required; the rest sharpen the draft.
Set `is_uiuc_alum` to `true` only for genuine Illini (it adds a "fellow Illini"
line). Once a row is drafted it's copied into `Leads` and deduped, so it won't be
emailed twice — add new rows as you find them.

### Targeting UIUC alumni: the `Alumni` tab

Apollo's API can't filter by school (and doesn't return education), so accurate
alumni targeting comes from **LinkedIn's Alumni tool**
(linkedin.com/school/university-of-illinois-urbana-champaign/people) — filter UIUC
alumni by employer/role, then paste them into the **`Alumni`** tab. Columns:
`name`, `company`, `linkedin`, `title`, `industry`, `location`, `email`.

**Only `name` + `company` are required** — if `email` is blank, `prepare` looks it
up via Apollo enrichment (a `linkedin` URL improves the match rate). Every row is
treated as a UIUC alum: flagged `is_uiuc_alum`, **ranked ahead of all other
leads**, and drafted with the "fellow Illini" angle. This is the highest-converting
channel, so keep this tab stocked.

### 2. Gemini API key (free tier)

1. Go to https://aistudio.google.com/apikey → Create API key
2. The free tier covers this workload (daily drafts + reply classification) at no cost — no payment method required
3. Save the key

### 3. Google Cloud setup

#### 3a. Create a GCP project + service account

1. Open g and create a project named e.g. `cube-outreach`
2. Enable APIs: **Gmail API** and **Google Sheets API** and **Google Drive API**
3. IAM & Admin → Service Accounts → Create Service Account
   - Name: `cube-outreach-bot`
   - Skip role assignment
4. Open the service account → Keys → Add Key → Create new key → JSON
5. Download the JSON file — this becomes the `GOOGLE_SERVICE_ACCOUNT_JSON` secret

#### 3b. Gmail App Password (for sending)

Outreach is sent from a single Gmail account over SMTP — **no domain-wide
delegation needed** (the service account above is only for Sheets). On the
sending account:

1. Turn on **2-Step Verification** (https://myaccount.google.com/security)
2. Create an **App Password** at https://myaccount.google.com/apppasswords
3. Save the account address + the 16-char password → these become the
   `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` secrets.

A dedicated Gmail (e.g. `cube.outreach@gmail.com`) is recommended over a personal
inbox for deliverability and separation. Note: many `*.edu` accounts disable App
Passwords, so use a regular `gmail.com` account.

### 4. Create the outreach Sheet

1. Create a new Google Sheet named e.g. `CUBE Outreach Pipeline`
2. Share it with the service account's email (found in the JSON, looks like `cube-outreach-bot@cube-outreach.iam.gserviceaccount.com`) as **Editor**
3. Copy the Sheet ID from the URL (`https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`)
4. *(Optional)* Do the same for the existing CUBE Alumni Sheet — share with the service account as **Viewer**, copy its ID

### 5. Local test

```bash
git clone <this repo>
cd project-acquisition
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in keys + IDs in .env, then:
set -a; source .env; set +a

# Initialize the Sheet tabs (one-time)
python -m src.main bootstrap

# Smoke test without spending Apollo credits / sending real mail
python -m src.main prepare --dry-run
# Should print 3 fake personalized drafts to stdout
```

### 6. Production: GitHub Actions secrets

In this repo on GitHub → Settings → Secrets and variables → Actions → New repository secret. Add:

| Secret | Value |
|---|---|
| `APOLLO_API_KEY` | from step 1 (Apollo; Basic plan recommended for credits) |
| `GEMINI_API_KEY` | from step 2 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | entire contents of the JSON file from step 3a |
| `SHEET_ID` | from step 4 |
| `ALUMNI_SHEET_ID` | from step 4 (optional) |
| `GMAIL_ADDRESS` | the sending Gmail address (from step 3b) |
| `GMAIL_APP_PASSWORD` | the 16-char App Password (from step 3b) |
| `ORG_NAME` | `CUBE Consulting` |
| `ORG_PHYSICAL_ADDRESS` | `707 S 4th St, APT 1006A, Champaign IL 61820` |
| `UNSUBSCRIBE_MAILTO` | `unsubscribe@cubeconsulting.org` |
| `SENDER_NAME` | e.g. `Raghav Taneja` |
| `SENDER_PHONE` | e.g. `(555) 123-4567` |

`APPROVER_EMAIL` and `DIGEST_RECIPIENT` are **not** secrets — they're set directly in `.github/workflows/prepare.yml` and `send.yml`. They're only the recipient of the daily summary email; approval itself happens in the Sheet.

Then go to Actions tab → `prepare` workflow → **Run workflow** → main. Watch it run, mark a draft `approved` in the Sheet, then run `send`.

After verifying both workflows work, the cron schedules take over and run automatically Mon–Fri.

## Smoke test (end-to-end, ~15 minutes)

1. `python -m src.main bootstrap` — creates the 5 tabs in your Sheet (incl. `Approvals`)
2. `python -m src.main prepare --dry-run` — confirm drafts print to stdout
3. Run `prepare` for real (small batch): `DAILY_PREPARE_TARGET=2 python -m src.main prepare` → check that the numbered approval email lands at `mannat2@illinois.edu`
4. **Reply to that email** with `approve all` (or `approve 1`)
5. `DAILY_SEND_CAP=1 python -m src.main send --dry-run` — verify the log shows the reply being parsed and the would-send list
6. Drop `--dry-run`: `DAILY_SEND_CAP=1 python -m src.main send` → check the recipient inbox
7. Reply to the outreach email as the recipient
8. Run `python -m src.main send` again → confirm `Hot Leads` row appears, lead status flips to `hot`, summary email arrives

## Day-to-day operation

- **Morning (anytime before 10am CT):** open the `Drafts` tab and set `approved` to `yes` on the rows you want to send (leave the rest blank).
- **After 10am:** check your inbox for the daily summary of what went out.
- **Replies from prospects** land in the sending account's own inbox — handle them there manually (the pipeline is send-only and doesn't track replies).
- **Don't-contact:** add an email to the `Suppression` tab and the system will never include them again.

## Tuning

- **Lower send cap while testing:** in `.github/workflows/send.yml`, change `DAILY_SEND_CAP: "10"` to `"3"` until quality is dialed in
- **Edit scoring weights:** `config/scoring.yaml` — bump `uiuc_alum` up if alumni outreach is your strongest channel
- **Change templates:** edit `src/templates.py` directly; Gemini follows whatever structure you put there
- **Add Apollo search profiles:** `config/search_profiles.yaml` — UIUC runs daily, breadth profiles rotate

## Cost ballpark (per weekday)

- Apollo: 1 credit per email unlocked; the pipeline only unlocks emails for the ~`DAILY_PREPARE_TARGET` leads it actually selects (~15/day ≈ ~300/mo)
- Gemini: ~15 drafts/day on `gemini-2.5-flash` fits inside the free tier's daily rate limits — $0/day
- GitHub Actions: free for the cron schedule (well under the 2,000 free minutes/month)

## Out of scope (v1)

- LinkedIn auto-DM (ToS-risky, defer)
- Phone outreach
- LOI / contract automation
- Multi-step nurture beyond a single follow-up
- Web dashboard (Sheets is enough)

## Maintenance notes for successors

- The cron times are in UTC and don't auto-adjust for daylight saving. Twice a year (March + November) you'll see jobs run an hour earlier/later in CT than expected — either accept it or update the cron expressions in `.github/workflows/`.
- `data/past_projects.json` is parsed once from the docx. If you update Past Projects.docx, regenerate by running:
  ```bash
  python -c "from docx import Document; import json, re; \
    doc = Document('Past Projects.docx'); \
    out = []; \
    [out.append({'semester': c[0].text.strip(), 'client': c[1].text.strip(), \
                 'keywords': [k.strip() for k in re.split(r'[,\n]', c[2].text) if k.strip()], \
                 'deliverables': c[3].text.strip()}) \
     for t in doc.tables for r in t.rows[1:] for c in [list(r.cells)] \
     if len(c) >= 4 and c[0].text.strip() and c[1].text.strip()]; \
    open('data/past_projects.json','w').write(json.dumps(out, indent=2))"
  ```
