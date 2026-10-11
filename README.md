# job-agent

Queue-based internship application agent for the SimplifyJobs Summer 2027 list. Python, Playwright, SQLite, Flask.

## Setup
```bash
pip install -r requirements.txt && python -m playwright install chromium
cp profile.example.yaml profile.yaml      # fill in, set verified: true
export LLM_API_KEY=...                    # free tier is fine (config.yaml: Gemini OpenAI-compatible endpoint; any compatible URL works)
python -m jobagent ingest                 # pull listings.json from the repo into data/jobs.db
python -m jobagent login                  # headed browser on the persistent profile: sign into Simplify / any ATS once
python -m jobagent dry-run 10             # fill + validate, NEVER submits
python -m jobagent dashboard              # http://localhost:5055
```
Live mode is off by default. Turn it on from the dashboard ("Enable live submission") or `python -m jobagent enable-live`; both refuse unless `profile.yaml` is complete and `verified: true`.

## Commands
`ingest` `stats` `worker [--limit N]` `dry-run [N] [--platform p]` `answer` (CLI for the input queue) `retry-failed` `export` (data/applications.csv) `enable-live` `profile-check` `dashboard`

## How it decides
For each form control: saved answer library, then profile rules (name, email, school, sponsorship, ...), then voluntary EEO (defaults to "decline"), then for open questions a short LLM answer grounded only in `facts:` from your profile, filtered for banned phrases and em dashes. Anything unknown or legally sensitive goes to the input queue (grouped by question); answering once saves it and re-queues every waiting job. Legal declarations (criminal history, clearance, non-compete, etc.) are never auto-answered.

Statuses: APPLIED requires confirmation text on the page. A clicked submit with no confirmation is UNCERTAIN. Dry runs are parked (`last_error=DRYRUN_OK`) until live mode is enabled.

## Limits (what is not automated, on purpose or by necessity)
- **Simplify**: no public export API, and the extension's autofill cannot be driven reliably from automation. The profile is a local YAML you verify once. To load the extension anyway, unpack it and set `extension_path` in config.yaml; existing field values are never overwritten.
- **Accounts and passwords**: the agent never creates accounts, types passwords, or bypasses MFA/CAPTCHA. Workday/iCIMS/Taleo/Oracle/SuccessFactors jobs that land on a sign-in page are parked in the queue; sign in once via `login` and retry.
- Custom portals fall through to the generic engine; expect more NEEDS_USER_INPUT there.
- Cover-letter *file* uploads are not generated (text-box cover letters are answered like any question).

## Tests
`python -m pytest` runs ingest/dedupe and a headless browser against local mock forms (single page, multi-step, apply-button landing, combobox, validation, closed, already applied, captcha, login wall, silent submit, unknown/legal questions, voice filter).

## Responsible use
Dry-run is the default. The agent never solves CAPTCHAs, never creates accounts or types passwords, and never answers legal declarations (criminal history, clearances, non-competes) on its own. Check each site's terms before enabling live submission, and keep pacing conservative (`delay_seconds` in config.yaml).
