# Adding a New Platform to The Forum

This guide documents the complete process for integrating a new messaging platform (like Telegram, WhatsApp, etc.) into The Forum.

> **Note on platforms that don't fit the webhook model.** The pattern below assumes the platform delivers events to an HTTP webhook. Discord doesn't — its DMs only arrive over a Gateway WebSocket connection. The Discord integration handles this by adding a small separate worker service that holds the WebSocket and forwards events to a webhook handler on the Forum. If your target platform has the same constraint, see [DISCORD_WORKER.md](DISCORD_WORKER.md) for the worker pattern.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Implementation Steps](#implementation-steps)
3. [Testing and Deployment](#testing-and-deployment)
4. [User Identity Linking](#user-identity-linking)
5. [Documentation Updates](#documentation-updates)
6. [Reference: Telegram Integration](#reference-telegram-integration)

## Architecture Overview

The Forum uses a **platform abstraction pattern** that allows it to support multiple messaging platforms through a unified interface.

### Key Components

1. **PlatformConnector (ABC)**: Abstract base class defining the interface all platforms must implement
2. **PlatformEvent**: Unified event schema that normalizes messages from all platforms
3. **MessageProcessorV2**: Platform-agnostic message processor that handles all business logic
4. **Cross-Platform Identity System**: Links user identities across multiple platforms

### Message Flow

```
Platform Webhook → parse_event() → PlatformEvent → MessageProcessorV2 → Vertex AI → send_message() → Platform API
```

### Benefits

- **~300-400 lines of code** to add a complete platform integration
- **Zero changes** to core business logic (MessageProcessorV2)
- **Automatic cross-platform sessions**: Users can start conversations on one platform and continue on another
- **Unified user identity**: Same user across Slack, Google Chat, Telegram, etc.

## Implementation Steps

### Step 1: Create Platform Connector

Create a new file: `app/services/platforms/{platform}_connector.py`

The connector must implement the `PlatformConnector` abstract base class with these 6 required methods:

```python
from app.services.platforms.base import PlatformConnector, PlatformEvent
from fastapi import Request
import aiohttp
from typing import Optional

class MyPlatformConnector(PlatformConnector):
    """Connector for MyPlatform messaging platform."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        bot_token_secret: Optional[str] = None,
        bot_token_project_id: Optional[str] = None,
        webhook_secret: Optional[str] = None
    ):
        """
        Initialize connector with credentials.

        Supports both:
        - Direct token (for development): bot_token
        - Secret Manager (for production): bot_token_secret + bot_token_project_id
        """
        if bot_token_secret and bot_token_project_id:
            # Fetch from Secret Manager
            self.bot_token = self._fetch_token_from_secret_manager(
                bot_token_secret, bot_token_project_id
            )
        else:
            self.bot_token = bot_token

        self.webhook_secret = webhook_secret
        self.api_base = f"https://api.myplatform.com/bot{self.bot_token}"

    async def send_message(self, recipient_id: str, text: str) -> dict:
        """Send text message to user."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.api_base}/sendMessage",
                json={"chat_id": recipient_id, "text": text}
            ) as response:
                return await response.json()

    async def download_file(self, file_id: str) -> bytes:
        """Download file from platform."""
        # Platform-specific file download logic
        pass

    async def get_user_info(self, user_id: str) -> dict:
        """Get user profile information."""
        # Platform-specific user info logic
        pass

    async def open_conversation(self, user_id: str) -> str:
        """Open/get conversation ID for direct messaging."""
        # Most platforms use user_id as conversation_id
        return user_id

    async def verify_request(self, request: Request) -> bool:
        """Verify webhook request authenticity."""
        # Platform-specific verification (HMAC, secret token, etc.)
        import secrets
        request_secret = request.headers.get("X-MyPlatform-Secret", "")
        return secrets.compare_digest(self.webhook_secret, request_secret)

    def parse_event(self, data: dict) -> PlatformEvent:
        """Parse platform webhook into unified PlatformEvent."""
        message = data.get("message", {})
        from_user = message.get("from", {})

        # Extract user info
        user_id = str(from_user.get("id"))
        display_name = from_user.get("username") or from_user.get("first_name")

        # Extract message content
        text = message.get("text")
        files = []  # Parse file attachments if present

        # Create unified event
        return PlatformEvent(
            platform="myplatform",
            user_id=user_id,
            conversation_id=str(message.get("chat", {}).get("id")),
            text=text,
            files=files,
            display_name=display_name,
            raw_event=data
        )

    def _fetch_token_from_secret_manager(
        self, secret_name: str, project_id: str
    ) -> str:
        """Fetch bot token from GCP Secret Manager."""
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
```

**Key Implementation Notes:**

- Use `aiohttp` for async HTTP requests (already in requirements.txt)
- Support both direct token and Secret Manager for flexibility
- Use constant-time comparison (`secrets.compare_digest`) for webhook verification
- Always return a complete `PlatformEvent` from `parse_event()`
- Handle file attachments according to platform's API

### Step 2: Create Route Handler

Create a new file: `app/api/v1/{platform}_events.py`

```python
"""MyPlatform Events API endpoint."""
import logging
from fastapi import APIRouter, Request, BackgroundTasks, HTTPException, Depends
from fastapi.responses import JSONResponse

from app.services.message_processor_v2 import MessageProcessorV2
from app.services.platforms.myplatform_connector import MyPlatformConnector
from app.core.dependencies import get_message_processor_v2, get_firestore_service
from app.services.firestore_service import FirestoreService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/myplatform", tags=["myplatform"])


@router.post("/events")
async def myplatform_events(
    request: Request,
    background_tasks: BackgroundTasks,
    message_processor: MessageProcessorV2 = Depends(get_message_processor_v2),
    firestore: FirestoreService = Depends(get_firestore_service),
):
    """
    MyPlatform webhook endpoint.

    Returns 200 immediately. Processes events in background.
    """
    # Parse JSON
    data = await request.json()

    # Platform-specific filtering (ignore bot messages, edits, etc.)
    message = data.get("message")
    if not message:
        return JSONResponse(content={"ok": True})

    from_user = message.get("from", {})
    if from_user.get("is_bot", False):
        logger.debug("Ignoring bot message to prevent loops")
        return JSONResponse(content={"ok": True})

    logger.info(
        f"Received MyPlatform message from user {from_user.get('id')}: "
        f"{message.get('text', '<non-text message>')}"
    )

    # Step 1: Find enabled MyPlatform agent
    agents = await firestore.list_agents()
    agent = None

    for candidate_agent in agents:
        config = candidate_agent.get_myplatform_config()
        if config and config.enabled:
            agent = candidate_agent
            break

    if not agent:
        logger.error("No enabled MyPlatform agent found")
        return JSONResponse(content={"ok": True})

    # Step 2: Get platform config from agent
    platform_config = agent.get_myplatform_config()
    if not platform_config or not platform_config.enabled:
        logger.error(f"Agent {agent.id} does not have MyPlatform enabled")
        return JSONResponse(content={"ok": True})

    # Validate credentials
    has_direct_token = platform_config.myplatform_bot_token is not None
    has_secret_config = (
        platform_config.myplatform_bot_token_secret is not None and
        platform_config.myplatform_bot_token_project_id is not None
    )

    if not has_direct_token and not has_secret_config:
        logger.error(
            f"Agent {agent.id} MyPlatform config missing bot token. "
            f"Need either myplatform_bot_token OR (myplatform_bot_token_secret + myplatform_bot_token_project_id)"
        )
        return JSONResponse(content={"ok": True})

    # Step 3: Verify webhook secret (if configured)
    webhook_secret = platform_config.myplatform_webhook_secret
    if webhook_secret:
        connector_check = MyPlatformConnector(
            bot_token=platform_config.myplatform_bot_token if has_direct_token else None,
            bot_token_secret=platform_config.myplatform_bot_token_secret if has_secret_config else None,
            bot_token_project_id=platform_config.myplatform_bot_token_project_id if has_secret_config else None,
            webhook_secret=webhook_secret
        )

        if not await connector_check.verify_request(request):
            logger.warning("Invalid MyPlatform webhook secret")
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    # Step 4: Create connector with agent's credentials
    connector = MyPlatformConnector(
        bot_token=platform_config.myplatform_bot_token if has_direct_token else None,
        bot_token_secret=platform_config.myplatform_bot_token_secret if has_secret_config else None,
        bot_token_project_id=platform_config.myplatform_bot_token_project_id if has_secret_config else None,
        webhook_secret=None  # Not needed for sending messages
    )

    # Step 5: Parse platform update into platform event
    platform_event = connector.parse_event(data)

    # Step 6: Process event in background
    background_tasks.add_task(
        message_processor.process_platform_event,
        platform_event,
        connector,
        agent.id
    )

    # Return immediately
    return JSONResponse(content={"ok": True})
```

**Key Implementation Notes:**

- Always return 200 OK immediately to acknowledge webhook
- Process messages in background using FastAPI's `BackgroundTasks`
- Filter out bot messages, edits, and other non-message events
- For MVP: Use first enabled agent (production can route by bot username or separate URLs)
- Verify webhook authenticity before processing
- Log all important events for debugging

**Important: Multiple Bots on Same Platform**

If you have multiple agents using the same platform (e.g., two different MyPlatform bots), you'll need agent-specific webhook URLs to route messages correctly. Add a parameterized route:

```python
@router.post("/events/{agent_id}")
async def myplatform_events_for_agent(
    request: Request,
    background_tasks: BackgroundTasks,
    agent_id: str,
    message_processor: MessageProcessorV2 = Depends(get_message_processor_v2),
    firestore: FirestoreService = Depends(get_firestore_service),
):
    """
    MyPlatform webhook endpoint for a specific agent.

    Each bot should be configured with its own webhook URL:
    - /api/v1/myplatform/events/AGENT_ID_1
    - /api/v1/myplatform/events/AGENT_ID_2
    """
    # ... same logic as above, but use the agent_id parameter directly
    # instead of searching for the first enabled agent
```

This ensures each bot's messages are routed to the correct agent configuration. Keep the non-parameterized `/events` endpoint for backward compatibility.

### Step 3: Register Router

Edit `app/api/v1/routes.py`:

```python
from app.api.v1 import slack_events_v2, google_chat_events, telegram_events, myplatform_events, scheduled_jobs

router = APIRouter()

router.include_router(slack_events_v2.router)
router.include_router(google_chat_events.router)
router.include_router(telegram_events.router)
router.include_router(myplatform_events.router)  # Add this line
router.include_router(scheduled_jobs.router)
```

### Step 4: Extend Agent Model

Edit `app/models/agent.py`:

Add platform-specific fields to `AgentPlatformConfig`:

```python
class AgentPlatformConfig(BaseModel):
    platform: str = Field(..., description="Platform name (slack, google_chat, telegram, myplatform)")
    enabled: bool = Field(default=True, description="Whether this platform is active")

    # ... existing fields ...

    # MyPlatform-specific fields
    myplatform_bot_token: Optional[str] = Field(
        default=None,
        description="Direct MyPlatform bot token (use myplatform_bot_token_secret instead for production)"
    )
    myplatform_bot_token_secret: Optional[str] = Field(
        default=None,
        description="Secret Manager secret name for MyPlatform bot token (e.g., 'my-agent-myplatform-token')"
    )
    myplatform_bot_token_project_id: Optional[str] = Field(
        default=None,
        description="GCP project ID where the MyPlatform bot token secret is stored"
    )
    myplatform_webhook_secret: Optional[str] = Field(
        default=None,
        description="Secret token for MyPlatform webhook verification"
    )
```

Add convenience method to `Agent`:

```python
class Agent(BaseModel):
    # ... existing code ...

    def get_myplatform_config(self) -> Optional[AgentPlatformConfig]:
        """Get MyPlatform platform configuration (convenience method)."""
        return self.get_platform_config("myplatform")
```

### Step 5: Add Terraform Secret Template

This step happens in the **Agent-Template repo** ([github.com/Comites-ai/Agent-Template](https://github.com/Comites-ai/Agent-Template)), not this Forum repo. Edit `terraform/main.tf` there.

Add a new section for the platform (follow the pattern of SECTION 4: TELEGRAM):

```terraform
# ==============================================================================
# SECTION X: MYPLATFORM-SPECIFIC INFRASTRUCTURE
# ==============================================================================

# Uncomment this section if your agent will use MyPlatform

# resource "google_secret_manager_secret" "myplatform_bot_token" {
#   project   = google_project.agent_project.project_id
#   secret_id = "${var.bot_account_id}-myplatform-token"
#
#   replication {
#     auto {}
#   }
#
#   depends_on = [google_project_service.secretmanager]
# }

# Grant middleware service account access to read MyPlatform bot token
# resource "google_secret_manager_secret_iam_member" "myplatform_token_accessor" {
#   project   = google_project.agent_project.project_id
#   secret_id = google_secret_manager_secret.myplatform_bot_token.secret_id
#   role      = "roles/secretmanager.secretAccessor"
#   member    = "serviceAccount:the-forum@vertex-ai-middleware-prod.iam.gserviceaccount.com"
# }
```

Add setup instructions to the outputs section:

```terraform
output "myplatform_setup_instructions" {
  value = <<-EOT

    === MyPlatform Bot Setup (Optional) ===

    1. Create bot via MyPlatform's bot creation tool
    2. Copy the bot token (format: 1234567890:ABC...)

    3. Store token in Secret Manager:
       gcloud secrets create ${var.bot_account_id}-myplatform-token \
         --project=${google_project.agent_project.project_id} \
         --replication-policy=automatic

       echo -n "YOUR_BOT_TOKEN" | gcloud secrets versions add ${var.bot_account_id}-myplatform-token \
         --project=${google_project.agent_project.project_id} \
         --data-file=-

    4. Grant middleware service account access:
       gcloud secrets add-iam-policy-binding ${var.bot_account_id}-myplatform-token \
         --project=${google_project.agent_project.project_id} \
         --member="serviceAccount:the-forum@vertex-ai-middleware-prod.iam.gserviceaccount.com" \
         --role="roles/secretmanager.secretAccessor"

    5. Configure webhook in MyPlatform:
       URL: https://the-forum-mqwj7cavdq-uc.a.run.app/api/v1/myplatform/events
       Secret: <generate a random secret token>

    6. Register agent with Forum's Firestore: run register_agent.py from
       the Agent-Template repo (it auto-detects platforms from secrets).
  EOT
}
```

Update `terraform.tfvars.example` platform-specific notes:

```terraform
# MYPLATFORM (if using):
#   Secret: {bot_account_id}-myplatform-token
#   Value: <bot token from platform>
```

### Step 6: Update Documentation

#### A. Add to README.md

Update the feature list:

```markdown
## Features

- ✅ Multi-platform support: **Slack**, **Google Chat**, **Telegram**, **MyPlatform**
```

Update the architecture diagram or platform list.

#### B. Add to Agent-Template

The per-platform onboarding flow lives in the [Agent-Template](https://github.com/Comites-ai/Agent-Template) repo, so adding a new platform requires two changes there in addition to the terraform section from Step 5:

**B1. Teach `register_agent.py` to auto-detect the new platform.**

Add a `validate_myplatform()` function that hits the platform's native auth-ping API and returns the bot's user_id (or equivalent), then wire it into `build_platforms()` alongside the existing Slack / Telegram / Discord / Google Chat probes. Pattern from the existing Discord validator:

```python
def validate_myplatform(token: str) -> str:
    """Call MyPlatform's auth endpoint. Returns the bot user_id."""
    req = urllib.request.Request(
        "https://api.myplatform.example/v1/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"  [x] MyPlatform /me failed: HTTP {e.code} {e.reason}")
    bot_id = data.get("id")
    if not bot_id:
        sys.exit(f"  [x] MyPlatform /me returned unexpected payload: {data}")
    print(f"  [OK] MyPlatform:  bot @{data.get('username')} (id {bot_id})")
    return bot_id


# Inside build_platforms(), after the existing platform probes:
myplatform_secret = f"{bot_account_id}-myplatform-token"
if secret_exists(sm_client, agent_project, myplatform_secret):
    token = access_secret(sm_client, agent_project, myplatform_secret)
    bot_id = validate_myplatform(token)
    platforms.append({
        "platform": "myplatform",
        "enabled": True,
        "myplatform_bot_id": bot_id,
        "myplatform_bot_token_secret": myplatform_secret,
        "myplatform_bot_token_project_id": agent_project,
        # add any other platform-specific fields the Forum-side connector reads
    })
```

**B2. Update Agent-Template's README** to document the new platform alongside Slack / Google Chat / Telegram / Discord in its setup walkthrough. The `get_started_linux.sh` bootstrap reads the section list, so make sure the new platform shows up in the platform-toggle prompts.

#### C. (Forum side) FOR_AGENT_DEVELOPERS.md only needs an update if your platform's message contract has nuances beyond `user_id` / `session_id` / `message` / `images`. The "What The Forum Sends Your Agent" section is platform-agnostic by design; only edit it if the new platform changes something visible to the agent (e.g., a different identity-prefix format in the `message` field).

### Step 7: Update Identity Linking Script

The existing `scripts/link_identities.py` should already support any platform name, but verify the validation list includes your new platform:

```python
# Validate platform
valid_platforms = ['slack', 'google_chat', 'telegram', 'myplatform']
if platform not in valid_platforms:
    print(f"ERROR: Invalid platform '{platform}'. Must be one of: {', '.join(valid_platforms)}")
    sys.exit(1)
```

## Testing and Deployment

### Automated Tests

Add a `tests/connectors/test_<platform>_connector.py` file alongside the existing ones — see [tests/connectors/test_slack_connector.py](../tests/connectors/test_slack_connector.py) and [tests/connectors/test_telegram_connector.py](../tests/connectors/test_telegram_connector.py) for the pattern. At minimum, cover:

- `verify_request` — valid signature/token accepted, invalid rejected, replay rejected if applicable.
- `parse_event` — happy-path message and at least one attachment shape, asserted against a real-shaped JSON fixture in [tests/fixtures/<platform>/](../tests/fixtures/).

Drop the JSON fixtures into `tests/fixtures/<platform>/` (a `.gitignore` allowlist already lets them through). Run `pytest tests/connectors/` to verify before opening the PR.

### Local Testing

1. **Set up ngrok** for local webhook testing:
   ```bash
   ngrok http 8000
   ```

2. **Configure webhook** to use ngrok URL:
   ```
   https://your-ngrok-url.ngrok.io/api/v1/myplatform/events
   ```

3. **Run middleware locally**:
   ```bash
   uvicorn app.main:app --reload
   ```

4. **Send test message** to your bot

5. **Check logs** for processing flow

### Production Deployment

1. **Deploy to Cloud Run**:
   ```bash
   bash scripts/deploy_forum.sh
   ```

2. **Update webhook URL** to production URL:
   ```
   https://the-forum-mqwj7cavdq-uc.a.run.app/api/v1/myplatform/events
   ```

3. **Test with real messages**

4. **Monitor Cloud Run logs**:
   ```bash
   gcloud run services logs read the-forum \
     --project vertex-ai-middleware-prod \
     --region us-central1 \
     --limit 50
   ```

### Common Issues

**Issue: 404 Not Found on webhook**
- Solution: Code not deployed. Run `bash scripts/deploy_forum.sh`

**Issue: "Agent config missing bot token"**
- Solution: Check Firestore agent document has correct field names:
  - `myplatform_bot_token_secret` (not `myplatform_bot_token_secre` or `myplatform_token_secret`)
  - `myplatform_bot_token_project_id` (not `myplatform_bot_token_project` or `myplatform_project_id`)

**Issue: "Invalid webhook secret"**
- Solution: Verify webhook secret in Firestore matches the one configured in platform

**Issue: Bot doesn't respond**
- Solution: Check Cloud Run logs for errors
- Verify service account has Secret Manager access
- Verify bot token is correct

## User Identity Linking

### Automatic Identity Creation

When a new user messages the bot, The Forum automatically creates a user record:

```python
{
  "primary_name": "John Doe",
  "email": null,
  "identities": [
    {
      "platform": "myplatform",
      "platform_user_id": "123456789",
      "display_name": "John Doe",
      "linked_at": "2026-04-11T23:11:40.796919Z"
    }
  ]
}
```

### Linking Existing Users

If a user already has an account from another platform (Slack, Google Chat), you need to link their new platform identity to prevent duplicate accounts.

#### Method 1: Automatic Email Linking

If both platforms provide email addresses, The Forum will automatically merge accounts based on matching emails.

#### Method 2: Manual Linking Script

Use `scripts/link_identities.py` to manually link identities:

**Step 1: Find the user's platform ID**

Check Cloud Run logs for the user's first message:
```
Received MyPlatform message from user 123456789: Hello
```

The user ID is `123456789`.

**Step 2: Find the user's Firestore document ID**

```bash
python -c "
from google.cloud import firestore
db = firestore.Client(project='vertex-ai-middleware-prod')
users = db.collection('users').where('email', '==', 'user@example.com').stream()
for user in users:
    print(f'User ID: {user.id}, Name: {user.to_dict().get(\"primary_name\")}')"
```

**Step 3: Check if an auto-created account exists**

```bash
python scripts/link_identities.py \
  --user-id YOUR_USER_ID \
  --platform myplatform \
  --platform-user-id 123456789 \
  --display-name "John Doe"
```

If you get an error saying the identity is already linked to another user, that means an auto-created account exists. You need to migrate the identity.

**Step 4: Migrate identity from auto-created account**

```python
from google.cloud import firestore
from datetime import datetime, timezone

db = firestore.Client(project='vertex-ai-middleware-prod', database='(default)')

# Step 1: Remove identity from auto-created user
auto_user_ref = db.collection('users').document('AUTO_CREATED_USER_ID')
auto_user_ref.update({'identities': []})

# Step 2: Add identity to main user
main_user_ref = db.collection('users').document('MAIN_USER_ID')
main_user_doc = main_user_ref.get()
main_user_data = main_user_doc.to_dict()

new_identity = {
    'platform': 'myplatform',
    'platform_user_id': '123456789',
    'display_name': 'John Doe',
    'linked_at': datetime.now(timezone.utc)
}

existing_identities = main_user_data.get('identities', [])
existing_identities.append(new_identity)

main_user_ref.update({
    'identities': existing_identities,
    'updated_at': firestore.SERVER_TIMESTAMP
})

# Step 3: Delete the empty auto-created user
auto_user_ref.delete()

print('Successfully migrated identity!')
```

**Step 5: Verify**

```bash
python -c "
from google.cloud import firestore
db = firestore.Client(project='vertex-ai-middleware-prod')
doc = db.collection('users').document('MAIN_USER_ID').get()
data = doc.to_dict()
print('Identities:')
for identity in data.get('identities', []):
    print(f'  - {identity[\"platform\"]}: {identity[\"platform_user_id\"]}')"
```

### Cross-Platform Sessions

Once identities are linked, users can:
- Start a conversation on Slack
- Continue it on Google Chat
- Finish it on MyPlatform

All messages are part of the same session, and conversation history is maintained across platforms.

## Reference: Telegram Integration

The Telegram integration (commit `7b49a11`) is a complete reference implementation showing all the steps above:

### Files Created

- `app/services/platforms/telegram_connector.py` (410 lines)
  - Implements TelegramConnector with all 6 required methods
  - Supports both direct token and Secret Manager
  - Handles photos, documents, videos, voice messages
  - Two-step file download (getFile → download)
  - Webhook verification via X-Telegram-Bot-Api-Secret-Token header

- `app/api/v1/telegram_events.py` (154 lines)
  - FastAPI webhook endpoint
  - Filters bot messages and edited messages
  - Finds enabled Telegram agent
  - Verifies webhook secret
  - Processes events in background

- `scripts/link_identities.py` (193 lines)
  - Generalized script supporting all platforms
  - Validates platform names
  - Prevents duplicate identities
  - Checks for conflicts across users

### Files Modified

- `app/models/agent.py`
  - Added Telegram fields to AgentPlatformConfig
  - Added `get_telegram_config()` convenience method

- `app/api/v1/routes.py`
  - Registered `telegram_events.router`

- `README.md`
  - Added "Adding a New Platform" guide
  - Updated feature list

In the **Agent-Template repo** (separate; github.com/Comites-ai/Agent-Template):

- `terraform/main.tf`
  - Added SECTION 4: TELEGRAM-SPECIFIC INFRASTRUCTURE
  - Added terraform outputs with setup instructions

- `terraform/variables.tf`
  - Made descriptions platform-agnostic

- `terraform/terraform.tfvars.example`
  - Added platform-specific notes section

- `register_agent.py`
  - Added `validate_telegram()` and wired it into `build_platforms()`

### Key Metrics

- **Total lines of code**: ~410 (connector) + 154 (route handler) = ~564 lines
- **Core business logic changes**: 0 lines (no changes to MessageProcessorV2)
- **Time to implement**: ~2-3 hours for complete integration
- **Time to deploy and test**: ~30 minutes

### Common Patterns Used

1. **aiohttp for async HTTP**: All API calls use async/await
2. **Secret Manager support**: Both direct token and Secret Manager
3. **Webhook verification**: Constant-time comparison for security
4. **Background processing**: FastAPI BackgroundTasks for async processing
5. **Error handling**: Return 200 OK even on errors (webhook best practice)
6. **Logging**: Comprehensive logging for debugging
7. **Type hints**: Full type annotations for better IDE support

## Reference: Discord Integration (Gateway-based)

Discord is in the codebase but follows a different pattern: it cannot
deliver DMs over HTTP webhooks, so we run a separate worker that holds
the Gateway WebSocket open and forwards events to the Forum. The
connector lives in [app/services/platforms/discord_connector.py](../app/services/platforms/discord_connector.py)
and the route handler in [app/api/v1/discord_events.py](../app/api/v1/discord_events.py),
exactly mirroring the Telegram structure — but authentication is done via
OIDC token (the worker's GCP service account) instead of a shared webhook
secret. See [DISCORD_WORKER.md](DISCORD_WORKER.md) for cost, patching, and
runbook details.

## Future Platform Ideas

Based on this architecture, these platforms could be added with similar effort:

- **WhatsApp Business API** (~400 lines)
- **Microsoft Teams** (~400 lines)
- **LINE** (~300 lines)
- **Facebook Messenger** (~400 lines)
- **SMS (Twilio)** (~200 lines)
- **WeChat** (~500 lines)

Each platform follows the same pattern:
1. Implement PlatformConnector (core logic)
2. Create webhook route handler (boilerplate)
3. Add to agent model (3 fields)
4. Update docs (copy/paste/modify)
5. Deploy and test (standard process)

Total effort: **2-4 hours per platform** including testing and documentation.
