# discord-worker

Long-running Discord Gateway client that forwards direct messages to the Forum.

## Why this exists

Discord does not deliver direct messages or arbitrary channel messages to
HTTP webhooks — they only arrive over the Gateway WebSocket protocol. The
Forum (Cloud Run) is request-driven and scales to zero, which is the wrong
fit for a long-lived WebSocket connection. This worker holds the Gateway
connection open and forwards each DM to the Forum's HTTP API.

See [docs/DISCORD_WORKER.md](../docs/DISCORD_WORKER.md) for the full
architecture, cost guidance, deploy runbook, and patching policy.

## Local development

```bash
cd discord-worker
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# For local dev, point at a staging Forum URL and use a sandbox agent.
export FORUM_URL=https://staging-the-forum.example.run.app
export AGENT_ID=my-test-agent
export DISCORD_BOT_TOKEN_SECRET=my-test-agent-discord-token
export DISCORD_BOT_TOKEN_PROJECT_ID=my-test-agent-prod

# OIDC fetch requires GCE metadata. For local dev, run via the gcloud
# default credentials and override OIDC_AUDIENCE if you're talking to a
# Forum that does not require auth (e.g. a local FastAPI process).
python worker.py
```

## Container build

The container is what actually runs in production on the e2-micro VM
(Container-Optimized OS pulls and runs this image at boot):

```bash
# From repo root, NOT from this directory
gcloud builds submit discord-worker \
  --tag=us-central1-docker.pkg.dev/$PROJECT_ID/discord-worker/worker:latest \
  --project=$PROJECT_ID
```

After a new image is pushed, the VM picks it up on next reboot, or you
can force it with `gcloud compute instances reset <vm-name>`.
