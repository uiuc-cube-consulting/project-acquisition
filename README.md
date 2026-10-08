# CUBE Consulting — Project Acquisition Automation

Automates CUBE's weekday client outreach: sources fresh leads, drafts personalized cold emails, writes pre-approved drafts to a Google Sheet, and sends approved, unsent rows via Gmail — then emails you a short summary. Afterwards it reads the mailbox (read-only) to record who replied, who bounced, and who was out of office, and turns all of it into a metrics dashboard.

## Current campaign: Spring 2027

Outreach runs a semester ahead — the Fall 2026 cycle is underway, so these emails
source projects for **Spring 2027**. These settings define the campaign through
workflow env values and code defaults (no code change needed to roll to the next term):

| Setting | Value | Effect |
|---|---|---|
| `TARGET_TERM` | `Spring 2027` | Named in the subject line and twice in the body. Substituted in Python, never by the model, so it cannot be paraphrased away. |
| `CAMPAIGN_START` | `2026-08-18` | Anyone first emailed on/after this date counts toward the Spring 2027 numbers on the dashboard. |
| `ALUMNI_TARGET_SHARE` | `0.35` | 35% of each batch to UIUC alumni, **65% to everyone else**. |
| `ENTERPRISE_TARGET_SHARE` | `0.35` | 35% of each batch reserved for big, well-known companies (see below). |
| `ENTERPRISE_COMPANIES_PER_RUN` | `12` | How many uncontacted companies from `config/enterprise_targets.yaml` to search per run. |
| `AUTO_APPROVE` | `1` | Drafts are written pre-approved; `send` mails them unattended. |
| `COMPANY_DEDUPE` | on | Excludes known companies during preparation. |
| `PACKET_URL` | *(not set — code default)* | Info-packet link in every first email. Deliberately **not** a GitHub secret: it is a public URL that appears in every email we send. The `fall2026` slug is intentional — the packet's contents are unchanged for Spring 2027, and the tinyurl is a redirect the team owns, so re-pointing it updates emails already sent. |

**Sending is unattended.** `prepare` writes drafts already marked approved and
`send` mails them the same morning, capped at `DAILY_SEND_CAP`. The suppression
list is checked during preparation; sending skips previously contacted addresses
for initial outreach. Follow-ups are a separate, disabled-by-default path. To put a human back in the loop, drop `AUTO_APPROVE` from
`.github/workflows/prepare.yml`.

### Who we email

Apollo's People Search cannot filter by school. UIUC alumni come from the
Sheet's `Alumni` tab (name + company, with Apollo enrichment for missing emails),
the optional CUBE alumni Sheet, or explicitly flagged `Prospects` rows. The
`cube_member` column on `Alumni` selects the warmer former-member template.

`_Selector` in [src/main.py](src/main.py) fills three quotas: 35% alumni
(`ALUMNI_TARGET_SHARE`), 35% non-alumni at large companies
(`ENTERPRISE_TARGET_SHARE`), and the remainder other discovery. With the default
target of 15, rounding gives 5 alumni, 5 enterprise and 5 other slots. Unfilled
slots are offered to the other pools; available contacts and the reveal budget
still limit the final batch.

Other discovery combines manually entered `Prospects` with three rotating
Apollo breadth profiles per run. Enterprise sourcing searches enterprise-tier
profiles and a subset of [config/enterprise_targets.yaml](config/enterprise_targets.yaml)
each run. See [config/search_profiles.yaml](config/search_profiles.yaml) for
the current profiles rather than adding a school filter to an Apollo search.

Preparation excludes known emails/LinkedIn URLs, suppressed addresses,
non-alumni `@illinois.edu` addresses, and companies already in the pipeline.
[config/scoring.yaml](config/scoring.yaml) defines scoring and exclusion settings;
[src/companies.py](src/companies.py) implements company-name/domain matching
against `Leads` and `Companies`. Scores order the alumni and general discovery
pools; enterprise candidates alternate by source, preferring available emails
and then score within each source.

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

