# Using the Global MCP Servers

This guide is for the **middleware owner** who wants to expose MCP (Model Context Protocol) tools to Claude Code or other AI tools via the middleware.

For agent developers who want to add MCP tools to their Vertex AI ADK agent, see the [For Agent Developers guide](FOR_AGENT_DEVELOPERS.md#11-using-mcp-servers-with-your-agent).

---

## Table of Contents

1. [Overview](#1-overview)
2. [Prerequisites](#2-prerequisites)
3. [Configuring the Global API Key](#3-configuring-the-global-api-key)
4. [Adding Global MCP Servers in Firestore](#4-adding-global-mcp-servers-in-firestore)
5. [Building a Custom MCP Server](#5-building-a-custom-mcp-server)
6. [Deploying a Custom HTTP Server to Cloud Run](#6-deploying-a-custom-http-server-to-cloud-run)
7. [Connecting Claude Code](#7-connecting-claude-code)
8. [Verifying with mcp-inspector](#8-verifying-with-mcp-inspector)
9. [Transports and Tool Naming](#9-transports-and-tool-naming)

---

## 1. Overview

The middleware exposes **one URL per globally-registered MCP server**, shaped:

```
GET/POST/DELETE  {middleware_url}/api/v1/mcp/global/{server_name}
```

Each URL:
- Uses **Streamable HTTP** transport (MCP spec 2025-03-26); a legacy SSE variant is available at `/api/v1/mcp/global/{server_name}/sse`
- Requires the same shared `X-API-Key` header (the `mcp-global-api-key` secret)
- Proxies **exactly one** backing server — no aggregation, no tool name prefixing
- Maps to a document in the top-level `mcp_servers` Firestore collection

Each backing server can use one of three transports:

| Transport | When to use |
|-----------|-------------|
| `stdio` | Running an npm/pypi-packaged MCP server as a subprocess (`npx @modelcontextprotocol/server-X`, `uvx mcp-server-Y`). Most ecosystem servers ship this way. |
| `streamable_http` | Modern HTTP-based MCP servers (you deployed a custom server that speaks the 2025-03-26 spec). |
| `sse` | Legacy HTTP-based MCP servers (older FastMCP deployments, etc.). |

Agent endpoints (`/api/v1/mcp/{agent_id}`) still aggregate all of an agent's servers into one tool surface — that's unchanged.

---

## 2. Prerequisites

- Admin access to the middleware GCP project
- The middleware deployed to Cloud Run (Terraform apply completed)
- `gcloud` CLI authenticated

---

## 3. Configuring the Global API Key

### Step 1: Generate and store the API key

```bash
export MIDDLEWARE_PROJECT_ID=your-middleware-project-id

openssl rand -base64 32 | tr -d '\n' | \
  gcloud secrets versions add mcp-global-api-key \
    --data-file=- \
    --project=$MIDDLEWARE_PROJECT_ID
```

The `mcp-global-api-key` secret was created by Terraform. You only need to populate it.

### Step 2: Enable the endpoints on the Cloud Run service

Set `MCP_GLOBAL_API_KEY_SECRET` so the middleware knows where to find the key:

```bash
export CLOUD_RUN_SERVICE=slack-vertex-middleware
export REGION=us-central1

gcloud run services update $CLOUD_RUN_SERVICE \
  --set-env-vars MCP_GLOBAL_API_KEY_SECRET=mcp-global-api-key \
  --region=$REGION \
  --project=$MIDDLEWARE_PROJECT_ID
```

### Step 3: Save the API key value

```bash
gcloud secrets versions access latest \
  --secret=mcp-global-api-key \
  --project=$MIDDLEWARE_PROJECT_ID
```

You'll need this in [Section 7](#7-connecting-claude-code).

---

## 4. Adding Global MCP Servers in Firestore

Each global MCP server is a document in the top-level `mcp_servers` collection. The **document ID must equal the server's `name` field** — the URL path `/api/v1/mcp/global/{server_name}` looks up the document by that ID.

### Common fields (all transports)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Must match the Firestore document ID |
| `transport` | string | No | `"sse"` (default), `"streamable_http"`, or `"stdio"` |
| `enabled` | boolean | Yes | Set `true` to activate |

### HTTP transport fields (`sse`, `streamable_http`)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | Yes | Backing MCP server endpoint URL |
| `api_key_secret` | string | No | Secret Manager secret name holding the backing server's API key |
| `api_key_project_id` | string | No | Project where the secret lives (defaults to middleware project) |
| `api_key_header` | string | No | Header to send the API key in (defaults to `X-API-Key`) |

### stdio transport fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `command` | string | Yes | Must be `"npx"` or `"uvx"` (allowlist) |
| `args` | list<string> | Yes | Arguments (e.g. `["-y", "@modelcontextprotocol/server-github"]`) |
| `env` | map<string,string> | No | Literal environment variables for the subprocess |
| `env_secrets` | map<string,string> | No | Env var name → Secret Manager secret name (resolved at call time) |

**Security note:** the stdio `command` is restricted to an allowlist (`npx`, `uvx`) defined in [app/models/agent.py](../app/models/agent.py) to prevent arbitrary code execution via Firestore writes. The packages you run via those commands still execute arbitrary code, so treat the `mcp_servers` collection as sensitive.

### Example: HTTP server with API key

```json
{
  "name": "github-http",
  "transport": "streamable_http",
  "enabled": true,
  "url": "https://your-github-mcp.example.com/mcp",
  "api_key_secret": "github-mcp-api-key",
  "api_key_project_id": "your-middleware-project-id"
}
```

### Example: stdio server via npx

```json
{
  "name": "github",
  "transport": "stdio",
  "enabled": true,
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-github"],
  "env_secrets": {
    "GITHUB_PERSONAL_ACCESS_TOKEN": "github-pat-secret"
  }
}
```

### Example: stdio server via uvx

```json
{
  "name": "time",
  "transport": "stdio",
  "enabled": true,
  "command": "uvx",
  "args": ["mcp-server-time", "--local-timezone=America/New_York"]
}
```

### Writing the Firestore document

Either via the Firebase Console (create a document in `mcp_servers` with the fields above) or via a small script:

```python
from google.cloud import firestore
db = firestore.Client(project="your-middleware-project-id")
db.collection("mcp_servers").document("github").set({
    "name": "github",
    "transport": "stdio",
    "enabled": True,
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env_secrets": {"GITHUB_PERSONAL_ACCESS_TOKEN": "github-pat-secret"},
})
```

For `env_secrets`, store the token in Secret Manager and grant the middleware's Cloud Run SA `secretAccessor`:

```bash
echo -n "ghp_YOUR_TOKEN" | \
  gcloud secrets versions add github-pat-secret \
    --data-file=- --project=$MIDDLEWARE_PROJECT_ID

MIDDLEWARE_SA="$(gcloud projects describe $MIDDLEWARE_PROJECT_ID \
  --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding github-pat-secret \
  --member="serviceAccount:${MIDDLEWARE_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$MIDDLEWARE_PROJECT_ID
```

---

## 5. Building a Custom MCP Server

If you want to expose your own tools and the stdio packages don't cover them, you can build a custom MCP server.

### stdio (simplest — no infrastructure)

Package your server for npm or PyPI, then reference it via `npx` or `uvx` in the Firestore config. No Cloud Run deployment needed.

Example using [FastMCP](https://github.com/jlowin/fastmcp) packaged for PyPI:

```python
# my_tools/server.py
from fastmcp import FastMCP
mcp = FastMCP("my-tools")

@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

if __name__ == "__main__":
    mcp.run()  # stdio by default
```

Publish to PyPI as `my-mcp-tools`, then register:

```json
{
  "name": "my-tools",
  "transport": "stdio",
  "enabled": true,
  "command": "uvx",
  "args": ["my-mcp-tools"]
}
```

### HTTP (for long-running services with state)

See [Section 6](#6-deploying-a-custom-http-server-to-cloud-run) for deployment.

```python
# server.py
import os
from fastmcp import FastMCP
mcp = FastMCP("my-tools")

@mcp.tool()
def hello(name: str) -> str:
    """Say hello."""
    return f"Hello, {name}!"

if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
```

---

## 6. Deploying a Custom HTTP Server to Cloud Run

For HTTP-based custom servers (`transport: "sse"` or `"streamable_http"`), deploy to Cloud Run:

```bash
export PROJECT_ID=your-project-id
export SERVER_NAME=my-tools-mcp

gcloud builds submit \
  --tag gcr.io/$PROJECT_ID/$SERVER_NAME:latest \
  --project=$PROJECT_ID \
  /path/to/your/mcp-server

gcloud run deploy $SERVER_NAME \
  --image gcr.io/$PROJECT_ID/$SERVER_NAME:latest \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars MCP_API_KEY_SECRET=your-mcp-api-key-secret \
  --project=$PROJECT_ID

export MCP_SERVER_URL=$(gcloud run services describe $SERVER_NAME \
  --region us-central1 --format 'value(status.url)' --project=$PROJECT_ID)
```

Generate an API key for the server and grant the middleware access (same pattern as stdio `env_secrets` above). Then register in Firestore:

```python
db.collection("mcp_servers").document("my-tools").set({
    "name": "my-tools",
    "transport": "sse",
    "enabled": True,
    "url": f"{MCP_SERVER_URL}/sse",
    "api_key_secret": "your-mcp-api-key-secret",
    "api_key_project_id": PROJECT_ID,
})
```

---

## 7. Connecting Claude Code

Each MCP server has its own URL and is added to Claude Code as a **separate** MCP server entry. All of them share the same `X-API-Key`.

### `~/.claude/settings.json`

```json
{
  "mcpServers": {
    "github": {
      "type": "http",
      "url": "https://YOUR_CLOUD_RUN_URL/api/v1/mcp/global/github",
      "headers": {
        "X-API-Key": "YOUR_API_KEY"
      }
    },
    "time": {
      "type": "http",
      "url": "https://YOUR_CLOUD_RUN_URL/api/v1/mcp/global/time",
      "headers": {
        "X-API-Key": "YOUR_API_KEY"
      }
    }
  }
}
```

The `mcpServers` **key** (`"github"`, `"time"`) is how Claude Code labels the server locally — it doesn't have to match the Firestore name, but keeping them in sync reduces confusion. The **URL path segment** (`/global/github`, `/global/time`) is what must match the Firestore document ID.

After saving, restart Claude Code.

---

## 8. Verifying with mcp-inspector

```bash
npx @modelcontextprotocol/inspector
```

Enter:
- **Transport type**: `Streamable HTTP`
- **URL**: `https://YOUR_CLOUD_RUN_URL/api/v1/mcp/global/{server_name}`
- **Headers**: `X-API-Key: YOUR_API_KEY`

You should see all tools from that one backing server. If nothing appears, check:
1. The Firestore document exists and `enabled: true`
2. The document ID matches `{server_name}` in the URL
3. For stdio: the `command` is `npx` or `uvx`, and the package runs successfully locally
4. For stdio: the Cloud Run instance has enough memory — npm package installs can hit OOM

---

## 9. Transports and Tool Naming

**Global per-server endpoints** expose the backing server's tools **unprefixed**. If your backing server has a `create_issue` tool, Claude Code sees it as `create_issue`. Naming collisions across servers are avoided naturally because each server has its own URL.

**Agent-scoped endpoints** (`/api/v1/mcp/{agent_id}`) still aggregate and prefix: a tool from backing server `"github"` becomes `"github__create_issue"` on that agent's MCP surface. This is unchanged and lets ADK agents point their single `MCPToolset` at one URL that exposes all their tools.

**Transport-specific notes:**
- **stdio cold start** can add 5-10 seconds on first call while `npx` fetches the package. Cloud Run's instance lifecycle means this recurs after scale-to-zero.
- **Streamable HTTP** works correctly with multi-instance Cloud Run (stateless per-request).
- **SSE** stores session state in process memory; if Cloud Run routes GET /sse and POST /messages to different instances, sessions break. Prefer Streamable HTTP for production.
