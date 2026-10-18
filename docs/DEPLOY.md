# Deploying to Google Cloud

The console runs on **Cloud Run**, built and deployed by **Cloud Build**, with secrets in **Secret Manager**.
Everything is sized to stay inside the free tier. A billing account (or hackathon credits) must be linked to the project:
Cloud Run, Cloud Build, Artifact Registry and Secret Manager require one even when usage is free.

## What you need

- A Google Cloud project with billing or credits linked.
- A Gemini API key from https://aistudio.google.com/apikey.
- Nothing installed locally: use **Cloud Shell** (the `>_` button in the Cloud Console), which has `gcloud`, `git` and `python3`.

## 1. One-time setup (about 3 minutes)

In Cloud Shell:

```bash
git clone <your-repo-url> payer-console && cd payer-console
gcloud config set project YOUR_PROJECT_ID
bash deploy/setup.sh
```

It asks for the Gemini key (input hidden) and a demo password, then creates:

| Resource | Name | Why |
|---|---|---|
| Artifact Registry repo | `payer` (us-central1) | Container images; a cleanup policy keeps the newest 3 |
| Service account | `payer-console-run` | Runs the app. Can read its three secrets and nothing else |
| Service account | `payer-console-build` | Cloud Build: deploys to Cloud Run, pushes images, reads uploaded source |
| Secrets | `gemini-api-key`, `sandbox-token`, `demo-password` | Mounted into Cloud Run as environment variables |
| Bucket | `gs://PROJECT_cloudbuild` | Where `gcloud builds submit` uploads the source |

## 2. Deploy

```bash
bash deploy/deploy.sh
```

Cloud Build then:

1. **test**: installs `requirements-dev.txt` and runs all tests. A failure stops here, before anything is built.
2. **build** and **push**: builds the `Dockerfile` (only `backend/`, `data/`, `portals/` go in) and pushes it to Artifact Registry.
3. **deploy**: Cloud Run, one instance at most, scale to zero, secrets from Secret Manager.

The script prints the service URL, checks `/healthz`, and creates a Cloud Monitoring uptime check on it.
Put the URL in the README and the submission.

Private repository? Judges need both the deployed link and repository access: add them as collaborators.

To keep the password off the sign-in page and hand it to judges privately instead:
`SHOW_DEMO_PASSWORD=false bash deploy/deploy.sh`

## 3. Optional: deploy on every push (CI/CD)

Cloud Console, **Cloud Build > Triggers > Connect repository** (GitHub), then **Create trigger**:
event *Push to a branch*, branch `^main$`, configuration *Cloud Build configuration file* `cloudbuild.yaml`,
service account `payer-console-build`. From then on every push to main is tested and, if green, deployed.

## 4. Optional: a budget alert

**Billing > Budgets & alerts > Create budget**, amount e.g. 1 (your currency), alerts at 50 % and 100 %.
It only notifies; it does not stop services. With these settings a demo stays within the free tier.

## What stays free, and why

| Setting | Effect |
|---|---|
| `--min-instances=0` | No instance runs (or bills) while nobody uses the demo |
| `--cpu-throttling` | CPU is billed only while a request is being served |
| `--max-instances=1` | Caps cost, and keeps the in-memory demo state and sessions on one instance |
| Artifact Registry cleanup policy | Keeps storage under the free 0.5 GB |
| Per-account agent limit (`AGENT_RUNS_PER_WINDOW`, default 30 per 5 min) | Protects the Gemini free-tier quota |

The uptime check sends a request every 5 minutes. That keeps the instance warm (so the shared demo state is not lost to
scale-to-zero while judges review) at a few seconds of billed CPU a day. Delete the check after judging if you prefer.

## Operating it

- **Logs:** Logging > Logs Explorer, resource *Cloud Run Revision*. Audit trail only: `labels.kind="audit"`.
- **Rotate a secret:** `printf 'NEW' | gcloud secrets versions add demo-password --data-file=-`, then `bash deploy/deploy.sh` (new instances read `latest`). Disable the old version in Secret Manager.
- **Reset the demo state:** deploy again, or `gcloud run services update payer-console --region=us-central1 --update-labels=reset=$(date +%s)` (starts a fresh instance).
- **Tear down after the competition:**
  ```bash
  gcloud run services delete payer-console --region=us-central1
  gcloud artifacts repositories delete payer --location=us-central1
  gcloud secrets delete gemini-api-key; gcloud secrets delete sandbox-token; gcloud secrets delete demo-password
  ```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `PERMISSION_DENIED` on `gcloud builds submit` | Your user needs Owner or Editor plus *Service Account User* on `payer-console-build`. Re-run `deploy/setup.sh`. |
| Deploy step: secret not found | Run `deploy/setup.sh`; it creates the three secrets. |
| App header says "rule-based fallback" | The Gemini key is missing or invalid. Settings > Gemini > Test connection shows the error type. |
| Sign-in works but you are bounced back | The session cookie is `Secure`: use the `https://` URL. |
