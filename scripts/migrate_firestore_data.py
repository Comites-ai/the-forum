# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""
Firestore Data Migration Script

Updates agent documents after migration to new GCP project.
Updates vertex_ai_agent_id fields with new agent resource names.
"""

import argparse
import sys
from google.cloud import firestore

def update_agent(
    project_id: str,
    agent_id: str,
    new_vertex_ai_agent_id: str,
    new_google_chat_bot_name: str = None
):
    """
    Update an agent document with new project-specific values.

    Args:
        project_id: New GCP project ID
        agent_id: Firestore document ID of the agent
        new_vertex_ai_agent_id: New Vertex AI agent resource name
        new_google_chat_bot_name: New Google Chat bot resource name (optional)
    """
    db = firestore.Client(project=project_id)
    agent_ref = db.collection('agents').document(agent_id)

    # Get current agent data
    agent_doc = agent_ref.get()
    if not agent_doc.exists:
        print(f"❌ Agent {agent_id} not found!")
        return False

    agent_data = agent_doc.to_dict()
    print(f"\n📝 Updating agent: {agent_data.get('display_name', agent_id)}")

    # Update Vertex AI agent ID
    old_agent_id = agent_data.get('vertex_ai_agent_id')
    print(f"   Old Vertex AI Agent ID: {old_agent_id}")
    print(f"   New Vertex AI Agent ID: {new_vertex_ai_agent_id}")

    updates = {
        'vertex_ai_agent_id': new_vertex_ai_agent_id
    }

    # Update Google Chat bot name if provided
    if new_google_chat_bot_name:
        platforms = agent_data.get('platforms', [])
        for i, platform in enumerate(platforms):
            if platform.get('platform') == 'google_chat':
                old_bot_name = platform.get('google_chat_bot_name')
                print(f"   Old Google Chat Bot: {old_bot_name}")
                print(f"   New Google Chat Bot: {new_google_chat_bot_name}")

                platform['google_chat_bot_name'] = new_google_chat_bot_name
                updates['platforms'] = platforms
                break

    # Apply updates
    agent_ref.update(updates)
    print(f"✅ Updated agent {agent_id}")
    return True


def list_agents(project_id: str):
    """List all agents in Firestore."""
    db = firestore.Client(project=project_id)
    agents_ref = db.collection('agents')

    print(f"\n📋 Agents in project {project_id}:\n")
    for doc in agents_ref.stream():
        agent_data = doc.to_dict()
        print(f"ID: {doc.id}")
        print(f"   Name: {agent_data.get('display_name')}")
        print(f"   Vertex AI Agent: {agent_data.get('vertex_ai_agent_id')}")

        platforms = agent_data.get('platforms', [])
        for platform in platforms:
            if platform.get('platform') == 'google_chat':
                print(f"   Google Chat Bot: {platform.get('google_chat_bot_name')}")
        print()


def main():
    parser = argparse.ArgumentParser(
        description='Migrate Firestore data after GCP project migration'
    )
    parser.add_argument(
        '--project-id',
        required=True,
        help='New GCP project ID'
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # List command
    subparsers.add_parser('list', help='List all agents')

    # Update command
    update_parser = subparsers.add_parser('update', help='Update an agent')
    update_parser.add_argument(
        '--agent-id',
        required=True,
        help='Firestore document ID of the agent'
    )
    update_parser.add_argument(
        '--vertex-ai-agent-id',
        required=True,
        help='New Vertex AI agent resource name (e.g., projects/PROJECT/locations/LOCATION/reasoningEngines/ID)'
    )
    update_parser.add_argument(
        '--google-chat-bot-name',
        help='New Google Chat bot resource name (e.g., projects/PROJECT/bots/BOT_ID)'
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == 'list':
        list_agents(args.project_id)

    elif args.command == 'update':
        success = update_agent(
            project_id=args.project_id,
            agent_id=args.agent_id,
            new_vertex_ai_agent_id=args.vertex_ai_agent_id,
            new_google_chat_bot_name=args.google_chat_bot_name
        )
        sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
