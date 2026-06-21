# CUBE Consulting — Project Acquisition Automation

Automates CUBE's weekday client outreach: sources fresh leads, drafts personalized cold emails, writes them to a Google Sheet for review, and sends the ones you approve via Gmail — then emails you a short summary. Send-only: the pipeline never reads any inbox.

The only recurring human action required is **marking which drafts to send** in the Sheet each morning.

## How it works

Two GitHub Actions cron jobs run every weekday:

| Job | Time (CT) | Does |
|---|---|---|
| `prepare` | 06:00 | Sources leads from Apollo (decision-makers) + the `Prospects`/CUBE alumni Sheets → dedupes → scores (UIUC alumni first) → drafts 15 personalized emails via Gemini → writes them to the `Drafts` tab for review |
| `send` | 10:00 | Sends every `Drafts` row you marked `approved` (up to 10, throttled 1 every 30s) via Gmail SMTP → marks them sent → emails you a short summary |

This gives you a 4-hour window to review and approve before send.

### Approving in the Sheet

`prepare` writes each draft as a row in the **`Drafts`** tab. To approve one, set
its **`approved`** column to `yes` (or `TRUE`). At 10am the `send` job mails
exactly the rows marked approved and unsent — nothing else goes out; leave a row
blank to skip it. The Sheet is the single source of truth, and the pipeline is
**send-only** (it never reads any inbox, so there's no reply parsing).

## Repository layout

```
src/
  main.py               # CLI: prepare / send / bootstrap
  models.py             # Pydantic: Lead, Draft, Reply, TemplateType
  templates.py          # 4 outreach templates copied from the docx
  past_projects.py      # Loads + matches past CUBE projects (credibility line)
  scoring.py            # Weighted lead scoring + hard filters
  template.py           # Industry → template router
  draft.py              # Claude personalization
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
.github/workflows/
  prepare.yml           # Cron 06:00 CT M-F
  send.yml              # Cron 10:00 CT M-F
```

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