The checked-in workflows currently read credentials, sender/footer settings,
summary recipients, `DAILY_PREPARE_TARGET` and `DAILY_SEND_CAP` from GitHub
Actions secrets. Campaign settings such as `TARGET_TERM`, `ALUMNI_TARGET_SHARE`,
`ENTERPRISE_TARGET_SHARE` and `AUTO_APPROVE` are workflow literals;
`PACKET_URL` uses its code default. See the [production settings table](#6-production-github-actions-secrets).

The maintainer confirmed that `DAILY_PREPARE_TARGET` and `DAILY_SEND_CAP`
should remain GitHub Actions secrets. Change those repository secrets to tune
volume; the workflows continue to read their existing secret references.

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

Scheduled runs are unattended. Keep the source tabs stocked, review failures,
and handle replies and suppression as described under day-to-day operation.

## Metrics dashboard

Open [dashboard/index.html](dashboard/index.html) in a browser to view the
committed snapshot. A maintainer with Sheets access can rebuild it with
`python -m src.main report --open`; this reads Sheets and writes local files.
It does not rescan the inbox. The scheduled `dashboard` workflow runs the inbox
scan before rebuilding the report. Local inbox checks must use
`python -m src.main replies --dry-run --no-classify` against test resources only
(see [Smoke test](#smoke-test)).

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
positive / neutral / negative / unsubscribe by Gemini. The `send` workflow runs a separate `replies` step
automatically, so the numbers stay current without anyone remembering to.

A plain-text version of the same figures is written to the **`Dashboard`** tab
inside the Sheet (`python -m src.main stats`).

## How it works

Three GitHub Actions workflows run on these UTC schedules; CT shifts with daylight saving:

| Workflow | UTC schedule | CT (winter / summer) | Does |
|---|---|---|---|
| [prepare](.github/workflows/prepare.yml) | 12:00 M–F | 06:00 / 07:00 | Sources, filters and scores contacts; fills three quotas; drafts up to `DAILY_PREPARE_TARGET` emails; writes `Leads`, pre-approved `Drafts`, `Companies`, `Runs` and the Sheet dashboard |
| [send](.github/workflows/send.yml) | 16:00 M–F | 10:00 / 11:00 | Sends approved, unsent drafts up to `DAILY_SEND_CAP`, spaced by `SEND_INTERVAL_SECONDS` (30); updates sent records and emails a summary; then runs a separate inbox scan |
| [dashboard](.github/workflows/dashboard.yml) | 13:00 Mon | 07:00 / 08:00 | Rescans replies, rebuilds the HTML/JSON dashboard and commits it |

The `send` CLI command sends mail and the summary; it does **not** invoke
`replies` itself. The workflow provides that second step. Both workflows that
scan replies allow a scan failure without failing the whole job.

### The `approved` column

`prepare` writes each draft as a row in the **`Drafts`** tab. With `AUTO_APPROVE=1`
(the current workflow setting) those rows arrive already ticked and the morning `send` job
mails them. Without it, a human sets **`approved`** to `yes`/`TRUE` and only
those rows go out. Either way `send` mails exactly the rows that are approved
and unsent — clearing `approved` before the send job reads the Sheet removes it from that run. The Sheet is the single source of truth. Sending is one-way
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
  templates.py          # Outreach templates, former-member variant and footer
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
  search_profiles.yaml  # Rotating breadth + enterprise Apollo profiles
  enterprise_targets.yaml # Named companies for enterprise sourcing
data/
  past_projects.json    # Past projects used for credibility matching
dashboard/
  index.html            # Built by `report` — the shareable metrics page
  data.json             # The same metrics as JSON
.github/workflows/
  prepare.yml           # 12:00 UTC M-F (source + draft, auto-approved)
  send.yml              # 16:00 UTC M-F (send + separate inbox scan)
  dashboard.yml         # 13:00 UTC Mon (weekly metrics refresh + commit)
```

### Sheet tabs

[TAB_HEADERS in src/sheets.py](src/sheets.py) currently defines **nine** tabs.
`bootstrap` creates missing tabs and repairs their headers; it can also remove
the default `Sheet1`. [src/dashboard.py](src/dashboard.py) creates `Dashboard`
separately when metrics are refreshed, bringing the total to ten.

| Tab | Purpose |
|---|---|
| `Leads` | Contacts in the pipeline, status, timestamps and message IDs |
| `Drafts` | Email content, `approved`, send timestamps and errors |
| `Alumni` | UIUC alumni input; `cube_member` marks former CUBE members |
| `Prospects` | Manually entered contacts with known email addresses |
| `Suppression` | Addresses to exclude during preparation |
| `Companies` | Company names/domains excluded during preparation |
| `Replies` | Inbox scan results, one row per person per category; rebuilt on a live scan |
| `Runs` | Preparation sourcing, reveal and draft counts |
| `Hot Leads` | Legacy tab still created by bootstrap; the current pipeline does not populate it |
| `Dashboard` | Aggregate Sheet metrics, written separately |

There is no `Approvals` entry in `TAB_HEADERS`; approval uses `Drafts.approved`.
Positive replies set `Leads.status` to `hot` and appear in `Replies`.

## One-time setup

Production provisioning below is for maintainers. Contributors working on an
issue do not need production credentials or new personal Apollo/Gemini keys.
Start with [Local test](#5-local-test).

### 1. Apollo API key

Use the organization's existing `APOLLO_API_KEY` for live sourcing. The client
uses People API Search for candidates and Bulk People Enrichment for email
reveals; see [src/sourcing/apollo.py](src/sourcing/apollo.py). Search does not
reveal an email; enrichment spends credits. Reveals happen near selection, with
a budget of twice `DAILY_PREPARE_TARGET`, so failed lookups and exclusions can
make credit use exceed the final number of drafts.

Apollo does not identify UIUC alumni by school. See [Who we email](#who-we-email)
and [config/search_profiles.yaml](config/search_profiles.yaml) for targeting.
Without an Apollo key, preparation uses Sheet sources with existing emails;
alumni rows needing an email lookup cannot be enriched.

### Source tabs: `Prospects` and `Alumni`

`Prospects` columns are `name`, `title`, `company`, `email`, `linkedin`,
`industry`, `location`, `is_uiuc_alum`. Name and a valid email are required;
the other fields improve scoring and drafting. Flag only confirmed UIUC alumni.

`Alumni` columns are `name`, `company`, `linkedin`, `title`, `industry`,
`location`, `email`, `cube_member`. The team identifies alumni using LinkedIn's
Alumni tool and enters them here. Name + company suffice for Apollo lookup;
name + email allow sourcing without a lookup. Every row is treated as an alum
and competes within the alumni quota. Mark `cube_member` true only for former
CUBE members. Live preparation caches resolved emails or `NOT_FOUND` in this
tab so failed lookups are not repeatedly charged.

### 2. Gemini API key

Maintainers configure the organization's `GEMINI_API_KEY`. Drafting uses the
model named by `GEMINI_DRAFT_MODEL` (default `gemini-2.5-flash`, in
[src/draft.py](src/draft.py)); reply sentiment uses `GEMINI_CLASSIFY_MODEL`
(default `gemini-3.5-flash-lite`, in [src/replies.py](src/replies.py)). Both are
GitHub Actions repository **variables**, not secrets, so a retired model can be
swapped under Settings → Secrets and variables → Actions → Variables without a
code change. Unset, they fall back to the defaults. Requests are paced and drafts are batched,
but quota exhaustion can still cause drafting shortfalls. Do not create a
personal key to experiment with real contact details or reply text.

### 3. Google Cloud and Gmail setup

Create a Google Cloud project in the [Cloud console](https://console.cloud.google.com/),
enable the Google Sheets and Drive APIs, and create a service account with a
JSON key. Its JSON becomes `GOOGLE_SERVICE_ACCOUNT_JSON`. For local test
resources, `GOOGLE_SERVICE_ACCOUNT_FILE` can instead point to a private key file
(the code defaults to `service_account.json`). Never commit either credential.

The service account accesses Sheets. Gmail uses a separate account and App
Password over SMTP and read-only IMAP; the bot does not use the Gmail API or
domain-wide delegation. On the sending account, enable
[2-Step Verification](https://myaccount.google.com/security), create an
[App Password](https://myaccount.google.com/apppasswords), and configure
`GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD`. The account must permit IMAP access.
`you@example.com` is a placeholder in examples, not a usable Gmail account.

### 4. Create the outreach Sheet

Create the workbook and share it with the service account as Editor. Set
`SHEET_ID` to the identifier between `/d/` and `/edit` in its URL. A maintainer
initializes it with `bootstrap` as part of provisioning; that command writes
to Sheets and has no dry-run option.

Optionally share a separate CUBE alumni workbook with the service account as
Viewer and set `ALUMNI_SHEET_ID`. Its `Alumni` worksheet is read by
[src/sourcing/cube_alumni.py](src/sourcing/cube_alumni.py).

### 5. Local test

Use Python 3.11 (the workflow version) and git. Claim the issue and wait for
maintainer assignment before opening a PR. Branch from `main`; **never push
directly to `main`**, which scheduled workflows use for production.

From a fresh clone, in Bash:

```bash
git clone https://github.com/uiuc-cube-consulting/project-acquisition.git
cd project-acquisition
git switch -c docs/5-readme-refresh
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -c "import src.main; print('ok')"
python -m pytest
python -m src.main prepare --dry-run --help
python -m src.main send --dry-run --help
python -m src.main replies --dry-run --help
```

In Windows PowerShell, create the environment with `py -3.11 -m venv .venv`
and activate it with `.\.venv\Scripts\Activate.ps1`; the remaining Python
commands are the same. The help commands exit before running any pipeline.
The import, tests and help checks above need no credentials and contact no
services.

**Current integration limits:** `.env.example` is not present on this revision;
[#4](https://github.com/uiuc-cube-consulting/project-acquisition/issues/4) adds it.
Do not run a copy command for a file that is not there. The CLI already loads
a local `.env` automatically if one exists. No `.env` is needed for the checks
above, and credentials must stay out of commits.

[#18](https://github.com/uiuc-cube-consulting/project-acquisition/issues/18)
tracks credential-free, write-free `prepare --dry-run`. **That behavior is not
implemented yet:** it constructs `SheetClient`, bootstraps/reads the Sheet,
then sends fictional fixture details to Gemini for drafting. It avoids live
Apollo sourcing and returns before appending leads/drafts, but can still
create tabs or change headers. Without Sheets credentials it fails before
drafting. Do not point it at production resources.

Coordinate these instructions with #4 and #18 when they merge: use the actual
example file and verify the new dry-run contract before removing these limits.
Until then, the credential-free contributor check is the sequence above;
maintainers handle the integration smoke test below using isolated resources.

### Running tests

The suite in `tests/` runs offline: no `.env`, no credentials, and nothing
touches Apollo, Gmail, Gemini, or the Sheet.

```bash
python -m pip install -r requirements-dev.txt  
pytest                                        
pytest tests/test_env.py -v                  
```

Run it from the repo root. `scoring.py` and `template.py` open `config/*.yaml`
relative to the current directory, so running from inside `tests/` breaks
them. `pyproject.toml` points pytest at `tests/` and puts the repo root on the
import path, which is why plain `pytest` can import `src`.

New tests go in `tests/test_<module>.py`, named after the file in `src/` they
cover, and must keep the suite offline. Set environment variables with
pytest's `monkeypatch` fixture rather than the real environment, fake network
clients instead of calling them, and use only made-up `example.com` addresses
in fixtures.

### 6. Production: GitHub Actions secrets

Maintainers configure repository secrets under Settings → Secrets and
variables → Actions. This table describes the current references in
[prepare.yml](.github/workflows/prepare.yml), [send.yml](.github/workflows/send.yml)
and [dashboard.yml](.github/workflows/dashboard.yml); no workflow settings are
changed by this documentation update.

| Secret | Purpose / example |
|---|---|
| `APOLLO_API_KEY` | Organization's Apollo key |
| `GEMINI_API_KEY` | Organization's Gemini key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service-account JSON for Sheets |
| `SHEET_ID` | Outreach workbook ID |
| `ALUMNI_SHEET_ID` | Optional separate CUBE alumni workbook ID |
| `GMAIL_ADDRESS` | Sending account; placeholder `you@example.com` |
| `GMAIL_APP_PASSWORD` | Sending account's App Password |
| `ORG_NAME` | Organization name, such as `CUBE Consulting` |
| `ORG_PHYSICAL_ADDRESS` | Actual postal address for live mail; placeholder `123 Main St`. Live preparation refuses a blank value |
| `UNSUBSCRIBE_MAILTO` | Monitored unsubscribe address; placeholder `you@example.com` |
| `SENDER_NAME` | Sender's name; placeholder `Jane Doe` |
| `SENDER_PHONE` | Sender's phone; placeholder `202-555-0100` |
| `APPROVER_EMAIL` | Summary recipient; placeholder `you@example.com` |
| `DIGEST_RECIPIENT` | Fallback summary recipient if `APPROVER_EMAIL` is blank |
| `DAILY_PREPARE_TARGET` | Preparation target; unset/blank defaults to 15 |
| `DAILY_SEND_CAP` | Send cap; unset/blank defaults to 10 |

If both summary-recipient settings are blank, summaries go to `GMAIL_ADDRESS`.
Despite its name, `APPROVER_EMAIL` only chooses the summary recipient.
The current workflows set `AUTO_APPROVE=1`, so triggering live preparation can
queue mail for the next scheduled send without human review. Maintainers must
complete a controlled integration check before enabling production schedules.

## Smoke test

Contributors use the credential-free Local test checks. The integration steps
below require a **maintainer-provisioned test Sheet, a dedicated test mailbox
and organization-managed test credentials**. Use only fictional contacts such
as `Jane Doe` at `you@example.com`; never copy production data into the test
Sheet, logs, fixtures or PR. The test Sheet needs initialized headers and a
synthetic approved draft to exercise send preview: prepare's dry run does not
persist its drafts.

1. Preview preparation with `python -m src.main prepare --dry-run`.
   It needs Sheets access and Gemini to generate drafts. Expect the fixture
   sourcing log and up to three printed drafts, depending on filtering and
   successful model responses. Apollo is skipped. Confirm no lead/draft rows
   are appended; tabs/headers may still be written by bootstrap.
2. Preview sending with `python -m src.main send --dry-run`.
   Set `DAILY_SEND_CAP=1` in the test environment first. Sheets access is
   required, and `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` must be set because the
   sender constructor reads them (dummy values suffice for SMTP dry run).
   Expect a would-send log for the synthetic approved, unsent draft and no
   SMTP send, sent timestamp or summary. **This can still write `send_error`
   cells for duplicate drafts or errors.** It does not scan replies.
3. Preview inbox matching with
   `python -m src.main replies --dry-run --no-classify`.
   This connects to the test mailbox over read-only IMAP using valid test Gmail
   credentials and reads the test Sheet. It prints matched results without
   writing reply/status rows. `--no-classify` skips Gemini; `--dry-run` alone
   does not skip classification. With no matching test messages, zero matches
   is expected. It never creates a `Hot Leads` row.
4. **Maintainer-only final live check:** in an isolated test configuration,
   initialize tabs with `bootstrap`, prepare a small controlled batch, verify
   `Drafts.approved`, send to a maintainer-controlled test recipient, and reply
   from that mailbox. Run the inbox scan and verify `Replies`, `Leads.status`
   (`hot` for a positive classified reply, otherwise `replied` for a human
   reply), `replied_at`, the summary and both dashboards. Only this final step
   may omit `--dry-run`. Keep test recipient details private and publish only
   aggregate outcomes. Do not trigger production workflows as a smoke test.

## Day-to-day operation

- Keep `Alumni` and `Prospects` stocked and maintain enterprise targets.
  The scheduled prepare workflow auto-approves initial drafts; daily manual
  approval is not required. For manual review, set `AUTO_APPROVE=0` in
  `prepare.yml` and explicitly clear any existing approvals that should wait.
  Review new rows and set `Drafts.approved` to `yes`/`TRUE` before sending.
- Check workflow logs and `Runs` for sourcing or drafting shortfalls. Preparation
  targets 15 by default, while sending caps at 10; approved drafts can accumulate.
- After the send workflow, check the summary and `Replies`. The workflow's
  separate IMAP scan records human replies, auto-replies and bounces; Gemini
  labels human sentiment when available. Staff still respond to people manually.
- For do-not-contact requests, add the address to `Suppression` and clear any
  already-approved pending drafts for it. Preparation checks suppression, but
  `cmd_send` currently does not recheck it. The inbox scan labels unsubscribes
  but does not automatically add them to `Suppression` (tracked in
  [#16](https://github.com/uiuc-cube-consulting/project-acquisition/issues/16)).
- Review Sheet metrics and the weekly HTML dashboard. A failed reply scan can
  leave metrics stale even when sending succeeds. Follow-ups are disabled unless
  `ENABLE_FOLLOW_UPS=1`; staff handle repeat outreach manually by default.

## Tuning

| Change | Current control |
|---|---|
| Batch size / daily send cap | Repository secrets `DAILY_PREPARE_TARGET` / `DAILY_SEND_CAP`, defaults 15 / 10. Change the secrets currently referenced by the workflows; there is no literal `DAILY_SEND_CAP: "10"` to edit |
| SMTP spacing | `SEND_INTERVAL_SECONDS` in `send.yml`, currently 30 seconds |
| Manual review | `AUTO_APPROVE` in `prepare.yml`; 1 auto-approves new initial drafts, 0 leaves them unchecked |
| Audience mix | `ALUMNI_TARGET_SHARE` and `ENTERPRISE_TARGET_SHARE` in `prepare.yml`. Alumni and enterprise slots are rounded; general discovery gets the remainder. Keep dashboard campaign settings aligned |
| Apollo rotation | `DISCOVERY_PROFILES_PER_RUN` and [config/search_profiles.yaml](config/search_profiles.yaml); breadth profiles rotate, enterprise-tier profiles run each time when the enterprise share is positive |
| Named enterprise searches | [config/enterprise_targets.yaml](config/enterprise_targets.yaml) and `ENTERPRISE_COMPANIES_PER_RUN`, currently 12 |
| Ranking | [config/scoring.yaml](config/scoring.yaml); scoring weights do not replace audience quotas |
| Company exclusions | [src/companies.py](src/companies.py) and the `Companies` tab; `COMPANY_DEDUPE` defaults on |
| Templates / routing | [src/templates.py](src/templates.py) and [config/industry_template_map.yaml](config/industry_template_map.yaml) |
| Gemini models | Repository variables `GEMINI_DRAFT_MODEL` / `GEMINI_CLASSIFY_MODEL`; unset uses the code defaults `gemini-2.5-flash` / `gemini-3.5-flash-lite` |
| Gemini batching / pacing | `DRAFT_BATCH_SIZE` (default 5) and `GEMINI_MIN_INTERVAL_SECONDS` (default 12.5); add workflow env overrides if needed. Missing batch drafts are retried individually |
| Campaign / packet | `TARGET_TERM`, `CAMPAIGN_START` and optional `PACKET_URL`; keep campaign settings consistent across workflows |

Keep the volume caps in repository secrets, as confirmed by the maintainer.

## Cost and limits

Apollo email enrichment consumes credits even when a revealed contact is later
filtered out or drafting fails. `Runs` records attempted reveals, emails found
and draft failures. Gemini quotas depend on the organization's allocation;
batching and pacing reduce requests but do not guarantee every draft succeeds.
Check actual service usage rather than assuming a fixed daily cost.

## Out of scope

- LinkedIn auto-DM
- Phone outreach
- LOI / contract automation
- Multi-step nurture beyond the optional single follow-up

The HTML dashboard is implemented; see [Metrics dashboard](#metrics-dashboard).

## Maintenance notes for successors

- Cron expressions are UTC and do not adjust for daylight saving. Use the
  schedule table above when planning local checks.
- Maintain [data/past_projects.json](data/past_projects.json) in the schema read
  by [src/past_projects.py](src/past_projects.py). The original source document
  is not checked into this repository.
- Keep PRs scoped to one issue and include `Closes #5` for this documentation
  change. Before review, run the import and tests from Local test and check the
  diff for credentials, local `.env` files and real contact data. Use fictional
  examples at `example.com`; never publish Sheet contents or inbox screenshots.
