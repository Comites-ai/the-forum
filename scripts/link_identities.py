# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

#!/usr/bin/env python3
"""Link platform identities to an existing user.

This script allows you to link identities from any platform (Slack, Google Chat, Telegram, Discord)
to an existing unified user in Firestore.

Usage:
    python scripts/link_identities.py \
        --user-id USER_FIRESTORE_ID \
        --platform PLATFORM_NAME \
        --platform-user-id PLATFORM_USER_ID \
        --display-name "Display Name"

Examples:
    # Link Telegram identity to Jonathan's user
    python scripts/link_identities.py \
        --user-id CXMYwKK1U6IDL7JGJOB3 \
        --platform telegram \
        --platform-user-id 123456789 \
        --display-name "Jonathan"

    # Link Google Chat identity
    python scripts/link_identities.py \
        --user-id CXMYwKK1U6IDL7JGJOB3 \
        --platform google_chat \
        --platform-user-id iQQ0owCMDnMyAcfke4Cp \
        --display-name "Jonathan Cavell"
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from google.cloud import firestore

# Set default project
os.environ['GCP_PROJECT_ID'] = os.environ.get('GCP_PROJECT_ID', 'vertex-ai-middleware-prod')


def link_identity(
    user_id: str,
    platform: str,
    platform_user_id: str,
    display_name: str,
    project_id: str = 'vertex-ai-middleware-prod'
):
    """
    Link a platform identity to an existing user.

    Args:
        user_id: Firestore user document ID
        platform: Platform name (slack, google_chat, telegram, discord)
        platform_user_id: Platform-specific user ID
        display_name: User's display name on this platform
        project_id: GCP project ID for Firestore
    """
    db = firestore.Client(project=project_id, database='(default)')

    # Validate platform
    valid_platforms = ['slack', 'google_chat', 'telegram', 'discord']
    if platform not in valid_platforms:
        print(f"ERROR: Invalid platform '{platform}'. Must be one of: {', '.join(valid_platforms)}")
        sys.exit(1)

    # Get the existing user
    user_ref = db.collection('users').document(user_id)
    user_doc = user_ref.get()

    if not user_doc.exists:
        print(f"ERROR: User {user_id} not found in Firestore")
        sys.exit(1)

    user_data = user_doc.to_dict()

    print("=== Current User State ===")
    print(f"\nUser ID: {user_id}")
    print(f"Primary Name: {user_data.get('primary_name')}")
    print(f"Email: {user_data.get('email')}")
    print(f"Existing Identities:")
    for identity in user_data.get('identities', []):
        print(f"  - {identity.get('platform')}: {identity.get('platform_user_id')} ({identity.get('display_name')})")

    # Check if this platform identity already exists
    existing_identities = user_data.get('identities', [])
    for identity in existing_identities:
        if identity.get('platform') == platform and identity.get('platform_user_id') == platform_user_id:
            print(f"\nWARNING: This {platform} identity ({platform_user_id}) is already linked to this user!")
            response = input("Do you want to update the display name? (y/n): ")
            if response.lower() != 'y':
                print("Aborted.")
                sys.exit(0)

            # Update display name
            identity['display_name'] = display_name
            identity['linked_at'] = datetime.now(timezone.utc)

            user_ref.update({
                'identities': existing_identities,
                'updated_at': firestore.SERVER_TIMESTAMP
            })

            print(f"\n✓ Updated {platform} identity display name to: {display_name}")
            sys.exit(0)

    # Check if this identity is linked to a different user
    users = db.collection('users').stream()
    for other_user_doc in users:
        if other_user_doc.id == user_id:
            continue

        other_user_data = other_user_doc.to_dict()
        for identity in other_user_data.get('identities', []):
            if identity.get('platform') == platform and identity.get('platform_user_id') == platform_user_id:
                print(f"\nERROR: This {platform} identity ({platform_user_id}) is already linked to user:")
                print(f"  User ID: {other_user_doc.id}")
                print(f"  Name: {other_user_data.get('primary_name')}")
                print(f"  Email: {other_user_data.get('email')}")
                print(f"\nYou must first unlink it from that user before linking it to {user_id}")
                sys.exit(1)

    # Add the new identity
    new_identity = {
        'platform': platform,
        'platform_user_id': platform_user_id,
        'display_name': display_name,
        'linked_at': datetime.now(timezone.utc)
    }

    existing_identities.append(new_identity)

    # Update user
    user_ref.update({
        'identities': existing_identities,
        'updated_at': firestore.SERVER_TIMESTAMP
    })

    print(f"\n=== Identity Linked Successfully ===")
    print(f"Added {platform} identity to user {user_id}")
    print(f"  Platform User ID: {platform_user_id}")
    print(f"  Display Name: {display_name}")
    print(f"\nUser now has {len(existing_identities)} platform identit{'y' if len(existing_identities) == 1 else 'ies'}:")
    for identity in existing_identities:
        print(f"  - {identity.get('platform')}: {identity.get('platform_user_id')} ({identity.get('display_name')})")

    print(f"\n✓ User {user_data.get('primary_name')} can now use this agent via {platform}!")


def main():
    parser = argparse.ArgumentParser(
        description='Link a platform identity to an existing user',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        '--user-id',
        required=True,
        help='Firestore user document ID (e.g., CXMYwKK1U6IDL7JGJOB3)'
    )
    parser.add_argument(
        '--platform',
        required=True,
        choices=['slack', 'google_chat', 'telegram', 'discord'],
        help='Platform name'
    )
    parser.add_argument(
        '--platform-user-id',
        required=True,
        help='Platform-specific user ID'
    )
    parser.add_argument(
        '--display-name',
        required=True,
        help='User\'s display name on this platform'
    )
    parser.add_argument(
        '--project-id',
        default='vertex-ai-middleware-prod',
        help='GCP project ID (default: vertex-ai-middleware-prod)'
    )

    args = parser.parse_args()

    link_identity(
        user_id=args.user_id,
        platform=args.platform,
        platform_user_id=args.platform_user_id,
        display_name=args.display_name,
        project_id=args.project_id
    )


if __name__ == '__main__':
    main()
