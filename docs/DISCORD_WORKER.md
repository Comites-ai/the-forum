# Discord Worker

The Discord integration is unlike Slack, Telegram, or Google Chat. Those
platforms POST events to an HTTP webhook on the Forum and we reply
synchronously. Discord does not: direct messages — and arbitrary channel
messages — are only delivered over the **Gateway**, a persistent
WebSocket connection.

We therefore run a separate service alongside the Forum: the
`discord-worker`. It's a **single multi-tenant process** on a small
Compute Engine VM that holds one Gateway connection per Discord-enabled
agent, all in one asyncio event loop. Each inbound DM is normalized to
the same `PlatformEvent`-shaped payload the rest of the system uses and
POSTed to the Forum's `/api/v1/discord/events/{agent_id}` endpoint over
HTTPS.

```
Discord ──WSS─┐
              ├──> [discord-worker VM]  ──HTTPS (OIDC)──> [the-forum Cloud Run]
Discord ──WSS─┘   N concurrent Gateway                            │
                  connections, one per                            ▼
                  Discord-enabled agent                  MessageProcessorV2 → Vertex AI
                                                                  │
[the-forum] ──REST──> Discord API  ◄── DiscordConnector.send_message
```

The worker only forwards. All Vertex AI calls, session management,
identity linking, and outbound message sending continue to live in the
Forum.

## How the worker finds its bots

On startup (and every `AGENT_REFRESH_INTERVAL_SECONDS`, default 300s)
the worker queries the Forum's Firestore `agents` collection. For each
document with a `platforms` entry that has `platform = "discord"` and
`enabled = true`, the worker reads:

- `discord_bot_token_secret` — the Secret Manager secret name
- `discord_bot_token_project_id` — the GCP project that owns that secret
- `discord_worker_service_account` — the worker SA email each agent is
  authorized to receive events from (the same value across every agent;
  the Forum checks an exact email match before accepting a forward)

