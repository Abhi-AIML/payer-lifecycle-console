# Security

A demo with synthetic data, built with production habits. This file lists each control and where it lives, so it can be checked.

## Identity and access

| Control | Where |
|---|---|
| Role checks on the server for every endpoint; the acting user comes from the session, never the request body | `backend/app.py` (`require`, `current_user`) |
| Separation of duties: whoever publishes rules cannot decide claims | `app.py` (`EDIT` vs `WORK`) |
| Passwords hashed with salted PBKDF2-HMAC-SHA256, 600,000 iterations; constant-time comparison | `backend/auth.py` |
| Same answer and timing for unknown users and wrong passwords | `auth.authenticate` |
| Lockout after 5 failures (trade-off: an attacker can lock an account for 60 s) | `auth.py` |
| Sessions: 256-bit random tokens, server-side; 1 h idle and 8 h absolute expiry | `auth.py` |
| Cookie: `HttpOnly`, `SameSite=Lax`, `Secure` over HTTPS (always on Cloud Run) | `app.login` |

## Web hardening

| Control | Where |
|---|---|
| Cross-site POST/PUT/PATCH/DELETE refused when `Origin` is another host | `app.security` middleware |
| `Content-Security-Policy` on pages: scripts and connections only to this origin, fonts only from Google Fonts, no framing, no plugins | `app.CSP` |
| `Strict-Transport-Security`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy`, `Permissions-Policy` | `app.security` |
| API responses `Cache-Control: no-store` | `app.security` |
| API docs (`/docs`, `/openapi.json`) off on Cloud Run | `config.API_DOCS` |
| No `Server` version header | `Dockerfile` |
| Output escaping in every page (`esc`) | `portals/shared.js` |

## Input and abuse

| Control | Where |
|---|---|
| Request bodies validated with size limits (Pydantic); uploads capped at 6 MB; documents at 200,000 characters | `app.py`, `service.py`, `casework.py` |
| Per-account limit on agent runs that can call Gemini (default 30 per 5 minutes), with `Retry-After` | `backend/ratelimit.py`, `app.throttled` |
| Outbound calls limited to the app's own host or loopback, so connection settings cannot be used for SSRF | `connectors._guard` |
| Sandbox endpoints require a bearer token, compared in constant time | `connectors.sandbox_auth` |
| AI output never decides amounts; every quote, value, amount and clause is verified in code before approval | `interpret.py`, `claims.py`, `denials.py` |

## Secrets and infrastructure

| Control | Where |
|---|---|
| Secrets in Secret Manager on Cloud Run; `.env` locally, git-ignored and excluded from the image | `deploy/setup.sh`, `.gitignore`, `.dockerignore` |
| No fixed fallback secrets: without `SANDBOX_TOKEN` a random token is generated per process | `connectors.py` |
| Gemini key sent in the `x-goog-api-key` header, never in a URL or log | `backend/ai.py` |
| Runtime service account can read only its three secrets; build account is separate | `deploy/setup.sh` |
| Container runs as an unprivileged user; image built from an allowlist of folders | `Dockerfile`, `.dockerignore` |
| Every audit event written to Cloud Logging as structured, labelled entries | `backend/logs.py`, `service.audit` |
| Tests gate every deploy | `cloudbuild.yaml` |

## Known limits (accepted for a demo)

- Demo accounts share one password so judges can sign in. Set `SHOW_DEMO_PASSWORD=false` to keep it off the page.
- No multi-factor sign-in. State, sessions and rate limits are in memory on a single instance.
- Inline scripts are allowed by the CSP (`'unsafe-inline'`) because the pages are built that way; all page output is escaped.
- No independent security review has been done.

Report a problem to the repository owner.
