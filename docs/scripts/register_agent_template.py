#!/usr/bin/env python3
"""
Register agent in Firestore with multi-platform configuration.

This script registers an agent with the middleware's Firestore database.
Copy this to your agent repository and customize the configuration section.
"""
from google.cloud import firestore
from datetime import datetime, UTC

# ==============================================================================
# CONFIGURATION - Update these values for your agent
# ==============================================================================
PROJECT_ID = "vertex-ai-middleware-prod"  # Middleware project ID
AGENT_NAME = "My Agent"  # Display name for your agent
VERTEX_AI_AGENT_ID = "projects/YOUR_PROJECT/locations/us-central1/reasoningEngines/YOUR_ENGINE_ID"
SLACK_BOT_ID = "U0XXXXXXXXX"  # Get from: curl -s https://slack.com/api/auth.test -H "Authorization: Bearer xoxb-..." | jq .user_id

# Platform configurations
# Slack platform - uncomment and configure if using Slack
SLACK_PLATFORM = {
    "platform": "slack",
    "enabled": True,
    "slack_bot_id": SLACK_BOT_ID,
    "slack_bot_token_secret": "my-agent-slack-token",  # Secret name in your agent's project
    "slack_bot_token_project_id": "my-agent-prod"  # Your agent's GCP project ID
}

# Google Chat platform - uncomment and configure if using Google Chat
# GOOGLE_CHAT_PLATFORM = {
#     "platform": "google_chat",
#     "enabled": True,
#     "google_chat_service_account_secret": "my-agent-credentials",  # Secret name in your agent's project
#     "google_chat_project_id": "my-agent-prod"  # Your agent's GCP project ID
# }

# Telegram platform - uncomment and configure if using Telegram
# TELEGRAM_PLATFORM = {
#     "platform": "telegram",
#     "enabled": True,
#     "telegram_bot_token_secret": "my-agent-telegram-token",  # Secret name in your agent's project
#     "telegram_bot_token_project_id": "my-agent-prod",  # Your agent's GCP project ID
#     "telegram_webhook_secret": "YOUR_WEBHOOK_SECRET"  # From: openssl rand -base64 32
# }

# ==============================================================================
# Registration Logic - No changes needed below this line
# ==============================================================================

def register_agent():
    """Register the agent in Firestore."""
    db = firestore.Client(project=PROJECT_ID)

    # Build platforms list (only include enabled platforms)
    platforms = []

    # Add Slack if configured
    if 'SLACK_PLATFORM' in globals():
        platforms.append(SLACK_PLATFORM)

    # Add Google Chat if configured
    if 'GOOGLE_CHAT_PLATFORM' in globals():
        platforms.append(GOOGLE_CHAT_PLATFORM)

    # Add Telegram if configured
    if 'TELEGRAM_PLATFORM' in globals():
        platforms.append(TELEGRAM_PLATFORM)

    # Check if agent already exists
    query = db.collection("agents").where("display_name", "==", AGENT_NAME).limit(1)
    existing = list(query.stream())

    agent_data = {
        "display_name": AGENT_NAME,
        "vertex_ai_agent_id": VERTEX_AI_AGENT_ID,
        "platforms": platforms,
        "updated_at": datetime.now(UTC)
    }

    # Legacy fields - middleware still uses these for backward compatibility
    # Only add if Slack platform is configured
    if 'SLACK_PLATFORM' in globals():
        agent_data["slack_bot_id"] = SLACK_BOT_ID

    if existing:
        # Update existing agent
        doc_id = existing[0].id
        db.collection("agents").document(doc_id).update(agent_data)
        print(f"[OK] Updated existing agent: {doc_id}")
    else:
        # Create new agent
        agent_data["created_at"] = datetime.now(UTC)
        doc_ref = db.collection("agents").add(agent_data)
        doc_id = doc_ref[1].id
        print(f"[OK] Created new agent: {doc_id}")

    print(f"\nAgent Details:")
    print(f"  Name: {AGENT_NAME}")
    print(f"  Firestore ID: {doc_id}")
    print(f"  Vertex AI Agent: {VERTEX_AI_AGENT_ID}")

    print(f"\nPlatforms enabled:")
    for platform in platforms:
        platform_name = platform["platform"].replace("_", " ").title()
        print(f"  [X] {platform_name}")

        if platform["platform"] == "slack":
            print(f"      Bot ID: {platform['slack_bot_id']}")
            print(f"      Token Secret: {platform['slack_bot_token_secret']} (in {platform['slack_bot_token_project_id']})")
        elif platform["platform"] == "google_chat":
            print(f"      Credentials Secret: {platform['google_chat_service_account_secret']} (in {platform['google_chat_project_id']})")
        elif platform["platform"] == "telegram":
            print(f"      Token Secret: {platform['telegram_bot_token_secret']} (in {platform['telegram_bot_token_project_id']})")

    print(f"\nNext steps:")
    if 'SLACK_PLATFORM' in globals():
        print(f"  1. Configure Slack Event Subscriptions")
        print(f"  2. Test by sending a DM to your bot in Slack")
    if 'GOOGLE_CHAT_PLATFORM' in globals():
        print(f"  1. Configure Google Chat bot settings in Console")
        print(f"  2. Test by messaging your bot in Google Chat")
    if 'TELEGRAM_PLATFORM' in globals():
        print(f"  1. Set Telegram webhook")
        print(f"  2. Test by messaging your bot in Telegram")

if __name__ == "__main__":
    register_agent()
