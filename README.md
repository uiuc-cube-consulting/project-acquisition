# CUBE Consulting — Project Acquisition Automation

Automates CUBE's weekday client outreach: sources fresh leads, drafts personalized cold emails, emails them to the approver for sign-off, sends approved drafts via Gmail, detects replies, schedules follow-ups, and emails a daily summary.

The only recurring human action required is **replying to one email each morning** to say which drafts to send.

## How it works

Two GitHub Actions cron jobs run every weekday:

| Job | Time (CT) | Does |
|---|---|---|
| `prepare` | 06:00 | Sources leads from People Data Labs (UIUC alumni first) + the `Prospects` tab + CUBE alumni Sheet → dedupes → scores → drafts 15 personalized emails via Gemini → writes to `Drafts` tab → **emails the approver a numbered list of every draft, inline** |
| `send` | 10:00 | **Reads the approver's reply to that email** and flips the approved rows → sends up to 10 via Gmail (throttled 1 every 30s) → checks Gmail for replies on prior threads → classifies replies → flags hot leads → drafts follow-ups after 3 business days → emails daily summary |

This gives the approver a 4-hour window to reply before send.

### Approving by reply (no spreadsheet, no uploads)

The 6am email lands in the approver's inbox (`mannat2@illinois.edu`) with every draft's full subject and body laid out and numbered. To approve, just **reply in that thread**:

| Reply | Sends |
|---|---|
| `approve all` | everything |
| `approve 1, 3, 5` | those drafts |
| `1-4` | drafts 1 through 4 |
| `skip 2` / `all but 2` | everything except 2 |
| `none` | nothing today |

At 10am the `send` job reads that reply straight from Gmail, parses it (Gemini, with a plain-text regex fallback), and flips exactly those rows to approved in the same Sheet — then sends. Nothing to open, nothing to upload. If no reply has arrived by 10am, nothing goes out that day and the batch is simply skipped. Editing the `approved` checkbox in the Sheet by hand still works too, if you ever prefer it.

The Sheet stays the single source of truth: `prepare` writes drafts there, the reply gate flips the `approved` cells there, and `send` reads them there — one continuous loop with no manual hand-off.

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
  gmail_send.py         # Gmail API send (impersonates projectacquisition@)
  reply_check.py        # Poll Gmail + Claude-classify replies
  approvals.py          # Read approver's reply to the digest → approve drafts
  follow_up.py          # 3-business-day follow-up drafter
  summary.py            # Daily digest + approval-request email
  sourcing/
    pdl.py              # People Data Labs Person Search wrapper (lead discovery)
    apollo.py           # (dormant) former Apollo wrapper, kept for reference
    cube_alumni.py      # Read CUBE alumni Sheet
config/
  scoring.yaml          # Tune lead scoring weights here
  industry_template_map.yaml  # Map industry → template
  search_profiles.yaml  # PDL search profiles (UIUC daily + rotated breadth)
data/
  past_projects.json    # 102 past projects parsed from Past Projects.docx
.github/workflows/
  prepare.yml           # Cron 06:00 CT M-F
  send.yml              # Cron 10:00 CT M-F
