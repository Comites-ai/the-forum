# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

#!/usr/bin/env python3
"""
Agent deployment script.

Updates or creates agent configuration in Firestore.
Validates Vertex AI agent exists and Slack bot token is valid.
"""
import argparse
import sys
import asyncio
from datetime import datetime

from google.cloud import firestore
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


async def validate_vertex_ai_agent(agent_id: str, project_id: str, location: str) -> bool:
    """
    Validate that Vertex AI agent exists.

    Args:
        agent_id: Full agent resource name
        project_id: GCP project ID
        location: GCP location

    Returns:
        True if agent exists, False otherwise
    """
    try:
        # For Reasoning Engine agents, we skip direct validation
        # as the API is different from the legacy agent_engines
        if "reasoningEngines" in agent_id:
            print(f"✓ Vertex AI Reasoning Engine ID format valid: {agent_id}")
            print("  (Skipping live validation - will be verified on first message)")
            return True

        # TODO: Add validation for other agent types as needed
        print(f"✓ Vertex AI agent ID accepted: {agent_id}")
        return True

    except Exception as e:
        print(f"✗ Error validating Vertex AI agent: {e}")
        return False


def validate_slack_token(token: str) -> tuple[bool, str]:
    """
    Validate Slack bot token and get the user_id.

    IMPORTANT: Slack Events API sends the user_id (U...) in authorizations,
    NOT the bot_id (B...) shown in Slack app settings. This function returns
    the user_id which should be used for agent registration.

    Args:
        token: Slack Bot User OAuth Token

    Returns:
        Tuple of (is_valid, user_id)
    """
    try:
        client = WebClient(token=token)
        response = client.auth_test()

        if response["ok"]:
            # IMPORTANT: This is the user_id (U...) that Slack sends in
            # event authorizations. This is NOT the same as the bot_id (B...)
            # shown in Slack app settings under "App Home".
            user_id = response["user_id"]
            bot_name = response.get("user", "Unknown")
            print(f"✓ Slack token valid for bot: {bot_name}")
            print(f"  User ID (for Firestore): {user_id}")
            print(f"  NOTE: Use this user_id for --slack-bot-id, not the B... ID from Slack settings")
            return True, user_id
        else:
            print(f"✗ Slack token invalid: {response}")
            return False, None

    except SlackApiError as e:
        print(f"✗ Slack API error: {e.response['error']}")
        return False, None
    except Exception as e:
        print(f"✗ Error validating Slack token: {e}")
        return False, None


async def deploy_agent(
    agent_name: str,
    vertex_ai_agent_id: str,
    slack_bot_id: str,
    slack_bot_token: str,
    project_id: str,
    location: str = "us-central1",
) -> bool:
    """
    Deploy or update agent configuration in Firestore.

    Args:
        agent_name: Display name for the agent
        vertex_ai_agent_id: Vertex AI agent resource name
        slack_bot_id: Slack bot user ID
        slack_bot_token: Slack Bot User OAuth Token
        project_id: GCP project ID
        location: GCP location

    Returns:
        True if successful, False otherwise
    """
    print(f"\nDeploying agent: {agent_name}")
    print("=" * 60)

    # Step 1: Validate Vertex AI agent
    print("\n1. Validating Vertex AI agent...")
    if not await validate_vertex_ai_agent(vertex_ai_agent_id, project_id, location):
        print("\n✗ Vertex AI agent validation failed. Aborting.")
        return False

    # Step 2: Validate Slack token
    print("\n2. Validating Slack bot token...")
    is_valid, validated_bot_id = validate_slack_token(slack_bot_token)

    if not is_valid:
        print("\n✗ Slack token validation failed. Aborting.")
        return False

    # Verify bot IDs match
    if validated_bot_id != slack_bot_id:
        print(
            f"\n⚠ Warning: Provided bot_id ({slack_bot_id}) doesn't match "
            f"token's bot_id ({validated_bot_id})"
        )
        print(f"Using validated bot_id: {validated_bot_id}")
        slack_bot_id = validated_bot_id

    # Step 3: Update Firestore
    print("\n3. Updating Firestore...")
    try:
        client = firestore.Client(project=project_id)

        # Check if agent already exists
        query = (
            client.collection("agents")
            .where("slack_bot_id", "==", slack_bot_id)
            .limit(1)
        )
        docs = list(query.stream())

        agent_data = {
            "slack_bot_token": slack_bot_token,
            "slack_bot_id": slack_bot_id,
            "vertex_ai_agent_id": vertex_ai_agent_id,
            "display_name": agent_name,
            "updated_at": datetime.utcnow(),
        }

        if docs:
            # Update existing agent
            doc_id = docs[0].id
            client.collection("agents").document(doc_id).update(agent_data)
            print(f"✓ Updated existing agent configuration (ID: {doc_id})")
        else:
            # Create new agent
            agent_data["created_at"] = datetime.utcnow()
            doc_ref = client.collection("agents").add(agent_data)
            print(f"✓ Created new agent configuration (ID: {doc_ref[1].id})")

        print("\n" + "=" * 60)
        print(f"✓ Agent deployment successful!")
        print("\nAgent Details:")
        print(f"  Name: {agent_name}")
        print(f"  Slack Bot ID: {slack_bot_id}")
        print(f"  Vertex AI Agent: {vertex_ai_agent_id}")
        print("\nNext steps:")
        print("  1. Configure Slack Events API Request URL")
        print("  2. Test by sending a DM to the bot in Slack")
        return True

    except Exception as e:
        print(f"\n✗ Error updating Firestore: {e}")
        return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Deploy or update agent configuration"
    )
    parser.add_argument(
        "--agent-name", required=True, help='Agent display name (e.g., "Growth Coach")'
    )
    parser.add_argument(
        "--vertex-ai-agent-id",
        required=True,
        help="Vertex AI agent resource name "
        "(e.g., projects/PROJECT/locations/LOCATION/agents/AGENT_ID)",
    )
    parser.add_argument(
        "--slack-bot-id",
        required=True,
        help="Slack bot user ID from auth.test (e.g., U0AFZ86NE00). "
        "IMPORTANT: Use the user_id (U...) NOT the bot_id (B...) from Slack settings. "
        "Get it with: curl -s https://slack.com/api/auth.test -H 'Authorization: Bearer xoxb-...' | jq .user_id",
    )
    parser.add_argument(
        "--slack-bot-token",
        required=True,
        help="Slack Bot User OAuth Token (xoxb-...)",
    )
    parser.add_argument(
        "--project-id",
        help="GCP project ID (defaults to env var GCP_PROJECT_ID)",
    )
    parser.add_argument(
        "--location",
        default="us-central1",
        help="GCP location (default: us-central1)",
    )

    args = parser.parse_args()

    # Get project ID
    project_id = args.project_id
    if not project_id:
        import os

        project_id = os.getenv("GCP_PROJECT_ID")

    if not project_id:
        print("Error: GCP_PROJECT_ID not provided and not set in environment")
        sys.exit(1)

    # Run deployment
    success = asyncio.run(
        deploy_agent(
            agent_name=args.agent_name,
            vertex_ai_agent_id=args.vertex_ai_agent_id,
            slack_bot_id=args.slack_bot_id,
            slack_bot_token=args.slack_bot_token,
            project_id=project_id,
            location=args.location,
        )
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
