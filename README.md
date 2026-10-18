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
python -m jobagent dashboard              # http://127.0.0.1:5055
```
Live mode is off by default. Turn it on from the dashboard ("Enable live submission") or `python -m jobagent enable-live`; both refuse unless `profile.yaml` is complete and `verified: true`.

The dashboard's **Final submission behavior** setting controls browser handoff: **Manual review** keeps tabs open both when an application needs an answer/file and when it reaches the final Submit button, including in dry-run mode; **Auto-submit, hand off if blocked** submits automatically in live mode but leaves the tab open when anti-bot protection rejects the click; **Auto-submit only** never pauses for that website handoff. Pause preserves open review tabs, while Stop ends the browser session and closes them. Graduation questions are derived from the verified preflight date (for example, `2028-05` becomes `Spring 2028`, `May`, or `2028` according to the question).

## Premium resume editor and multiple resume sources
The dashboard accepts multiple PDF, DOCX, TXT, or self-contained LaTeX (`.tex`) resumes. Mark one as **primary**; that is the only source that can be submitted. Older or specialized resumes stay **supplementary** and are used only as evidence for skills and experience.

The existing `llm` configuration remains the low-cost/local orchestrator for parsing the job posting. Premium resume tailoring is enabled only when the primary source is a `.tex` file. The selected `resume_editor` receives the complete extracted text of every primary and supplementary resume plus the editable non-Education sections of the LaTeX source. It returns structured exact-text patches, not a complete resume. The app permits changes inside any section except Education, but locks every section header and all LaTeX structure. It deliberately does not reject a patch because its own parser did not recognize a skill, date, metric, or other fact; the user reviews factual accuracy before approving the tailored resume. The app applies patches locally, confirms that the command and brace structure did not change, and compiles the result with Tectonic in untrusted mode. PDF, DOCX, and TXT primary resumes are submitted unchanged and are never reformatted by the model. The authoritative rules are in [`docs/premium-resume-editing-contract.md`](docs/premium-resume-editing-contract.md).

The tailored PDF is cached per job/source version, and every tailored resume requires explicit review before it can be uploaded to an application. The same premium provider writes open-ended application essays from a smaller, contact-free evidence packet. Essay drafts require review unless **Generated essay answers** is explicitly set to **Fill automatically** in Applicant preflight.

For every premium resume evaluation, **Recent events** explicitly reports `CHANGE`, `NO CHANGE`, or `ERROR`, along with the model's reason, accepted edit count, affected sections, and ATS keywords. The editor is instructed to prefer a few truthful, job-relevant skills or bullet-wording improvements; `NO CHANGE` is reserved for cases where no safe change would improve the match. A zero-edit decision keeps the original resume and does not create an unnecessary review task.

Before the dashboard can start the worker, complete and verify **Applicant preflight**. It stores contact, education, work authorization/sponsorship, and common eligibility answers in the local `profile.yaml`. Voluntary race, gender, ethnicity, veteran, and disability answers are also available there; the default remains **Decline to self-identify**. Unsupported or legal questions still pause in the human queue.

The dashboard offers three provider modes:

- **ChatGPT plan (default):** click **Continue with ChatGPT**. Eligible Plus and Pro accounts can authorize this local app without an API key. The OAuth credentials are stored only in `data/provider_credentials.json` with owner-only file permissions. The app discovers the models available to the signed-in account and uses OpenAI's required streamed, non-stored Responses flow. Manage or limit its allowance in [ChatGPT Settings → Usage](https://chatgpt.com/#settings/Usage).
- **Gemini API:** create a key in [Google AI Studio](https://aistudio.google.com/app/apikey), select Gemini in the dashboard, and paste the key. `gemini-3.8-flash` is the default because Google currently describes it as its most intelligent Flash model and lists it as free-tier eligible. Free-tier availability and quotas can change, and Google states that free-tier content may be used to improve its products. You can use `GEMINI_API_KEY` instead of saving a key in the dashboard.
- **OpenAI API key:** the previous `RESUME_LLM_API_KEY` path remains available as a fallback and uses normal API billing.

Useful advanced overrides are `RESUME_LLM_PROVIDER`, `RESUME_LLM_MODEL`, and `RESUME_LLM_BASE_URL`. Keep resume-provider credentials out of `LLM_API_KEY` if you want every non-resume call to remain on the free/local endpoint. No model can guarantee an ATS pass, so the review gate is intentionally retained.

For a strictly local/open-source orchestration model, point the existing compatible client at your server, for example `LLM_BASE_URL=http://127.0.0.1:11434/v1 LLM_MODEL=<your-local-model>`. Premium-editor credentials remain separate from that general model configuration.

## Commands
`ingest` `stats` `worker [--limit N]` `dry-run [N] [--platform p]` `answer` (CLI for the input queue) `retry-failed` `export` (data/applications.csv) `enable-live` `profile-check` `dashboard`

## How it decides
For each form control: saved answer library, then profile rules (name, email, school, sponsorship, ...), then voluntary EEO (defaults to "decline"), then for open questions a short LLM answer grounded only in `facts:` from your profile, filtered for banned phrases and em dashes. Anything unknown or legally sensitive goes to the input queue (grouped by question); answering once saves it and re-queues every waiting job. Legal declarations (criminal history, clearance, non-compete, etc.) are never auto-answered.

When exact normalized text does not match, the inexpensive/local model compares the new question with saved answers and unresolved queue questions. It reuses or groups them only at high confidence when the same literal answer still means the same thing; minor wording and synonym changes are allowed, but changed polarity, employer, country, timeframe, threshold, or scope are rejected. Essays, legal declarations, and sensitive demographic questions are excluded from semantic reuse.

Statuses: APPLIED requires confirmation text on the page. A clicked submit with no confirmation is UNCERTAIN. Dry runs are parked (`last_error=DRYRUN_OK`) until live mode is enabled.

The dashboard can revert one unsubmitted application or all pending work. Reverting stops the worker, clears unresolved questions and generated-resume review state for those jobs, and resets them to `NOT_STARTED`. `APPLIED`, `ALREADY_APPLIED`, and `UNCERTAIN` jobs are protected because resetting local state cannot undo or safely determine an external submission.

## Limits (what is not automated, on purpose or by necessity)
- **Simplify**: no public export API, and the extension's autofill cannot be driven reliably from automation. The profile is a local YAML you verify once. To load the extension anyway, unpack it and set `extension_path` in config.yaml; existing field values are never overwritten.
- **Accounts and passwords**: the agent never creates accounts, types passwords, or bypasses MFA/CAPTCHA. Workday/iCIMS/Taleo/Oracle/SuccessFactors jobs that land on a sign-in page are parked in the queue; sign in once via `login` and retry.
- Custom portals fall through to the generic engine; expect more NEEDS_USER_INPUT there.
- Cover-letter *file* uploads are not generated (text-box cover letters are answered like any question).

## Tests
`python -m pytest` runs ingest/dedupe and a headless browser against local mock forms (single page, multi-step, apply-button landing, combobox, validation, closed, already applied, captcha, login wall, silent submit, unknown/legal questions, voice filter).

## Responsible use
Dry-run is the default. The agent never solves CAPTCHAs, never creates accounts or types passwords, and never answers legal declarations (criminal history, clearances, non-competes) on its own. Check each site's terms before enabling live submission, and keep pacing conservative (`delay_seconds` in config.yaml).