```

## One-time setup

### 1. People Data Labs (PDL) API key

Lead discovery runs on [People Data Labs](https://www.peopledatalabs.com/person-data).
The pipeline searches PDL for UIUC alumni in decision-maker roles first (our
highest-converting segment), then broadens to other Illinois executives / tech
founders only if alumni don't fill the daily target.

1. Create an account at https://www.peopledatalabs.com and grab your API key
2. **A paid tier with email access is required** — PDL's free tier blanks the
   `work_email` field, and without an email there's nothing to send. PDL bills
   per record returned, so the pipeline fetches roughly `DAILY_PREPARE_TARGET`
   records/day and runs the breadth search only to fill a gap.
3. Save the key for the `PDL_API_KEY` secret below

If `PDL_API_KEY` is unset, the pipeline still runs and sources from the free
`Prospects` tab / CUBE alumni Sheet only (no discovery).

### Free lead source: the `Prospects` tab

`bootstrap` creates a **`Prospects`** tab in the outreach Sheet. Paste prospective
clients there — one row each — and `prepare` reads them like any other lead.
Columns: `name`, `title`, `company`, `email`, `linkedin`, `industry`, `location`,
`is_uiuc_alum`. Only `name` and `email` are required; the rest sharpen the draft.
Set `is_uiuc_alum` to `true` only for genuine Illini (it adds a "fellow Illini"
line). Once a row is drafted it's copied into `Leads` and deduped, so it won't be
emailed twice — add new rows as you find them.

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

#### 3b. Authorize Workspace domain-wide delegation

The service account needs to *act as* `projectacquisition@cubeconsulting.org` to send mail and read replies.

1. In the service account, enable **"Domain-wide delegation"**, give it a product name, save
2. Note the service account's **Client ID** (numeric, ~20 digits)
3. Log in to https://admin.google.com as a Workspace super admin for `cubeconsulting.org`
4. Security → Access and data control → API controls → **Manage Domain Wide Delegation**
5. Add new → enter the Client ID, then these OAuth scopes (comma-separated):
   ```
   https://www.googleapis.com/auth/gmail.send,
   https://www.googleapis.com/auth/gmail.readonly,
   https://www.googleapis.com/auth/gmail.modify,
   https://www.googleapis.com/auth/spreadsheets,
   https://www.googleapis.com/auth/drive
   ```
6. Authorize. Changes can take up to 30 minutes to propagate.

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

# Smoke test without spending PDL credits / sending real mail
python -m src.main prepare --dry-run
# Should print 3 fake personalized drafts to stdout
```

### 6. Production: GitHub Actions secrets

In this repo on GitHub → Settings → Secrets and variables → Actions → New repository secret. Add:

| Secret | Value |
|---|---|
| `PDL_API_KEY` | from step 1 (People Data Labs; paid tier with email access) |
| `GEMINI_API_KEY` | from step 2 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | entire contents of the JSON file from step 3a |
| `SHEET_ID` | from step 4 |
| `ALUMNI_SHEET_ID` | from step 4 (optional) |
| `IMPERSONATE_EMAIL` | `projectacquisition@cubeconsulting.org` |
| `ORG_NAME` | `CUBE Consulting` |
| `ORG_PHYSICAL_ADDRESS` | `707 S 4th St, APT 1006A, Champaign IL 61820` |
| `UNSUBSCRIBE_MAILTO` | `unsubscribe@cubeconsulting.org` |
| `SENDER_NAME` | e.g. `Raghav Taneja` |
| `SENDER_PHONE` | e.g. `(555) 123-4567` |

`APPROVER_EMAIL` and `DIGEST_RECIPIENT` are **not** secrets — they're set directly in `.github/workflows/prepare.yml` and `send.yml` to `mannat2@illinois.edu`. Change them there to reroute the daily approval email.

Then go to Actions tab → `prepare` workflow → **Run workflow** → main. Watch it run. Repeat with `send` once you've replied to approve a draft.

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

- **Morning (anytime before 10am CT):** the 6am email arrives at `mannat2@illinois.edu` with every draft inline. Reply `approve all`, `approve 1,3`, `skip 2`, or `none`. (Editing the `approved` checkbox in the Sheet still works if you'd rather.)
- **After 10am:** check your inbox for the daily summary
- **Replies:** positive replies auto-route to the `Hot Leads` tab. The director takes over manually from there for the call → LOI conversation.
- **Don't-contact:** add an email to the `Suppression` tab and the system will never include them again.

## Tuning

- **Lower send cap while testing:** in `.github/workflows/send.yml`, change `DAILY_SEND_CAP: "10"` to `"3"` until quality is dialed in
- **Edit scoring weights:** `config/scoring.yaml` — bump `uiuc_alum` up if alumni outreach is your strongest channel
- **Change templates:** edit `src/templates.py` directly; Claude follows whatever structure you put there
- **Add PDL search profiles:** `config/search_profiles.yaml` — UIUC runs daily, breadth profiles rotate

## Cost ballpark (per weekday)

- People Data Labs: billed per record returned (~1 credit each); the pipeline fetches roughly `DAILY_PREPARE_TARGET` records/day (~15), breadth only to fill a gap
- Gemini: ~15 drafts + reply classification on `gemini-2.5-flash` / `gemini-2.5-flash-lite` fits inside the free tier's daily rate limits — $0/day
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
