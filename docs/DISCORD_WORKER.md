# Discord Worker

The Discord integration is unlike Slack, Telegram, or Google Chat. Those
platforms POST events to an HTTP webhook on the Forum and we reply
synchronously. Discord does not: direct messages — and arbitrary channel
messages — are only delivered over the **Gateway**, a persistent WebSocket
connection.

We therefore run a separate small service alongside the Forum: the
`discord-worker`. It holds the Gateway connection open, listens for DMs,
normalizes each one to the same `PlatformEvent`-shaped payload the rest of
the system uses, and POSTs it to the Forum's
`/api/v1/discord/events/{agent_id}` endpoint over HTTPS.

```
Discord ──WSS──> [discord-worker VM] ──HTTPS (OIDC)──> [the-forum Cloud Run]
                                                              │
                                                              ▼
                                                     MessageProcessorV2 → Vertex AI
                                                              │
[the-forum] ──REST──> Discord API  ◄─── DiscordConnector.send_message
```

The worker only forwards. All Vertex AI calls, session management,
identity linking, and outbound message sending continue to live in the
Forum, exactly as they do for Slack and Telegram.

## Cost

The worker runs on a single `e2-micro` Compute Engine VM.

**GCP Always Free tier**: one `e2-micro` per billing account, in
`us-central1`, `us-west1`, or `us-east1`, is **free** — plus 30 GB
standard PD and 1 GB internet egress per month. The Discord Gateway
keepalive plus the small JSON forwards to the Forum (same region, free
egress) consume a tiny fraction of those quotas.

**If your free-tier e2-micro is already in use elsewhere**, or you deploy
this VM in a region outside the free list, you will be billed at the
standard `e2-micro` rate, currently about **$6–7/month**. Add a few cents
for Secret Manager (one secret).

Check your billing console before applying terraform if you're unsure
whether the free-tier slot is available — there's no "tell me if it's
free" preflight, the discount just shows up (or doesn't) on the invoice.

## OS patching and your responsibilities

The VM runs **Container-Optimized OS (COS)** with automatic updates
enabled. Google ships security patches to the host OS and the COS kernel
on a regular cadence; the VM picks them up at next reboot. You do not need
to do anything for OS updates.

**The container image is a different story.** It's pinned to whatever tag
you supplied to terraform (typically `:latest` of an image in your
project's Artifact Registry), and **it is your responsibility to rebuild
and redeploy it** when:

- `discord.py` ships a security fix
- The `python:3.12-slim` base image ships a security fix
- Any other dependency in `discord-worker/requirements.txt` ships a CVE fix

A reasonable cadence:

- **Quarterly** by default: rebuild and redeploy whether or not there's
  been a CVE.
- **Within 7 days** of a high-severity CVE against `discord.py`,
  `requests`, `google-auth`, or the Python base image.
- **Immediately** if a CVE is being actively exploited in the wild.

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

# Look for: "Connected to Discord as <bot-name> (id=...)"
```

## Operational runbook

### View logs

The worker writes structured logs to Cloud Logging under the COS Ops
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

### Restart the worker container

`gcloud compute instances reset` (above) reboots the VM, which restarts
the container. If you want to restart the container *without* rebooting
the VM, SSH in and use `docker restart`:

```bash
gcloud compute ssh discord-worker --zone="${DISCORD_WORKER_ZONE}"
# Inside the VM:
docker ps                          # find the container ID
docker restart <container-id>
exit
```

### Rotate the bot token

1. Generate a new token in the Discord Developer Portal (Bot → Reset
   Token). The previous token becomes invalid immediately, so be ready.
2. Add a new secret version:
   ```bash
   echo -n "NEW_TOKEN" | gcloud secrets versions add discord-bot-token \
     --data-file=- --project="${PROJECT_ID}"
   ```
3. Reset the VM so the worker re-reads the secret at startup:
   ```bash
   gcloud compute instances reset discord-worker \
     --zone="${DISCORD_WORKER_ZONE}" --project="${PROJECT_ID}"
   ```

## Failure modes

- **WebSocket drops.** `discord.py` reconnects with exponential backoff.
  If you see a tight reconnect loop in the logs, check Discord's status
  page and the bot's privileged intents (see below).
- **Discord rate limits.** The worker forwards one event per inbound DM
  with no batching; even a busy bot stays well under the per-route limit.
  Outbound `send_message` calls happen on the Forum side, not the worker.
- **`PrivilegedIntentsRequired` on startup.** Enable Message Content
  Intent and Direct Messages Intent in the Developer Portal under
  Bot → Privileged Gateway Intents. Bots in 100+ servers require Discord
  to verify these intents before they can be enabled.
- **VM doesn't come back after host maintenance.** `automatic_restart`
  and `MIGRATE` are set, so this is rare. If it happens, the next
  `gcloud compute instances start discord-worker` brings it back; logs
  will show the reconnect.
- **OIDC token rejected by the Forum.** The most likely cause is a
  mismatch between the VM's service account and the
  `discord_worker_service_account` field on the agent's Firestore
  document. Compare them character-for-character.

## Why one VM per agent

Discord allows one bot per token, and a bot can only be online once. So
running two Discord agents requires two Gateway connections, which
requires two worker processes — and in our setup, two VMs. The Forum side
already routes per-agent via the `/api/v1/discord/events/{agent_id}`
suffix, so adding a second agent is purely a terraform exercise: bump up
to a second `google_compute_instance.discord_worker_2` (or refactor the
existing block into a `for_each` over an agent map).

We've left this as a manual extension because most installs run a single
agent and the simpler shape is easier to reason about.
