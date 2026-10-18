# Where Gemini and Google services are used, and what can honestly be said

This file describes what the code does today, so any claim in a video, deck or proposal can be checked against it.

## Gemini in the code (backend/ai.py calls the Gemini REST API; key in `.env` locally, Secret Manager on Cloud Run)

| Step | What Gemini does | What code does afterwards |
|---|---|---|
| Read a policy PDF or image (`ai.transcribe`) | Transcribes it to text with page markers | Everything extracted next is verified against that text |
| Interpretation Agent (`interpret.gemini_extract`) | Proposes rules with page, clause and exact quote | Quote must exist, value must be in the quote, quote must sit in that page and clause |
| Configuration & Coding Agent (`configure.py`) | Suggests code families for an unmapped service | Suggestions only; a coding reviewer confirms |
| Intake Agent (`claims.gemini_bill`) | Reads a bill into lines | Every description and amount must appear in the bill text |
| Classification Agent (`denials.extract_letter`) | Reads a settlement letter in another format | Every amount must appear in the letter |
| Explanation Agent (`claims.run_explain`) | Writes the patient letter in the configured template | Every amount and clause must exist in the computed facts, or the draft is replaced |
| Drafting Agent (`denials.run_draft`) | Drafts the appeal, clarification or correction note | Same amount and clause verification |

**Amounts are never decided by Gemini.** The deterministic engine (`backend/engine.py`) computes every amount, and a person approves rules, decisions and letters. That is deliberate: it is what makes each decision explainable.

## Google Cloud (set up by `deploy/`, see docs/DEPLOY.md)

| Product | What it does here | Where in the repo |
|---|---|---|
| Cloud Run | Hosts the app over HTTPS, one instance, scales to zero | `Dockerfile`, `cloudbuild.yaml` |
| Cloud Build | Runs the tests, builds the image, deploys | `cloudbuild.yaml`, `deploy/deploy.sh` |
| Artifact Registry | Stores images, with a cleanup policy | `deploy/setup.sh`, `deploy/ar-cleanup-policy.json` |
| Secret Manager | Gemini key, sandbox token, demo password | `deploy/setup.sh`, `cloudbuild.yaml` (`--set-secrets`) |
| Cloud Logging | Structured audit trail (`labels.kind="audit"`) | `backend/logs.py` |
| Cloud Monitoring | Uptime check on `/healthz` | `deploy/deploy.sh` |

## Status today: check each before claiming it

- [ ] Gemini run live with a real key (`python scripts/check_gemini.py`, or Settings > Gemini > Test connection). Until then the app header says it uses the rule-based fallback.
- [ ] Deployed: `deploy/deploy.sh` succeeded and the URL is in the README.
- [ ] Uptime check created (Monitoring > Uptime checks).
- [ ] Audit entries visible in Logs Explorer with `labels.kind="audit"`.

**Google Antigravity:** say this only if the project was actually built or changed with it.
Do not claim a Google tool that was not used. The competition verifies the deployed link and the repository.
