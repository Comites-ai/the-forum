# Using the Global MCP Server

This guide is for the **middleware owner** who wants to expose MCP (Model Context Protocol) tools to Claude Code or other AI tools via the middleware's global endpoint.

For agent developers who want to add MCP tools to their Vertex AI ADK agent, see the [For Agent Developers guide](FOR_AGENT_DEVELOPERS.md#11-using-mcp-servers-with-your-agent).

---

## Table of Contents

1. [Overview](#1-overview)
2. [Prerequisites](#2-prerequisites)
3. [Configuring the Global API Key](#3-configuring-the-global-api-key)
4. [Adding Global MCP Servers in Firestore](#4-adding-global-mcp-servers-in-firestore)
5. [Building a Custom MCP Server](#5-building-a-custom-mcp-server)
6. [Deploying a Custom Server to Cloud Run](#6-deploying-a-custom-server-to-cloud-run)
7. [Connecting Claude Code](#7-connecting-claude-code)
8. [Verifying with mcp-inspector](#8-verifying-with-mcp-inspector)
9. [Tool Naming Conventions](#9-tool-naming-conventions)

---

## 1. Overview

The middleware exposes a **global MCP endpoint** at:

```
GET/POST/DELETE  {middleware_url}/api/v1/mcp
```

This endpoint:
- Uses **Streamable HTTP** transport (MCP spec 2025-03-26) — the modern standard
- Requires an `X-API-Key` header for authentication
- Aggregates tools from **all configured MCP servers**: both global-only servers (stored in the `mcp_servers` Firestore collection) and servers registered for each agent
- Is intended for the middleware owner — use it with Claude Code, custom scripts, or any MCP-compatible client

Tools from backing servers are prefixed to prevent collisions:
- Global servers: `{server_name}__{tool_name}` (e.g., `github__create_issue`)
- Agent servers: `{agent_id}__{server_name}__{tool_name}` (e.g., `growthcoach__calendar__list_events`)

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

# Generate a random API key and store it in Secret Manager
openssl rand -base64 32 | tr -d '\n' | \
  gcloud secrets versions add mcp-global-api-key \
    --data-file=- \
    --project=$MIDDLEWARE_PROJECT_ID
```

The `mcp-global-api-key` Secret Manager secret was created by Terraform. You only need to populate it with a value.

### Step 2: Enable the endpoint on the Cloud Run service

Set the `MCP_GLOBAL_API_KEY_SECRET` environment variable to tell the middleware where to find the API key:

```bash
export CLOUD_RUN_SERVICE=slack-vertex-middleware  # your Cloud Run service name
export REGION=us-central1

gcloud run services update $CLOUD_RUN_SERVICE \
  --set-env-vars MCP_GLOBAL_API_KEY_SECRET=mcp-global-api-key \
  --region=$REGION \
  --project=$MIDDLEWARE_PROJECT_ID
```

### Step 3: Save the API key value for client configuration

```bash
# Retrieve the key you just stored
gcloud secrets versions access latest \
  --secret=mcp-global-api-key \
  --project=$MIDDLEWARE_PROJECT_ID
```

Save this value — you'll need it in [Section 7](#7-connecting-claude-code).

---

## 4. Adding Global MCP Servers in Firestore

Global MCP servers (not tied to any agent) are stored in a top-level Firestore collection called `mcp_servers`. Each document represents one backing MCP server.

### Adding a server via the Firebase Console

1. Open [Firebase Console](https://console.firebase.google.com) → your middleware project → Firestore
2. Navigate to the `mcp_servers` collection (create it if it doesn't exist)
3. Create a new document with the **document ID** set to the server name (e.g., `github`)
4. Add the following fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Friendly name, becomes tool prefix (e.g. `github`) |
| `url` | string | Yes | MCP server SSE endpoint URL |
| `enabled` | boolean | Yes | Set `true` to activate, `false` to disable |
| `api_key_secret` | string | No | Secret Manager secret name for the API key |
| `api_key_project_id` | string | No | GCP project where the secret lives (defaults to middleware project) |
| `api_key_header` | string | No | Header name to send API key in (defaults to `X-API-Key`) |

### Example: Adding a public GitHub MCP server

```json
{
  "name": "github",
  "url": "https://your-github-mcp-server.example.com/sse",
  "enabled": true,
  "api_key_secret": "github-mcp-api-key",
  "api_key_project_id": "your-middleware-project-id"
}
```

### Adding via gcloud (alternative)

```bash
# Store the API key for a backing server
echo -n "YOUR_BACKING_SERVER_API_KEY" | \
  gcloud secrets versions add github-mcp-api-key \
    --data-file=- \
    --project=$MIDDLEWARE_PROJECT_ID
```

Then write the Firestore document via the Firebase Admin SDK, Firebase Console, or a short Python script:

```python
from google.cloud import firestore
db = firestore.Client(project="your-middleware-project-id")
db.collection("mcp_servers").document("github").set({
    "name": "github",
    "url": "https://your-github-mcp-server.example.com/sse",
    "enabled": True,
    "api_key_secret": "github-mcp-api-key",
    "api_key_project_id": "your-middleware-project-id",
})
```

---

## 5. Building a Custom MCP Server

If you want to expose your own tools, you can build a custom MCP server and deploy it as a Cloud Run service.

### Recommended approach: FastMCP

[FastMCP](https://github.com/jlowin/fastmcp) is the simplest way to build Python MCP servers.

```bash
pip install fastmcp
```

### Minimal example (`server.py`)

```python
import os
from fastmcp import FastMCP

mcp = FastMCP("my-tools")

API_KEY = os.environ.get("MCP_API_KEY")

@mcp.tool()
def hello(name: str) -> str:
    """Say hello to someone."""
    return f"Hello, {name}!"

@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

if __name__ == "__main__":
    # Run as SSE server (for middleware compatibility)
    mcp.run(transport="sse", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
```

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY server.py .

CMD ["python", "server.py"]
```

`requirements.txt`:
```
fastmcp>=0.4.0
```

### Adding API key authentication

FastMCP does not handle auth natively — protect your server at the infrastructure level (Cloud Run IAM) or add middleware in your server:

```python
import os
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from mcp.types import JSONRPCMessage

mcp = FastMCP("my-tools")
EXPECTED_KEY = os.environ.get("MCP_API_KEY", "")

class ApiKeyMiddleware(Middleware):
    async def on_message(self, message: JSONRPCMessage, call_next):
        # Note: auth at HTTP layer is simpler — see Nginx/Cloud Run IAM approach
        return await call_next(message)
```

The simplest production approach: keep Cloud Run public (unauthenticated), protect with an API key checked at the HTTP level. The middleware passes the API key via the header you specify in `api_key_header` (default: `X-API-Key`).

---

## 6. Deploying a Custom Server to Cloud Run

The agent-project Terraform template includes a commented-out Section 5 for deploying a custom MCP server. You can also deploy directly:

### Build and push the container image

```bash
export PROJECT_ID=your-agent-project-id
export SERVER_NAME=my-tools-mcp

# Build and push using Cloud Build
gcloud builds submit \
  --tag gcr.io/$PROJECT_ID/$SERVER_NAME:latest \
  --project=$PROJECT_ID \
  /path/to/your/mcp-server
```

### Deploy to Cloud Run

```bash
gcloud run deploy $SERVER_NAME \
  --image gcr.io/$PROJECT_ID/$SERVER_NAME:latest \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars MCP_API_KEY_SECRET=your-mcp-api-key-secret \
  --project=$PROJECT_ID

# Get the URL
export MCP_SERVER_URL=$(gcloud run services describe $SERVER_NAME \
  --region us-central1 \
  --format "value(status.url)" \
  --project=$PROJECT_ID)

echo "MCP server URL: $MCP_SERVER_URL/sse"
```

### Generate and store an API key for the server

```bash
# Generate key
openssl rand -base64 32 | tr -d '\n' | \
  gcloud secrets versions add your-mcp-api-key-secret \
    --data-file=- \
    --project=$PROJECT_ID

# Grant middleware SA access to read this secret
MIDDLEWARE_PROJECT_NUMBER=$(gcloud projects describe $MIDDLEWARE_PROJECT_ID \
  --format="value(projectNumber)")
MIDDLEWARE_SA="${MIDDLEWARE_PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding your-mcp-api-key-secret \
  --member="serviceAccount:${MIDDLEWARE_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$PROJECT_ID
```

### Register the server in Firestore

Add it to the `mcp_servers` collection as described in [Section 4](#4-adding-global-mcp-servers-in-firestore):

```python
db.collection("mcp_servers").document("my-tools").set({
    "name": "my-tools",
    "url": f"{MCP_SERVER_URL}/sse",
    "enabled": True,
    "api_key_secret": "your-mcp-api-key-secret",
    "api_key_project_id": PROJECT_ID,
})
```

---

## 7. Connecting Claude Code

Add the middleware's global MCP endpoint as an MCP server in Claude Code.

### Option A: Claude Code settings UI

1. Open Claude Code settings (⚙️ or `claude config`)
2. Navigate to MCP Servers
3. Add a new server with:
   - **Name**: `middleware` (or any name you prefer)
   - **URL**: `{middleware_url}/api/v1/mcp`
   - **Headers**: `X-API-Key: {your-api-key}`

### Option B: `~/.claude/settings.json`

```json
{
  "mcpServers": {
    "middleware": {
      "type": "http",
      "url": "https://YOUR_CLOUD_RUN_URL/api/v1/mcp",
      "headers": {
        "X-API-Key": "YOUR_API_KEY"
      }
    }
  }
}
```

Replace `YOUR_CLOUD_RUN_URL` with the Cloud Run service URL (from `terraform output cloud_run_url`) and `YOUR_API_KEY` with the value from [Section 3](#3-configuring-the-global-api-key).

After saving, restart Claude Code. The connected MCP tools will appear in the tool picker.

---

## 8. Verifying with mcp-inspector

[mcp-inspector](https://github.com/modelcontextprotocol/inspector) is a CLI tool for testing MCP endpoints.

```bash
npx @modelcontextprotocol/inspector
```

When prompted, enter:
- **Transport type**: `Streamable HTTP`
- **URL**: `https://YOUR_CLOUD_RUN_URL/api/v1/mcp`
- **Headers**: `X-API-Key: YOUR_API_KEY`

You should see:
- A list of all available tools, prefixed by server name
- The ability to call individual tools and see their responses

If tools from a specific backing server don't appear, check:
1. The server document in Firestore has `enabled: true`
2. The `url` points to a working SSE endpoint (accessible from Cloud Run)
3. The `api_key_secret` exists and the middleware SA has `secretAccessor` access

---

## 9. Tool Naming Conventions

The middleware prefixes tool names to prevent collisions when aggregating from multiple backing servers:

| Source | Tool prefix format | Example |
|--------|-------------------|---------|
| Global-only server | `{server_name}__{tool_name}` | `github__create_issue` |
| Agent-specific server | `{agent_id}__{server_name}__{tool_name}` | `growthcoach__calendar__list_events` |

The double underscore (`__`) is the separator. When the middleware routes a tool call, it parses the prefix to determine which backing server to forward the call to.

**Naming tips:**
- Keep server names short and lowercase (e.g., `github`, `gdrive`, `mytools`)
- Avoid underscores in server names to prevent ambiguous prefix parsing
- Server names must be unique within the set of servers visible to an endpoint
