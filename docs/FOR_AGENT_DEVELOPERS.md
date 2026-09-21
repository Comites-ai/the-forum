# The Forum — Contract for Agent Developers

For higher-level project context (what Comites.ai is, how the Forum and
agents fit together, the marketplace), see
[comites.ai/developers](https://comites.ai/developers) and
[comites.ai/architecture](https://comites.ai/architecture).

Your agent code and its GCP project live in their own repo. **Start at
[github.com/Comites-ai/Agent-Template](https://github.com/Comites-ai/Agent-Template)** —
clone it, follow its README, run `get_started_linux.sh`. That repo owns the
agent-side setup end-to-end: per-platform secrets, terraform, deploy
scripts, and the `register_agent.py` that writes your agent's record into
the Forum's Firestore.

**This document covers only the Forum-side contracts your agent must
respect** — what the Forum sends to your agent on each turn, what services
the Forum hosts (so you don't have to), how the Forum handles agent
failures so you don't accidentally re-implement them, and the operator
scripts that run in the Forum project (not the agent's project).

If you're looking for "how do I create a new Slack/Google Chat/Telegram/
Discord bot," that flow now lives entirely in Agent-Template's README and
`get_started_linux.sh`. The Forum-side hooks are bullet-pointed below.

## Table of Contents

1. [What The Forum Sends Your Agent](#what-the-forum-sends-your-agent)
2. [Forum-Hosted Services](#forum-hosted-services)
3. [How The Forum Handles Agent Errors](#how-the-forum-handles-agent-errors)
4. [Adding Your Own MCP Servers (Agent-Side)](#adding-your-own-mcp-servers-agent-side)
5. [Operator-Side Identity Linking](#operator-side-identity-linking)
6. [Forum-Side Troubleshooting](#forum-side-troubleshooting)

---

## What The Forum Sends Your Agent

Every message your agent receives — interactive turn or scheduled job —
flows through this contract. The Forum normalises platform differences
(Slack, Google Chat, Telegram, Discord) before invoking your Reasoning
Engine.

### Message structure

Your agent is invoked with exactly three keys. **There is no `images`
parameter** — everything the agent needs, including any image reference,
is carried inside the `message` string:

```python
{
    "message": "[IMAGE: gs://your-bucket/slack-files/20260328/a1b2c3d4e5f6.png | image/png]\n\n"
               "[From: Jonathan Cavell] [This message was sent at ...] What wine pairs with this?",
    "user_id": "Jonathan Cavell",  # User's actual name from Firestore
    "session_id": "Jonathan Cavell:5695302693795397632",
}
```

`session_id` is omitted for old-style session IDs that carry no
`user:session` split. Nothing else is sent.

This is deliberate: passing a `gs://` URI as text keeps large images out
of the request body, which is what keeps us inside Agent Engine's request
size limits. Your agent fetches the object itself — see
[Reading an inbound image](#reading-an-inbound-image).

#### Anatomy of the `message` string

Segments are prepended in this order, each separated by a blank line, and
each is present only when it applies:

| Order | Segment | When |
|---|---|---|
| 1 | `Note to Agent:  The user attempted to send you a file...` | An attachment your agent can't read was dropped |
| 2 | `[IMAGE: gs://… \| image/png]` | Exactly one image passed intake |
| 2 | `[FILE: gs://… \| application/pdf]` | Exactly one non-image file passed intake |
| 3 | `[From: <name>] [<time context>] <user's text>` | Always |

`[IMAGE:` is reserved for image MIME types. Anything else arrives as
`[FILE:`, so an agent that only matches `[IMAGE:` can never be handed a
PDF by mistake.

For scheduled jobs the prefix instead reads
`[From: <name> | <platform>_id: <recipient>] <job prompt>`, and no image
is ever attached.

Parse defensively: match on the literal `[IMAGE: ` prefix and split on
` | ` for the MIME type. Treat these as additive — new bracketed tokens
may be introduced, and an agent that ignores tokens it doesn't recognise
keeps working.

### User identity format

The Forum sends the user's **actual name** (not platform IDs):

- **`user_id`** — the user's primary name from the unified Firestore
  `users` collection (e.g., "Jonathan Cavell"). Same value across Slack,
  Google Chat, Telegram, and Discord once identities are linked.
- **`session_id`** — combines the user name and the Vertex AI session ID
  (e.g., `Jonathan Cavell:5695302693795397632`). Reuse it on subsequent
  turns to maintain conversation continuity.
- **`message` prefix** — includes the user's name for context (e.g.,
  `[From: Jonathan Cavell]`). For scheduled jobs the prefix is richer:
  `[From: Jonathan Cavell | slack_id: U0ABC123] ...`.

Same person on different platforms → same `user_id` → unified history.
See [Operator-Side Identity Linking](#operator-side-identity-linking) for
how to merge identities when auto-linking doesn't suffice.

### Image handling contract

The Forum enforces a single-image policy and rejects files it cannot send
to your agent **before** invoking you:

- **Non-image attachments** (PDFs, videos, audio, etc.) → user sees
  *"Sorry, it appears you sent me a file type that I can't read..."*
  and the agent receives the user's text with a `Note to Agent:` prefix
  explaining a non-image file was dropped.
- **More than one image** (or a Telegram album) → user sees *"Sorry, I
  can only handle one image at a time..."* and the agent is **not**
  called.
- **A single image that fails to download or upload** → user gets a
  specific error (download / size / unsupported MIME / GCS save) and the
  agent is **not** called.

Your agent only ever sees: text alone, or text plus exactly one readable
file referenced by a single `[IMAGE: …]` or `[FILE: …]` token.

### Declaring which file types you accept

Which attachments count as readable is **per-agent**. Your Firestore
record carries an `accepted_file_types` list, written at deploy time by
`register_agent.py`:

```python
"accepted_file_types": ["image/png", "image/jpeg", "application/pdf"]
```

| Declaration | What reaches your agent |
|---|---|
| absent / empty | The default image allowlist — PNG, JPEG, GIF, WebP, HEIC, HEIF |
| `["application/pdf"]` | **PDFs only.** Images are now rejected |
| `["image/png", "image/jpeg", "application/pdf"]` | Those three types |

**Declaring anything replaces the default — it does not extend it.** If
you want images *and* PDFs, list the image types explicitly. An agent
that declares only `application/pdf` stops receiving photographs, which
is rarely what you want for a receipt- or document-style agent, since
users photograph documents as often as they attach them.

The Forum enforces this before your agent is invoked, so you never
receive a type you didn't declare, and the user gets an accurate
rejection naming what you *can* read ("I can accept typed words, images,
and PDF"). Agents that declare nothing keep the exact wording and
behavior they had before this field existed.

Non-image files are capped separately from images
(`MAX_DOCUMENT_SIZE_MB`, default 20 MB), since a scanned multi-page
invoice is legitimately larger than a photo.

### Reading an inbound file

The token gives you a `gs://` URI and a MIME type. Download the object
and hand it to your model as an inline part — there is no base64 payload
in the request to fall back on:

```python
from google.cloud import storage
from google.genai import types

def read_file(gcs_uri: str, mime_type: str) -> types.Part:
    bucket_name, _, blob_name = gcs_uri.removeprefix("gs://").partition("/")
    data = storage.Client().bucket(bucket_name).blob(blob_name).download_as_bytes()
    return types.Part.from_bytes(data=data, mime_type=mime_type)
```

**Branch on the MIME type before you prompt.** Gemini will happily accept
a PDF into a part built for a photo, then answer a photo question about a
document and sound confident doing it. If your agent accepts more than
images, key the prompt off `mime_type` rather than assuming what arrived:

```python
if mime_type.startswith("image/"):
    prompt = "Analyze this photo…"      # your existing vision prompt
elif mime_type == "application/pdf":
    prompt = "Extract the line items from this document…"
else:
    return f"Error: I can't read {mime_type}."
```

Two things to get right:

- **Bucket access.** Your agent's runtime service account needs
  `roles/storage.objectViewer` on The Forum's inbound-files bucket, or
  the download 403s. Agents running as the default compute SA already
  have it; per-agent service accounts must be granted it explicitly.
- **Lifecycle.** Objects are deleted after `gcs_bucket_lifecycle_days`
  (1 day in production). If your agent needs to keep a file, copy it out
  **during the turn** — a `gs://` URI stored for later will 404.

For ADK-side image handling (multimodal model selection, `LlmAgent`
subclassing), see Agent-Template's README — its `agent.py` already wires
multimodal handling correctly.

---

## Forum-Hosted Services

The Forum hosts services your agent calls into. They live here (not in
your agent) because the data, identity, or scheduling logic is owned by
the Forum.

### Scheduler MCP Server

The Forum hosts three MCP servers. The first is the scheduler, at:

```
POST {forum_url}/api/v1/mcp/scheduler/       (Streamable HTTP, MCP spec 2025-03-26)
```

The trailing slash matters — the path is a Starlette `Mount`, so the
bare form 307-redirects to `/scheduler/` and most MCP HTTP clients follow
with GET instead of re-POSTing. Always include the slash.

It wraps the existing `/api/v1/scheduled-jobs` REST API as MCP tools so
your agent can manage user reminders directly through the LLM tool loop
instead of you maintaining wrapper functions. The other two servers, the
[agents](#agents-mcp-server-agent-to-agent-communication) and
[history](#history-mcp-server-your-own-conversations-verbatim) servers,
share this one's transport and API key.

#### Why this one is hosted by the Forum

- The scheduling logic already lives in the Forum
  (`app/services/scheduled_job_service.py`) and the data lives in the
  Forum's Firestore. Co-hosting saves a network hop and avoids
  duplicating the service code in every agent.
- `agent_id` is auto-resolved from the API key — your LLM never has to
  learn its own ID, removing a category of tool-call mistakes.
- Authorization (jobs filtered by the calling agent) is enforced
  server-side.

#### Tools exposed

| Tool | Inputs | Returns |
|---|---|---|
| `create_scheduled_reminder` | `name`, `prompt`, `schedule` (cron), `user_name`, optional `timezone`, `output_platform` | the new job |
| `list_scheduled_reminders` | `user_name` | array of jobs |
| `update_scheduled_reminder` | `job_id`, optional `name`/`prompt`/`schedule`/`timezone`/`enabled` | updated job |
| `delete_scheduled_reminder` | `job_id` | `{success, job_id}` |
| `pause_scheduled_reminder` | `job_id` | job with `enabled=false` |
| `resume_scheduled_reminder` | `job_id` | job with `enabled=true` |

`user_name` is the human name from the `[From: ...]` message prefix —
the server resolves it to the unified user record. `output_platform` is
one of `slack`, `google_chat`, `telegram`, `discord`; if omitted, it
defaults to whichever platform the user most recently chatted with this
agent on (falling back to `slack` if there's no session yet).

Creates are idempotent per `(agent, user, name)`: re-creating a reminder
with the same name updates it in place instead of duplicating it. This
also means an agent can retarget a job's schedule from another job —
e.g. a nightly job that re-creates a "morning check" reminder with a new
time each night.

`create_scheduled_reminder` **fails closed**. If the Forum cannot read
your existing reminders in full — a Firestore error, or a stored job
document that won't parse — the create returns an error instead of
inserting. Treat that error as retryable and do *not* fall back to
creating under a different name: the point is that an unreadable list
must never be mistaken for an empty one, which is how one agent
accumulated 37 reminders where 7 were intended. A create that would
duplicate an existing job is likewise rejected outright.

#### Silent replies: `[SILENT]`

When a scheduled job fires, the agent's reply is normally delivered to
the user. If the agent's reply **starts with `[SILENT]`**, the run is
recorded as a success but nothing is delivered. Use this for
condition-check jobs ("if there's anything new, tell me — otherwise stay
quiet"): instruct your agent, in the job's `prompt`, to reply `[SILENT]`
when there is nothing worth saying. An *empty* reply is still treated as
a failure (it usually means a broken tool), so `[SILENT]` is the only
supported way to decline delivery.

#### Provisioning your agent's API key

Agent-Template's terraform provisions the secret container + IAM binding
automatically (see Section 5 of its `terraform/main.tf`). The remaining
two steps — generate the key, store the hash, then populate the secret
value — happen here in the Forum repo:

**Step 1 — Generate the key + store its hash** (from the Forum repo):

```bash
cd /path/to/the-forum
python scripts/provision_scheduler_api_key.py --agent-id YOUR_AGENT_FIRESTORE_ID
```

The script writes the SHA-256 hash to your agent's Firestore doc and
prints the plaintext **once**. Copy it.

**Step 2 — Populate the secret value in the agent's project**:

```bash
echo -n 'PLAINTEXT_FROM_STEP_1' | gcloud secrets versions add \
  ${BOT_ACCOUNT_ID}-scheduler-mcp-key \
  --data-file=- --project=$AGENT_PROJECT
```

To rotate: re-run step 1 (overwrites the hash; old plaintext stops
working immediately), then step 2 with the new plaintext.

#### Wiring it into your ADK agent

```python
import os
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StreamableHTTPConnectionParams

scheduler_toolset = MCPToolset(
    connection_params=StreamableHTTPConnectionParams(
        # Trailing slash is required: the route is mounted as a sub-app at
        # `/scheduler`, so Starlette 307-redirects bare requests to the
        # canonical `/scheduler/` form. The MCP HTTP client follows the
        # redirect with GET instead of re-POSTing, which silently breaks
        # the JSON-RPC handshake.
        url=f"{os.environ['FORUM_URL']}/api/v1/mcp/scheduler/",
        headers={"X-API-Key": os.environ["SCHEDULER_MCP_KEY"]},
    ),
)

root_agent = LlmAgent(
    model="gemini-2.0-flash",
    tools=[scheduler_toolset, ...],
)
```

Your agent populates `FORUM_URL` from its config and `SCHEDULER_MCP_KEY`
by reading from Secret Manager at startup.

#### Cron expression reference

| Schedule | Cron expression |
|---|---|
| Every day at 9 AM | `0 9 * * *` |
| Weekdays at 9 AM | `0 9 * * 1-5` |
| Every Monday at 10 AM | `0 10 * * 1` |
| Every hour | `0 * * * *` |
| Every 30 minutes | `*/30 * * * *` |
| First day of month at noon | `0 12 1 * *` |

Format: `minute hour day-of-month month day-of-week`. The `timezone`
field interprets the cron in the IANA zone you pass (e.g.,
`America/New_York`); defaults to UTC.

#### REST API fallback

The REST API at `/api/v1/scheduled-jobs` still works and is not
deprecated — same data, same behavior — if you have reason to call it
directly (ops scripts, admin tools, etc.). For agents, prefer the MCP
path above.

### Agents MCP Server (agent-to-agent communication)

The Forum hosts a second MCP server that lets any attached agent talk to
any other attached agent, with the Forum mediating the call:

```
POST {forum_url}/api/v1/mcp/agents/       (Streamable HTTP, MCP spec 2025-03-26)
```

Same transport, same trailing-slash rule, and the **same API key** as the
scheduler MCP (one key per agent authenticates both servers).

#### Inquiries: publish what other agents can ping you about

Each agent publishes **inquiries** — the requests it knows how to field
from other agents — on its Firestore agent document. They're visible in
the admin UI (agent detail page) and discoverable through this MCP
server. Ship an `inquiries.json` in your agent repo:

```json
{
  "description": "Marathon coach: training plans, workouts, Garmin data.",
  "inquiries": [
    {
      "name": "planned_workouts_today",
      "description": "Explains today's planned workout, its training purpose, energy demand, and estimated calorie burn.",
      "request_format": "AGENT_QUERY: planned_workouts_today",
      "response_format": "PLANNED_WORKOUTS <date>: <workout> | purpose=<...> | energy=<low|med|high> | est_calories=<n>"
    }
  ]
}
```

and have your `register_agent.py` write `description` + `inquiries` to
your agent's Firestore doc at deploy time (the Agent-Template's
register_agent.py does this when `inquiries.json` is present). Keep the
prompt sections that ANSWER each inquiry in sync with what you publish —
the registry is a promise to other agents.

#### Tools exposed

| Tool | Inputs | Returns |
|---|---|---|
| `list_agents` | — | every other agent's `display_name`, `description`, inquiry names |
| `get_agent_inquiries` | `agent_name` | the agent's full inquiry records |
| `query_agent` | `agent_name`, `message`, `on_behalf_of` | `{agent, on_behalf_of, query_id, reply}` |
| `get_query_status` | `query_id`, or `agent_name` + `on_behalf_of` (+ `limit`) | what became of an earlier `query_agent` call: delivered? replied? (see below) |

#### The on-behalf-of contract

Agents serve multiple human users, so **every `query_agent` call must
say which user it concerns** (`on_behalf_of`, validated against the
Forum's user registry). The target agent receives:

```
[From Agent: <caller display name> | On Behalf Of: <user primary name>] <message>
```

The caller's identity comes from the API key — an agent cannot
impersonate another agent or omit attribution. Conversation history is
kept per (caller, target, user), so different users' exchanges never
share context.

Prompt guidance for BOTH sides of an A2A exchange:

- **When queried** (`[From Agent: ...]` prefix): answer in your published
  `response_format`, scoped strictly to the On-Behalf-Of user's data. If
  you don't serve that user, say so plainly instead of answering from
  another user's data. Skip the conversational persona — the caller is
  an LLM parsing your reply.
- **When querying**: pass the user's name from your own `[From: <name>]`
  prefix through as `on_behalf_of` verbatim. Prefer the target's
  published `request_format`.

#### How long you have to answer

The Forum waits **240 seconds** for a queried agent's reply, which is as
long as it can wait: the whole call rides inside one Cloud Run request,
and Cloud Run cuts the request off at 300s regardless. A turn that needs
longer than that has to be restructured — acknowledge the request and
report back later, rather than holding the line.

When the Forum gives up it tells the caller so, but note what that error
does *not* mean. The Forum has stopped listening; the target engine is
almost certainly still running and finishing what it was asked to do.
**A timeout says the reply was lost, not that the work was undone.** If
you are the caller and the request had side effects, ask the target for
the current state before sending it again — a blind retry double-writes.
The timeout error carries the call's `query_id`, and the Forum keeps
listening after it has answered you: if the target's reply arrives late,
`get_query_status` will hand it to you.

#### Delivery receipts: telling "not sent" from "sent, reply lost"

Every `query_agent` call leaves a record on the Forum from the moment the
message is handed to the target's engine. Its id, the `query_id`, comes
back in the result and in the text of every error. `get_query_status`
reads the record:

- **By id** — `get_query_status(query_id=...)` returns that one call.
- **By target and user** — `get_query_status(agent_name=..., on_behalf_of=...)`
  lists your most recent queries to that agent for that user, newest
  first. This is the form for the case the receipt exists for: your MCP
  client dropped the connection mid-call and you never saw a result, so
  you have no id.

Each record reports `status`, `delivered`, the timestamps, the message you
sent (so you can recognise it), the reply if there is one, and a `meaning`
line saying what to do. The statuses:

| status | what it means |
|---|---|
| `sent` | handed to the engine, nothing heard back yet (in flight, or the Forum was cut off before recording an outcome) |
| `delivered` | the engine has the message and has started answering; no outcome recorded yet |
| `replied` | delivered and answered; `reply` is included |
| `timed_out` | the Forum stopped waiting; `delivered` says whether the engine had started. A late reply, if one comes, replaces this |
| `empty_reply` | delivered; the target ran its turn (tools, thinking) but ended it without text |
| `failed` | the engine call broke; `delivered` says whether it had the message first |

**No record at all means the message never reached the engine**, and
resending is safe. Records are kept for seven days.

The rule for callers: treat a lost connection, a timeout, and any error
as *delivery unknown*, not as *not delivered*. Check the receipt, then
either read the reply, ask the target for the current state, or resend,
in that order of preference. Never resend blind. This matters because the
ADK's MCP client can drop a session while a concurrent turn is running,
which surfaces as `MCP session connection lost` on calls the Forum
answered perfectly well.

For the same reason the Forum itself no longer resends on your behalf
when the target returns no text but demonstrably ran (it used to retry
once on any empty reply; now only the zero-chunk dead-session signature
triggers that).

#### Using it from Claude Code (development)

The agents MCP server is also handy while *building* agents: attach
Claude Code (or any MCP client) to it and you can list attached agents,
read their inquiry contracts, and send test queries without touching a
messaging platform:

```bash
claude mcp add --transport http forum-agents \
  https://YOUR_FORUM_URL/api/v1/mcp/agents/ \
  --header "X-API-Key: YOUR_AGENT_MCP_KEY"
```

Any provisioned agent key works; the caller identity will be that agent.

### History MCP Server (your own conversations, verbatim)

The Forum keeps every message it relays for **seven days** and serves it
back to the agent that took part in it, verbatim, through a third MCP
server:

```
POST {forum_url}/api/v1/mcp/history/       (Streamable HTTP, MCP spec 2025-03-26)
```

Same transport, same trailing-slash rule, same API key as the other two.

#### Why it exists

Without it, an agent's only record of a past exchange is whatever it
chose to write into its own memory afterwards — a lossy, self-authored
summary. That is how an agent ends up asserting something about last
Tuesday, confidently and wrongly, until the user produces a screenshot.
The Forum saw the actual text go by in both directions, on every
platform, so it is the right place to keep it.

#### What is logged

Every message between you and a user, live or scheduled, in both
directions: the user's words as sent, and your reply exactly as it was
delivered (including any fallback copy the Forum sent on your behalf
when you returned nothing). A reply the Forum split into several
platform messages is one entry. Attachments appear as placeholders such
as `[image: image/jpeg]`; bytes and URLs are never stored. Scheduled
trigger prompts are logged as inbound messages authored by the job name.
Agent-to-agent (`query_agent`) exchanges are **not** logged here; their
delivery receipts are (see `get_query_status` above).

Retention is a Firestore TTL policy on the log: entries disappear about
seven days after they were written. There is no backfill.

#### Tools exposed

| Tool | Inputs | Returns |
|---|---|---|
| `search_history` | `user`, `query`, `date_from?`, `date_to?`, `author?` (`user`/`agent`), `limit?` (≤25) | ranked hits: `message_id`, localized `timestamp`, `author`, `platform`, ~200-char `snippet` |
| `get_context` | `message_id`, `before?`, `after?` (default 5, ≤20 each) | the hit in full plus the messages around it, verbatim |
| `get_messages` | `user`, `start`, `end`, `cursor?`, `limit?` (≤100) | a time range in order, paged via `next_cursor` |

Ranking is deliberately simple: an exact phrase match (case-insensitive
substring) first, then messages containing every word of the query,
then some words, newer messages breaking ties. Phrase matching is what
settles "didn't you tell me to move the SOW whole?"; there is no
semantic search and nothing is summarised on the way out.

Timestamps are localized to the user's timezone (their Forum
`default_timezone`, else the Forum's default). Dates you pass in are read
the same way: `2026-09-12` means that user's local day.

**Every response is capped at 8,000 characters.** When the cap bites the
response says `"truncated": true` and either gives a `next_cursor` or
tells you how to narrow. Ask for a day, not a week.

#### Scoping

You only ever see your own conversations with the user you name. The
caller is identified by its API key; `user` is validated against the
Forum's user registry exactly as `on_behalf_of` is for `query_agent`
(an unknown name is rejected, a user you have never spoken with returns
nothing); and every `message_id` is bound to the conversation it came
from, so a message id from another agent's history is refused.

#### Wiring it into your ADK agent

```python
history_toolset = MCPToolset(
    connection_params=StreamableHTTPConnectionParams(
        url=f"{os.environ['FORUM_URL']}/api/v1/mcp/history/",   # trailing slash
        headers={"X-API-Key": os.environ["SCHEDULER_MCP_KEY"]},
    ),
)
```

Prompt guidance:

- The history is **read-only** and **pull-only**. The Forum never pushes
  it into your turn; you look something up when a question turns on
  what was actually said.
- Quote what you retrieve as it is, with its timestamp. Never summarise
  it into a claim it does not support.
- When retrieved text contradicts your own memory, the retrieved text
  wins — but say so to the user and correct the record together, rather
  than silently overwriting what you believed.
- Two steps: `search_history` for hits, then `get_context` on the one
  that matters. Use `get_messages` only for a recap of a bounded range.

#### Using it from Claude Code (development)

```bash
claude mcp add --transport http forum-history \
  https://YOUR_FORUM_URL/api/v1/mcp/history/ \
  --header "X-API-Key: YOUR_AGENT_MCP_KEY"
```

### GCS Image Storage (Forum-operator setup)

When `GCS_BUCKET_NAME` is configured in the Forum's `.env`, the Forum
uploads inbound images to Google Cloud Storage and passes the object's
`gs://` URI to your agent in an `[IMAGE: …]` token. This keeps image
bytes out of the request body, which matters because Agent Engine has
relatively small request limits.

**GCS is effectively required for image support.** The agent input
carries no base64 channel, so with `GCS_BUCKET_NAME` unset an inbound
image is accepted from the user and then dropped before the agent is
called (logged as a warning). Configure the bucket if your agents need
to see images at all.

The setup below is for the **Forum operator**, not the agent developer.
Your agent just needs to know that these objects auto-delete after 1 day,
so don't store the URIs long-term in your agent's state.

```bash
# Forum-operator setup (one-time)
export PROJECT_ID="your-gcp-project"
export BUCKET_NAME="${PROJECT_ID}-slack-files"
export REGION="us-central1"

gcloud storage buckets create gs://${BUCKET_NAME} \
    --project=${PROJECT_ID} \
    --location=${REGION} \
    --uniform-bucket-level-access

# 1-day lifecycle so images don't accumulate
cat > /tmp/lifecycle.json << 'EOF'
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 1}}]}
EOF
gcloud storage buckets update gs://${BUCKET_NAME} --lifecycle-file=/tmp/lifecycle.json

# Grant the Forum's Cloud Run SA write access
export PROJECT_NUMBER=$(gcloud projects describe ${PROJECT_ID} --format="value(projectNumber)")
gcloud storage buckets add-iam-policy-binding gs://${BUCKET_NAME} \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role="roles/storage.objectAdmin"
```

Then set in the Forum's `.env`:

```bash
GCS_BUCKET_NAME=your-project-slack-files
GCS_FILE_PREFIX=slack-files
```

If your agents run under a service account that doesn't already have
project-level storage access, grant them `roles/storage.objectViewer`
on the bucket so they can read the `gcs_uri` references.

---

## How The Forum Handles Agent Errors

The Forum does its best to keep users from seeing raw failures. Most
error handling happens in the Forum so agent code can stay simple. A
few cases are worth knowing about as an agent developer.

### Empty agent responses & intelligent error detection

If your agent's response stream finishes without producing any text,
the Forum analyzes the stream to determine the most likely cause and
shows the user an appropriate message. The middleware examines:

1. **Function call vs response counts** — did tools get called but
   never respond?
2. **Error patterns in function_response** — rate limits, permission
   errors, general failures.
3. **Function names** — which tool was the agent working with?

#### "The first tool I called ({tool_name}) didn't respond at all..."

Appears when `function_call > 0` but `function_response = 0`. The tool
was invoked but never executed — usually a permissions issue.

**What to check:**
- Does your Reasoning Engine's service account have the IAM roles the
  tool needs?
- Is the tool trying to access a resource (Sheet, API, etc.) that
  isn't shared with the service account?

#### "One of my tools ({tool_name}) hit a rate limit..."

Appears when a `function_response` contains rate-limit indicators (429,
quota, resource_exhausted, etc.). The tool executed but was throttled
by an external API.

**What to check:**
- Project quotas for the affected API.
- Rate limiting in the external service.
- Consider adding backoff/retry logic in the tool.

#### "One of my tools ({tool_name}) doesn't have the access it needs..."

Appears when a `function_response` contains permission-denied indicators
(403, forbidden, unauthorized, etc.).

**What to check:**
- IAM bindings for the service account.
- Resource sharing (Google Sheets, Docs, etc.).
- API enablement in the project.

#### "Oh no, I appear to have a broken tool..."

Fallback when tools were called and responded, but the agent still
produced no text. Usually a "tool loop" — the agent keeps calling tools
without summarising results.

**What to check:**
1. **Your agent's prompt** — does it explicitly require a text response
   after tool calls? Many tool-loop cases are fixed by adding an
   instruction like *"After running tools, always summarise the result
   in a short message to the user."*
2. **The named tool** — is it returning confusing output that makes the
   agent call more tools instead of responding?
3. **Token / iteration limits** — if the agent runs out of tokens or
   iteration budget mid-loop, it can finish without writing text.

### Diagnostic logging

The middleware logs every empty-response event with detailed
diagnostics:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   AND resource.labels.service_name="the-forum"
   AND textPayload:"Empty text extracted"' \
  --project=YOUR_FORUM_PROJECT_ID \
  --format='value(timestamp,textPayload)' \
  --limit=50
```

Log entries include:
- **Chunk breakdown**: `text=0 function_call=3 function_response=0`
- **Functions called**: `get_memory,search_web,update_record`
- **Function errors detected**: `[{"tool_name":"search_web","error_type":"rate_limit"}]`

The pattern `function_call=N function_response=0` is a strong signal of
a permissions problem with the first tool called.

The Forum does **not** automatically retry these failures, because the
tool calls the agent already made may have side effects (creating
records, sending notifications, etc.) and replaying the same turn could
double them up. Fix the underlying agent issue rather than relying on
retries.

### Other empty-response branches

If the stream ends with no chunks at all (a different failure shape
that has not been observed in production but is handled defensively),
the user gets a generic *"I wasn't able to process that request"*
message instead of the broken-tool one. If the user attached an image
when this happens, the Forum adds *"I may not be set up to handle
images"* — usually a sign your agent's prompt or model doesn't have
image support enabled.

### Scheduled job failure tracking

When scheduled jobs produce empty responses (same failure patterns as
interactive messages), the Forum tracks them in Firestore:

| Field | Description |
|---|---|
| `consecutive_failures` | Number of failures since last success |
| `last_error` | Description of most recent failure |
| `last_execution_at` | Timestamp of last successful execution |

**Failure types recorded in `last_error`:**
- `Tool 'X' did not respond (possible permission issue)` — tool never executed
- `Tool 'X' hit rate limit` — tool was throttled
- `Empty response (N chunks)` — agent returned no text (generic)

**User notification:** if a job fails 288 consecutive times (~24 hours
with the default 5-minute dispatcher), the user receives:

> My scheduled job *{job_name}* has not been working since {last_execution_at}.

This gives users visibility into persistent failures without spamming
them on every failed attempt.

**Recovery:** the job keeps running on schedule. Once the underlying
issue is fixed (permissions granted, rate limit cleared, etc.), the
next successful execution resets `consecutive_failures` to 0 and the
user receives the normal scheduled message.

**Monitoring scheduled job health:**

```bash
# Find jobs with failures
gcloud firestore documents list scheduled_jobs \
  --project=YOUR_FORUM_PROJECT_ID \
  --format="table(name,data.consecutive_failures,data.last_error)"

# Check logs for specific job failures
gcloud logging read \
  'resource.type="cloud_run_revision"
   AND textPayload:"Job" AND textPayload:"failed"' \
  --project=YOUR_FORUM_PROJECT_ID \
  --limit=20
```

---

## Adding Your Own MCP Servers (Agent-Side)

The Forum **does not proxy general-purpose MCP servers** (Garmin,
GitHub, Filesystem, etc.). Each agent integrates those directly via
ADK's `MCPToolset`, owning the connection in its own Reasoning Engine
container. This keeps the Forum focused on identity, delivery, and
scheduling — not tool routing.

> **The one exception is the scheduler MCP**, which the Forum *does*
> host because the scheduling logic and data live in the Forum's
> Firestore. See [Scheduler MCP Server](#scheduler-mcp-server) above.

### Why agent-side MCP

- **No Forum changes needed** to add tools — agents own their toolchain
  end-to-end.
- **Per-user credentials** are easier to handle in agent code, where
  you already have the user's identity.
- **Failure isolation** — a flaky MCP server only impacts that one
  agent, not the whole platform.
- **Aligned with ADK design** — `MCPToolset` is a first-class ADK
  primitive.

### Implementation patterns

For working code examples covering both transports — stdio (`uvx` /
`npx` ecosystem servers) and Streamable HTTP / SSE (hosted MCP
servers) — see Agent-Template's README and `requirements.txt`. The
template already wires `uvx` into its dependencies and has
boilerplate for `MCPToolset(StdioServerParameters(...))` and
`MCPToolset(StreamableHTTPConnectionParams(...))`.

### Per-user credentials (the Forum contract)

If the MCP server needs user-specific credentials (Garmin, Gmail,
Calendar, etc.), instantiate the toolset **per request** using the
calling user's stored credentials, rather than a process-wide token.
The Forum passes the user's identity in the `message` prefix —
`[From: Name | slack_id: U0ABC123]` for scheduled jobs, `[From: Name]`
for interactive turns. Your agent uses that to look up the right
credential from your Secret Manager / Firestore before constructing
the toolset for the request.

This per-request pattern is what makes the cross-platform identity
work cleanly: the same user on Slack and Telegram resolves to the
same Forum `user_id`, which maps to the same per-user credentials in
your agent's storage.

---

## Operator-Side Identity Linking

The Forum maintains a unified `users` collection that links a person's
Slack, Google Chat, Telegram, and Discord identities into a single
record. Your agent always sees the same `user_id` (the person's name)
regardless of which platform they messaged from.

### Auto-linking

When a message arrives, the Forum:

1. **Creates** a user with that platform identity if no match exists.
2. **Email-links** to an existing user if both platforms surface the
   same email address.
3. Otherwise leaves the new identity as a standalone user — needing a
   manual link.

### Manual linking

Two common cases need the operator to merge identities:

- **Telegram doesn't surface email**, so Telegram identities for an
  existing Slack/Google Chat user must be linked manually.
- **Same person, different email addresses** (work Slack vs personal
  Google Chat) — auto-linking won't catch this.

Run from the Forum repo:

```bash
# Step 1: Find the user IDs to link
python scripts/check_user_identities.py

# Example output:
# User abc123:
#   Name: Jonathan Cavell
#   Email: jonathan@company.com
#   Identities:
#     - slack: U0ABC123 (Jonathan Cavell)
#     - google_chat: users/123456 (Jonathan)
#
# User xyz789:
#   Name: Jonathan
#   Identities:
#     - telegram: 987654321 (Jonathan)

# Step 2: Link the Telegram identity into the existing user
python scripts/link_identities.py \
  --user-id abc123 \
  --platform telegram \
  --platform-user-id 987654321 \
  --display-name "Jonathan"
```

#### Script parameters

- `--user-id` — the Firestore user document ID to add the identity to
  (the one being kept).
- `--platform` — one of `slack`, `google_chat`, `telegram`, `discord`.
- `--platform-user-id` — the platform-specific user ID being merged in.
- `--display-name` — the user's display name on that platform.
- `--project-id` — required, or set the `GCP_PROJECT_ID` env var. Points at your Forum project's Firestore.

### Unlinking

Rare. Edit the user document in the Firestore console: `users` → user
doc → remove the entry from the `identities` array → bump
`updated_at`. There's no script for this on purpose; unlinking should
require human review.

---

## Forum-Side Troubleshooting

For agent-side problems (deploy errors, ADK boot failures, your bot
token rotated, your secret isn't in the agent's project), see
Agent-Template's troubleshooting. This section is only for
Forum-side issues.

### "URL verification failed" on Slack Event Subscriptions

The Forum's `SLACK_SIGNING_SECRET` env var is a **comma-separated list**
— one entry per Slack app it serves. Your new bot's signing secret
must be in this list **before** you set its Event Subscriptions
Request URL in the Slack app config, or the URL verification
challenge will fail with no clear error in Slack's UI.

```bash
# In the Forum's .env (deployed via gcloud run services update ... or terraform)
SLACK_SIGNING_SECRET=existing-secret,new-bot-signing-secret
```

Find each secret at *Slack app → Basic Information → Signing Secret*.

Sanity check the Forum is reachable:

```bash
curl https://YOUR_FORUM_URL/health
# {"status":"healthy"}
```

For local dev with ngrok, confirm the tunnel is live before
configuring Event Subscriptions:

```bash
curl http://localhost:4040/api/tunnels   # ngrok admin API
```

### "No response, no errors" from a registered agent

Most often: the Forum is happy, but your agent isn't running or isn't
responding.

```bash
# 1. Is the agent registered in Firestore?
gcloud firestore documents list agents

# 2. Is there a session for this user?
gcloud firestore documents list sessions --limit=10

# 3. Test the agent directly in the Vertex AI Console
#    (Reasoning Engines → your engine → Test)
```

If the Vertex AI test succeeds but the Forum still returns nothing,
check the Forum's Cloud Run logs for the empty-response diagnostics
described in [How The Forum Handles Agent Errors](#how-the-forum-handles-agent-errors).

### Image not reaching the agent

The Slack bot needs the `files:read` OAuth scope. If you added image
support to an existing bot, reinstall the app to the workspace after
adding the scope, or the bot still won't see file events. The Forum
logs a clear `"Downloaded image: image/png, NNNN bytes"` line per
inbound image, followed by `"Sending 1 image(s) to Reasoning Engine"`
when it forwards to your agent. Absence of both lines means the bot
itself never got the file event.
