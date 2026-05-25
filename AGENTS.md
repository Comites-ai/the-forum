# AGENTS.md — Instructions for AI Coding Assistants

This file orients AI coding assistants (Claude Code, Cursor, Copilot,
Aider, etc.) working in The Forum's codebase. The Forum has two
distinct audiences, and the right behavior differs between them:

- **Operators** run a deployment of The Forum and run agents against
  it. They configure their own GCP project; they don't modify The
  Forum's source. → [Section 1](#section-1-instructions-for-coding-assistants-of-people-who-are-deploying-the-forum-and-running-agents)
- **Contributors** improve The Forum itself and open pull requests
  against `main`. → [Section 2](#section-2-instructions-for-coding-assistants-of-people-who-are-working-on-improving-the-forum-and-contributing-to-the-open-source-codebase)

If the human you're working with is unclear which mode they're in,
ask before doing scope-expanding or destructive work.

**Project-level context** lives on the website rather than in this
repo. [comites.ai/architecture](https://comites.ai/architecture)
explains *why* the design looks the way it does (the cross-project
SA pattern, the per-agent GCP project for blast-radius isolation,
the Forum's central Reasoning Engine choice).
[comites.ai/developers](https://comites.ai/developers) is the
six-step builder walkthrough that points at this repo and Agent-Template.

---

## Section 1: Instructions for Coding Assistants of People Who Are Deploying The Forum and Running Agents

This section applies when:

- The human is deploying The Forum from this repo into their own GCP
  project.
- The human is creating, updating, or operating an agent that talks to
  their deployed Forum.
- The human is editing their own `terraform/terraform.tfvars`, `.env`,
  or `providers.tf`, but not the rest of the source.

It does **not** apply when the human is editing The Forum's source code,
opening a PR, or adding a feature. For that, jump to
[Section 2](#section-2-instructions-for-coding-assistants-of-people-who-are-working-on-improving-the-forum-and-contributing-to-the-open-source-codebase).

### Infrastructure changes go through Terraform, not gcloud

**Never make GCP infrastructure changes directly via `gcloud` CLI
commands.** All infrastructure must be managed through Terraform
(`terraform/`).

- Any new resource (secret, service account, IAM binding, Cloud Run
  env var, scheduler job, bucket, etc.) must be added to the
  appropriate `.tf` file before being applied.
- Run `terraform plan` first, review the diff, then `terraform apply`.
- After applying, commit the updated `.tf` files so state and code
  stay in sync.
- The only exception is populating secret **values** (not creating the
  secret resource itself), which must be done via
  `gcloud secrets versions add` after Terraform creates the secret
  shell.

### Terraform state lives in GCS, not locally

The Forum's terraform uses a `backend "gcs"` block pointing at
`{project_id}-terraform-state`. Don't expect or create a local
`terraform.tfstate` file — if you see one in the working tree, it's
stale (from a pre-remote-backend era) and should be moved aside
before running `terraform init`.

The state bucket itself is managed by terraform with versioning + a
90-day lifecycle on archived versions. Don't recreate or clean it
out by hand.

If `terraform/providers.tf` is missing on a fresh clone, copy from
`providers.tf.example` and update the bucket name. The actual
`providers.tf` is gitignored on purpose — its `bucket = ` field is
operator-specific.

### To create a new agent, go to Agent-Template — not this repo

The agent-developer onboarding flow lives in
[github.com/Comites-ai/Agent-Template](https://github.com/Comites-ai/Agent-Template).
Its `get_started_linux.sh` bootstraps everything: GCP project
creation, terraform apply, secret population, ADK deploy, and
registration into The Forum's Firestore via its `register_agent.py`.

**Don't try to add a new agent by editing files in this repo.** If
the human asks how to create a new Slack/Telegram/Discord/Google
Chat bot, point them at Agent-Template and clone it for them. The
Forum-side surface this repo exposes for agent developers is
intentionally contract-only — see `docs/FOR_AGENT_DEVELOPERS.md`.

### Operator-side scripts live in `scripts/`

These run **from this repo** against the Forum's Firestore / Cloud
Run — they're operator tools, not part of an agent's repo:

- `scripts/provision_scheduler_api_key.py` — generates a scheduler
  MCP API key for an agent. Writes the SHA-256 hash to the agent's
  Firestore doc and prints the plaintext once.
- `scripts/link_identities.py` /
  `scripts/check_user_identities.py` — merge cross-platform user
  identities (e.g., link a Telegram identity to an existing
  Slack/Google Chat user).
- `scripts/enable_google_chat_agent.py` — toggle Google Chat on for
  an existing agent's Firestore record.
- `scripts/deploy_agent.py` — legacy registration. New agents
  should use Agent-Template's `register_agent.py` instead.

### Configuration files that never get committed

- `terraform/terraform.tfvars` — your project ID, region, feature
  flags. Gitignored.
- `terraform/providers.tf` — your backend config. Gitignored.
- `.env` — runtime config (Slack signing secrets, GCS bucket name,
  OAuth client/secret, scheduler MCP key, etc.). Gitignored.

If you find any of these staged for commit, unstage them before
committing. They contain values specific to one operator's deployment.

### Be cautious with destructive operations

`terraform destroy`, `gcloud secrets delete`, force-overwrites of
Cloud Run revisions, deleting state bucket versions — these can
take down production. Always confirm before running, and prefer
narrower commands (`terraform destroy -target=...`,
`gcloud run services update-traffic --to-revisions=...`) over
wholesale destroys.

### Discord, the admin UI, and other optional pieces

Optional capabilities are gated behind `use_discord` /
`enable_admin_ui` etc. in `terraform.tfvars`. If the human wants to
turn one on:

- **Discord**: see `docs/DISCORD_WORKER.md` — the worker is a
  separate multi-tenant Compute Engine VM that holds Gateway
  WebSockets. Per-agent bot tokens live in each agent's own GCP
  project, not the Forum's.
- **Admin UI**: see `docs/ADMIN_UI.md` for OAuth client setup.

---

## Section 2: Instructions for Coding Assistants of People Who Are Working on Improving the Forum and Contributing to the Open Source Codebase

This section applies when:

- The human is editing files in `app/`, `terraform/`, `scripts/`,
  `tests/`, `docs/`, `discord-worker/`, etc.
- The human is opening or reviewing a pull request against `main`.

### Pre-PR checks must all pass

CI (`.github/workflows/`) runs three jobs. Reproduce them locally
before pushing:

```bash
# 1. Python tests
pytest -v

# 2. Terraform formatting
terraform fmt -check -recursive terraform/

# 3. Shell linting (only if scripts/*.sh changed)
for f in scripts/*.sh; do bash -n "$f"; done
shellcheck -S error scripts/*.sh
```

Setup, once: `python -m venv .venv && .venv/bin/pip install -r
requirements-dev.txt`. CI uses Python 3.11. Don't push a red branch
to save time on the round-trip.

### Architecture in one paragraph

The Forum is a FastAPI app on Cloud Run. Each chat platform has a
*connector* under `app/services/platforms/` and a *route handler*
under `app/api/v1/`. Both feed into `MessageProcessorV2`
(`app/services/message_processor_v2.py`) which talks to Vertex AI
Reasoning Engines. Discord is the exception — its Gateway WebSocket
can't deliver to HTTP webhooks, so a separate multi-tenant
`discord-worker/` Compute Engine VM holds Gateway connections per
agent and POSTs normalized events to the Forum. Sessions, users,
agents, scheduled jobs, and the scheduler-MCP API-key hashes all
live in Firestore. The scheduler MCP server (the *only* MCP server
the Forum hosts) is mounted at `/api/v1/mcp/scheduler/`.

### Don't bundle agent-developer setup back into this repo

After the Agent-Template extraction, `docs/FOR_AGENT_DEVELOPERS.md`
is **contract-only** — it describes what The Forum sends to agents,
what services the Forum hosts, and how the Forum handles agent
errors. Per-platform setup recipes live in Agent-Template.

Resist the urge to:

- Add "Creating a Brand New Agent on <platform>" walkthroughs back
  here. They belong in Agent-Template's README and bootstrap script.
- Recreate `docs/terraform-templates/agent-project/` or
  `docs/scripts/register_agent_template.py`. Canonical versions live
  in Agent-Template.
- Add to `FOR_AGENT_DEVELOPERS.md` anything that isn't a Forum-side
  contract (message shape, hosted services, runtime error handling,
  identity linking, Forum-side troubleshooting).

If you're updating something in both repos at once (e.g., adding a
new chat platform), confirm with the human that the dual change is
intentional rather than silently duplicating one repo's content into
the other.

### Adding a new chat platform

`docs/ADDING_A_NEW_PLATFORM.md` is the reference. The work spans
**two repos**:

- **This repo** — new connector under `app/services/platforms/`,
  new route handler under `app/api/v1/`, model fields in
  `app/models/agent.py`, tests under `tests/connectors/`, fixtures
  in `tests/fixtures/<platform>/`.
- **Agent-Template** — new SECTION X in `terraform/main.tf` (secret
  container + cross-project IAM binding for the Forum to read the
  bot token), and a `validate_<platform>()` probe in
  `register_agent.py` wired into `build_platforms()`.

### Infrastructure changes still go through Terraform

Same rule as Section 1 — any new GCP resource the code expects
(secret, IAM binding, API enablement, scheduler job, bucket) needs
an entry in the appropriate `.tf` file in the same PR. Code that
*implicitly* depends on a new resource ("works on my machine because
I `gcloud`'d it") will break for the next operator who deploys from
clean.

When the resource is for an *agent template* feature (per-agent SA
pattern, etc.), the terraform change likely needs to land in **both
repos**: the operator-side override in this repo's `terraform/`, and
the matching agent-side override in Agent-Template's
`terraform/main.tf`. The Discord cross-project SA work in commit
history is the worked example.

### Commit and PR conventions

- Imperative subject under ~70 characters; the body explains *why*,
  not *what* (the diff already shows what changed).
- One logical change per commit. Mechanical `terraform fmt` and
  similar formatting-only changes go in their own small commit.
- Never use `--amend` after a commit has been pushed — create a new
  commit instead.
- PRs include a `## Test plan` checklist. Mark which boxes were
  verified locally and which still need reviewer attention.

### Things this repo's history has burned us on

These are the patterns that *look* fine but bit us in production —
worth knowing before suggesting a similar approach:

- **Stale agent template copies.**
  `docs/terraform-templates/agent-project/` and
  `docs/scripts/register_agent_template.py` once duplicated content
  that also lived in Agent-Template. They went stale within weeks
  because no one diff-checks two copies. Anything that smells like
  a template-shaped duplicate of an Agent-Template file should
  *only* live in Agent-Template.
- **GCP API auto-enablement is too slow for cold starts.**
  `iamcredentials.googleapis.com` and a couple of others
  "auto-enable on first call," but the auto-enable is async and
  Reasoning Engines deployed in the meantime return 500 on the
  first user message. Explicitly enable every API the runtime
  touches in `terraform/apis.tf`.
- **`client = "gcloud"` on the Cloud Run service.**
  Any time someone updates the Cloud Run service via gcloud
  (intentional or not), GCP stamps `client = "gcloud"` and
  `client_version = "..."` on the resource. These show up in
  `terraform plan` as harmless `-> null` proposed changes. Ignore.
- **Local `terraform/terraform.tfstate` files.**
  The state lives in GCS. If you see a local `terraform.tfstate`
  in a fresh clone, it's a snapshot from before the GCS backend
  migration; move it aside before running `terraform init` so it
  doesn't get treated as authoritative.
- **Hardcoded thresholds tied to dispatcher cadence.**
  The scheduler dispatcher used to run every minute; the
  "notify user after 1440 consecutive failures (~24 hours)" was
  written assuming that cadence. When the cadence changed, the
  threshold had to change too (now 288 at 5-minute cadence).
  When suggesting cadence changes, audit for threshold constants
  that depended on the old rate.

### CLA

Contributions require signing the Comites-ai CLA via CLA Assistant.
See `CONTRIBUTING.md` for the full flow.

### Related AI-assistant docs

- `CLAUDE.md` (this repo) — short subset of these instructions,
  loaded by Claude Code automatically. Treat `AGENTS.md` (this
  file) as the canonical version; `CLAUDE.md` may lag.
- Agent-Template's own `AGENTS.md` — covers the *agent* side of the
  contract, including ADK-specific guidance. Read it if you're
  helping someone work in both repos at once.
