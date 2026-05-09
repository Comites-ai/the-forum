# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

#!/usr/bin/env python3
"""Link Slack and Google Chat accounts for the same user."""
import os
from google.cloud import firestore

os.environ['GCP_PROJECT_ID'] = 'vertex-ai-middleware-prod'

def main():
    db = firestore.Client(project='vertex-ai-middleware-prod', database='(default)')

    # The primary user (Slack user)
    slack_user_id = "CXMYwKK1U6IDL7JGJOB3"

    # The Google Chat user to merge
    google_chat_user_id = "iQQ0owCMDnMyAcfke4Cp"

    # Get both users
    slack_user_ref = db.collection('users').document(slack_user_id)
    google_chat_user_ref = db.collection('users').document(google_chat_user_id)

    slack_user = slack_user_ref.get()
    google_chat_user = google_chat_user_ref.get()

    if not slack_user.exists:
        print(f"ERROR: Slack user {slack_user_id} not found")
        return

    if not google_chat_user.exists:
        print(f"ERROR: Google Chat user {google_chat_user_id} not found")
        return

    slack_data = slack_user.to_dict()
    google_chat_data = google_chat_user.to_dict()

    print("=== Current State ===")
    print(f"\nSlack User ({slack_user_id}):")
    print(f"  Name: {slack_data.get('primary_name')}")
    print(f"  Email: {slack_data.get('email')}")
    print(f"  Identities: {slack_data.get('identities')}")

    print(f"\nGoogle Chat User ({google_chat_user_id}):")
    print(f"  Name: {google_chat_data.get('primary_name')}")
    print(f"  Email: {google_chat_data.get('email')}")
    print(f"  Identities: {google_chat_data.get('identities')}")

    # Merge: Add Google Chat identity to Slack user
    slack_identities = slack_data.get('identities', [])
    google_chat_identities = google_chat_data.get('identities', [])

    # Add the Google Chat identity
    for identity in google_chat_identities:
        if identity.get('platform') == 'google_chat':
            slack_identities.append(identity)
            break

    # Update Slack user with email and merged identities
    updates = {
        'identities': slack_identities,
        'email': google_chat_data.get('email'),  # Add email from Google Chat user
        'updated_at': firestore.SERVER_TIMESTAMP
    }

    slack_user_ref.update(updates)
    print(f"\n✓ Merged Google Chat identity into Slack user {slack_user_id}")
    print(f"  Added email: {google_chat_data.get('email')}")
    print(f"  Updated identities: {slack_identities}")

    # Delete the Google Chat-only user
    google_chat_user_ref.delete()
    print(f"\n✓ Deleted duplicate Google Chat user {google_chat_user_id}")

    print("\n=== Merge Complete ===")
    print(f"User {slack_user_id} now has both Slack and Google Chat identities")
    print("You should now be recognized as the same user across both platforms!")

if __name__ == '__main__':
    main()
