# Admin UI

The Forum ships with an optional admin UI mounted at `/admin` on the same
Cloud Run service that handles webhook traffic. It is intended for the
developer who owns the GCP deployment — the user who can already `gcloud
run services describe` and read Cloud Logging. It is **off by default** and
only mounts when OAuth credentials are configured.

## What you get

- **Agents list** — every agent in your `agents` Firestore collection, with
  the platforms each one supports and per-platform "last used" timestamps
  derived from recent session activity.
- **Agent detail** — agent metadata, configured platform connectors, the
  last 10 sessions, the most recent ERROR-or-higher Cloud Logging entry
  that mentions the agent, and a deep link to the Reasoning Engine in the
  GCP Console.
- **Scheduled jobs** — list, create, edit, and delete the same scheduled
  jobs the API serves under `/api/v1/scheduled-jobs`.

The theme is the Roman Forum the project is named after: travertine
backgrounds, oxblood accents, Cinzel display type. CSS lives in
`app/static/admin.css` and is hand-rolled — no Tailwind/Bootstrap.

## Auth model

Login is Google OAuth. After the OAuth dance, the admin UI uses the user's
access token to call Cloud Resource Manager `getIamPolicy` on the
deployment's GCP project. If — and only if — the signed-in account holds
the role configured by `ADMIN_REQUIRED_ROLE` (default `roles/owner`) as a
**direct** binding on the project, the session is established. Inherited
roles from folder/org bindings are intentionally not honored: that's the
narrowest interpretation of "owns this deployment."

The access token is stored in a signed session cookie (HttpOnly, Secure in
production, SameSite=Lax) and expires in roughly an hour. The same token is
used to call Cloud Logging and Vertex AI on agent detail pages, so log and
engine reads run with **your** permissions, not the service account's.

## Setup

### 1. Create an OAuth 2.0 Client ID

In the GCP Console for the project that hosts this Cloud Run service:

1. APIs & Services → Credentials → **Create credentials** → OAuth client ID.
2. Application type: **Web application**.
3. Authorized redirect URIs:
   - Production: `https://<your-cloud-run-host>/admin/auth/callback`
   - Local dev: `http://localhost:8080/admin/auth/callback` (Google accepts
     `http://localhost` but **not** `http://127.0.0.1`.)
4. Save and note the **Client ID** and **Client secret**.

### 2. Generate a session secret

```bash
openssl rand -hex 32
```

### 3. Set environment variables

Either via `.env` (for local dev) or Secret Manager + terraform (for Cloud
Run, see below).

```bash
OAUTH_CLIENT_ID=...
OAUTH_CLIENT_SECRET=...
OAUTH_REDIRECT_URI=https://<your-cloud-run-host>/admin/auth/callback
SESSION_SECRET=<openssl output>
ADMIN_REQUIRED_ROLE=roles/owner       # optional; default shown
CLOUD_RUN_SERVICE_NAME=the-forum      # optional; default shown
```

When any required value is blank, the admin UI is not mounted and all
`/admin/*` paths simply 404. Existing deployments without these values are
unaffected.

### 4. Terraform-managed deploys

The bundled terraform creates three new Secret Manager secrets and binds
them onto the Cloud Run service as env vars. Pass the values through
`TF_VAR_*` the same way `slack_signing_secret_value` is handled — see
[terraform/README.md](../terraform/README.md).

### 5. APIs to enable

The admin UI calls three Google APIs with the signed-in user's token:

- Cloud Resource Manager API (`cloudresourcemanager.googleapis.com`) — for
  the IAM owner check at login.
- Cloud Logging API (`logging.googleapis.com`) — for the last-error card.
- Vertex AI API (`aiplatform.googleapis.com`) — for Reasoning Engine
  metadata and the Console deep link.

The bundled terraform already enables Cloud Logging and Vertex AI for the
service. Cloud Resource Manager is typically already on in any GCP project
managed via terraform but enable it explicitly if `getIamPolicy` returns
`API not enabled`.

## Local development

```bash
pip install -r requirements.txt
# Fill OAUTH_* + SESSION_SECRET in .env (see step 3 above)
uvicorn app.main:app --reload --port 8080
# Open http://localhost:8080/admin/
```

The login page is the only thing visible without authenticating; private
information is rendered only after the IAM check passes.

## Security notes

- The signed session cookie is HMAC-signed (Starlette `SessionMiddleware`)
  but **not** encrypted. Anyone with the cookie value can read the OAuth
  access token within it. Tokens expire in ~1 hour and the cookie is set
  with `HttpOnly` and (in production) `Secure` + `SameSite=Lax`, so the
  exposure window is narrow and limited to a single trusted laptop.
- The IAM check looks for a **direct** project-level binding. If you grant
  yourself `roles/owner` only at the folder or org level, the admin UI
  will reject you. Add a direct project binding, or override
  `ADMIN_REQUIRED_ROLE` to a role you do hold directly.
- The `/admin/*` paths share auth boundary with the rest of the Cloud Run
  service. Webhook traffic from Slack / Chat / Telegram never sees the
  admin session cookie because requests come from different hostnames and
  different cookie scopes. There is no auth confusion between the two.

## Roadmap

Activity charts are stubbed in `agent_detail.html` under
`<section id="charts">`. A follow-up can drop Chart.js into the
`{% block scripts %}` slot in `app/templates/admin/base.html` and render
client-side from a small JSON endpoint — no route restructuring needed.

Other things considered but deferred:

- OAuth refresh-token storage (currently re-login on token expiry).
- Materialized counters for per-platform last-used (currently derived from
  the last 10 sessions per agent — fine until you have a lot of sessions).
- Concurrent-edit ETags on scheduled jobs (currently last-writer-wins).