It fetches the bot token via Secret Manager (cross-project — the worker
SA needs `secretAccessor` granted in each agent's project) and opens a
Gateway connection. On each refresh cycle the worker reconciles: new
agents come online, removed agents are disconnected, and rotated tokens
trigger a clean restart.

## Where things live

| Resource | Project | Created by |
|---|---|---|
| Worker VM, SA, Artifact Registry repo | **Forum's** project | `terraform/discord_worker.tf` (gated on `var.use_discord`) |
| Forum's Firestore `agents` collection | **Forum's** project | (existing) |
| Each agent's `discord-bot-token` secret container | **Agent's** project | [Agent-Template](https://github.com/Comites-ai/Agent-Template) `terraform/main.tf` SECTION 5 |
| Cross-project `secretAccessor` grant on each token | **Agent's** project | same as above |
| Each agent's bot token value | **Agent's** project | `gcloud secrets versions add` (out-of-band, never in terraform) |

## Cost

The worker runs on a single `e2-micro` Compute Engine VM, regardless of
how many Discord agents it serves.

**GCP Always Free tier**: one `e2-micro` per billing account in
`us-central1`, `us-west1`, or `us-east1` is **free** — plus 30 GB
standard PD and 1 GB internet egress per month. The Gateway keepalive
plus the small JSON forwards to the Forum (same region, free egress)
consume a tiny fraction of those quotas, even with many concurrent
connections.

**If your free-tier e2-micro is already in use elsewhere**, or you
deploy this VM in a region outside the free list, you will be billed at
the standard `e2-micro` rate — about **$6–7/month**. That's the total
cost no matter how many Discord agents you onboard; the worker is
multi-tenant.

## OS patching and your responsibilities

The VM runs **Container-Optimized OS (COS)** with automatic updates
enabled. Google ships security patches to the host OS and the COS kernel
on a regular cadence; the VM picks them up at next reboot. **You do not
need to do anything for OS updates.**

**The container image is a different story.** It's pinned to whatever
tag you supplied to terraform (typically `:latest` of an image in your
project's Artifact Registry), and **it is your responsibility to rebuild
and redeploy it** when:

- `discord.py` ships a security fix
- The `python:3.12-slim` base image ships a security fix
- Any other dependency in `discord-worker/requirements.txt` ships a CVE fix

A reasonable cadence:

- **Quarterly** by default: rebuild and redeploy whether or not there's
  been a CVE.
- **Within 7 days** of a high-severity CVE against `discord.py`,
  `requests`, `google-auth`, `google-cloud-firestore`, or the Python
  base image.
- **Immediately** if a CVE is being actively exploited in the wild.

## Onboarding a Discord agent

Forum-side: **nothing**. The multi-tenant worker auto-discovers new
Discord-enabled agents from Firestore on its next refresh tick.

Agent-side: register the bot in Discord, populate the bot-token secret
in the agent's project, set `discord_application_id` in the agent's
`terraform.tfvars`, and run `register_agent.py`.

### 1. Create the Discord application + bot

The Discord Developer Portal UI is a bit confusing if you're new to
Discord. The concrete click-path:

1. Go to <https://discord.com/developers/applications> → **New
   Application**. Register it and fill out the **General Information**
   tab. Copy the **Application ID** from this tab — you'll need it in
   step 3.
2. **Bot** tab → enable **Message Content Intent** and **Direct
   Messages Intent**. Without these the worker connects but receives
   nothing (you'll see `PrivilegedIntentsRequired` for that agent in
   the worker logs).
3. **Bot** tab → **Reset Token** → copy the token (shown once).
4. **Installation** → **Default Install Settings** → make sure
   **Guild Install** scopes include `bot`, and add bot permissions
   `Send Messages`, `Read Message History`, and `Manage Messages`.
5. Copy the **install link** from that same page and open it in a
   browser to add the bot to your Discord server. Once the "bot
   joined" system message appears in the server, click on the bot's
   name in that message to open a DM with it — that's the simplest
   way to test the end-to-end flow.

More complex server-side wiring — channel-specific permissions, slash
commands, role mentions — is not required for the basic DM flow.
Document that as a follow-up if your agent needs it.

### 2. Provision the bot-token secret

The bot-token secret container is provisioned by the agent-project
terraform template's SECTION 5. After `terraform apply` (which also
creates the two cross-project IAM bindings — worker VM SA for inbound
Gateway, Forum's Cloud Run compute SA for outbound REST replies),
populate the secret value:

```bash
echo -n "YOUR_BOT_TOKEN" | gcloud secrets versions add \
  "${BOT_ACCOUNT_ID}-discord-token" \
  --data-file=- --project="${AGENT_PROJECT_ID}"
```

### 3. Set the Application ID in tfvars and register

Set the Application ID copied in step 1.1 in `terraform.tfvars`:

```hcl
discord_application_id = "1234567890123456789"
```

Then run `register_agent.py` from the agent repo. It auto-detects
every platform whose secret is populated in the agent's project,
validates each token via the platform's native API (Discord's
`users/@me` for the Discord block), and writes the resulting
`platforms` array to the agent's Firestore doc:

```bash
python register_agent.py \
  --agent-name "My Agent" \
  --vertex-ai-agent-id projects/.../reasoningEngines/...
```

The Discord entry it writes looks like:

```json
{
  "platform": "discord",
  "enabled": true,
  "discord_bot_token_secret": "<bot_account_id>-discord-token",
  "discord_bot_token_project_id": "<agent-project-id>",
  "discord_application_id": "1234567890123456789",
  "discord_worker_service_account": "discord-worker@<forum-project>.iam.gserviceaccount.com"
}
```

If the Discord secret is populated but `discord_application_id` is
empty, `register_agent.py` refuses to write the Discord block (better
to fail loud than write an unidentifiable agent into Firestore).

### 4. Wait for the worker to reconcile

The worker re-reads Firestore every `AGENT_REFRESH_INTERVAL_SECONDS`
(default 300s). To force an immediate reconcile from the Forum project:

```bash
gcloud compute instances reset discord-worker \
  --zone=us-central1-a --project=<forum-project>
```

Then DM the bot to verify the end-to-end path. The worker logs the
successful connection as `Agent <id> online as <BotName>#<discrim>`
and each forwarded message as `Forwarded DM agent=<id> user_id=...
msg_id=...`.

## Redeploy runbook

From the repository root:

```bash
# 1. Rebuild the worker image
gcloud builds submit discord-worker \
  --tag="us-central1-docker.pkg.dev/${PROJECT_ID}/discord-worker/worker:latest" \
  --project="${PROJECT_ID}"

# 2. Force the VM to pull and run the new image
gcloud compute instances reset discord-worker \
  --zone="${DISCORD_WORKER_ZONE}" \
  --project="${PROJECT_ID}"

# 3. Confirm the worker reconnected
gcloud compute instances get-serial-port-output discord-worker \
  --zone="${DISCORD_WORKER_ZONE}" \
  --project="${PROJECT_ID}" | tail -50

# Look for: "discord-worker starting: forum=... firestore=..."
# Then per agent: "Agent <id> online as <BotName> (id=...)"
```

## Operational runbook

### View logs

The worker writes structured logs to Cloud Logging via the COS Ops
Agent. Filter by VM:

```bash
gcloud logging read \
  'resource.type="gce_instance" AND resource.labels.instance_id="$(
     gcloud compute instances describe discord-worker \
       --zone=$DISCORD_WORKER_ZONE --format="value(id)" \
       --project=$PROJECT_ID
   )"' \
  --limit 100 \
  --project="${PROJECT_ID}" \
  --format=json
```

### Restart without rebooting

```bash
gcloud compute ssh discord-worker --zone="${DISCORD_WORKER_ZONE}"
# Inside the VM:
docker ps                          # find the container ID
docker restart <container-id>
exit
```

### Rotate a bot token

Tokens are per-agent, so rotation happens in the agent's project, not
the Forum's:

1. Generate a new token in the Discord Developer Portal (Bot → Reset
   Token).
2. Add a new secret version in the **agent's** project:
   ```bash
   echo -n "NEW_TOKEN" | gcloud secrets versions add \
     ${BOT_ACCOUNT_ID}-discord-token \
     --data-file=- --project="${AGENT_PROJECT_ID}"
   ```
3. On its next reconcile (within `AGENT_REFRESH_INTERVAL_SECONDS`), the
   worker notices the token differs from the one it cached, closes the
   old Gateway connection, and opens a new one with the rotated token.
   No worker restart needed; no Forum-side change.

## Failure modes

- **WebSocket drops.** `discord.py` reconnects with exponential backoff
  per agent. If you see a tight reconnect loop in the logs, check
  Discord's status page and that the bot's privileged intents are
  enabled.
- **Discord rate limits.** The worker forwards one event per inbound DM
  with no batching; even a busy bot stays well under the per-route
  limit. Outbound `send_message` calls happen on the Forum side, not
  the worker.
- **`PrivilegedIntentsRequired` on startup of one agent's connection.**
  Enable Message Content Intent and Direct Messages Intent in the
  Developer Portal under Bot → Privileged Gateway Intents for that
  specific application. The OTHER agents stay connected — only the
  misconfigured one fails.
- **Worker can't read an agent's secret.** Cross-project
  `secretAccessor` missing. Re-apply the agent-project terraform; check
  that the binding's `member` is the discord-worker SA email exactly.
- **VM doesn't come back after host maintenance.** `automatic_restart`
  and `MIGRATE` are set, so this is rare. If it happens,
  `gcloud compute instances start discord-worker` brings it back; logs
  will show each agent reconnecting in turn.
- **OIDC token rejected by the Forum.** The most likely cause is a
  mismatch between the worker's SA email and the
  `discord_worker_service_account` field on the agent's Firestore
  document. Compare them character-for-character — they must match
  exactly.

## Why a single process for all bots

Discord allows one bot per token, and a bot can only be online once at
a time. So N agents means N Gateway connections — that's fixed. What's
NOT fixed is how those N connections are processed.

`discord.py` happily runs multiple `Client` instances in the same
asyncio event loop, each with its own bot token and its own connection.
A single Python process on an e2-micro can easily hold dozens of
concurrent Gateway connections; the limiting factor is bandwidth and
RAM, not connection count.

The alternative — one VM per agent — was rejected because:

- It exceeds the GCP Always Free tier the moment you have a second
  Discord agent.
- It doesn't match the centralized-shared-infra pattern that Slack and
  Telegram already use on the Forum side.
- Adding an agent would require terraform changes to the Forum's
  infrastructure, when the goal is for each agent to be added entirely
  in its own project + a Firestore doc.

The tradeoff: if the worker process crashes, all Discord agents are
down until it restarts. For a small ops shop that's an acceptable
blast radius; `discord.py`'s reconnect logic plus COS auto-restart
keeps the gap small.
