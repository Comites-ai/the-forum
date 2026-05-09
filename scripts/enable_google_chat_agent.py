# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

#!/usr/bin/env python3
"""Enable Google Chat for an agent."""
import argparse
import os
from google.cloud import firestore

def main():
    parser = argparse.ArgumentParser(description='Enable Google Chat for an agent')
    parser.add_argument('--project', required=True, help='GCP project ID')
    parser.add_argument('--agent-id', required=True, help='Agent ID from Firestore')
    parser.add_argument('--secret-name', required=True, help='Secret Manager secret name for service account credentials')
    parser.add_argument('--google-chat-project-id', help='Google Chat project ID (if different from middleware project)')
    args = parser.parse_args()

    os.environ['GCP_PROJECT_ID'] = args.project

    db = firestore.Client(project=args.project, database='(default)')

    # Get the agent
    agent_ref = db.collection('agents').document(args.agent_id)
    agent = agent_ref.get()

    if not agent.exists:
        print(f"ERROR: Agent {args.agent_id} not found")
        return 1

    data = agent.to_dict()
    print(f"Current agent config: {data.get('name', args.agent_id)}")

    # Add or update platforms array with Google Chat
    platforms = data.get('platforms', [])

    # Check if Google Chat platform already exists
    google_chat_exists = False
    for i, platform in enumerate(platforms):
        if platform.get('platform') == 'google_chat':
            platforms[i]['enabled'] = True
            platforms[i]['google_chat_service_account_secret'] = args.secret_name
            if args.google_chat_project_id:
                platforms[i]['google_chat_project_id'] = args.google_chat_project_id
            google_chat_exists = True
            print("Updated existing Google Chat platform config")
            break

    # If Google Chat platform doesn't exist, add it
    if not google_chat_exists:
        config = {
            'platform': 'google_chat',
            'enabled': True,
            'google_chat_service_account_secret': args.secret_name
        }
        if args.google_chat_project_id:
            config['google_chat_project_id'] = args.google_chat_project_id
        platforms.append(config)
        print("Added new Google Chat platform config")

    # Update the agent
    agent_ref.update({'platforms': platforms})

    print(f"\n✓ Successfully enabled Google Chat for agent {args.agent_id}")
    print(f"  Secret: {args.secret_name}")
    if args.google_chat_project_id:
        print(f"  Google Chat Project ID: {args.google_chat_project_id}")
    print(f"  Platforms: {platforms}")
    
    return 0

if __name__ == '__main__':
    exit(main())
