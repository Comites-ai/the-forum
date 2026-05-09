# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

#!/usr/bin/env python3
"""Check user identities and help with linking Google Chat and Slack accounts."""
import os
from google.cloud import firestore

os.environ['GCP_PROJECT_ID'] = 'vertex-ai-middleware-prod'

def main():
    db = firestore.Client(project='vertex-ai-middleware-prod', database='(default)')

    # List all users
    users_ref = db.collection('users')
    users = users_ref.stream()

    print("=== All Users ===\n")
    for user_doc in users:
        user_data = user_doc.to_dict()
        print(f"User ID: {user_doc.id}")
        print(f"  Primary Name: {user_data.get('primary_name')}")
        print(f"  Email: {user_data.get('email')}")
        print(f"  Identities:")
        for identity in user_data.get('identities', []):
            platform = identity.get('platform')
            platform_user_id = identity.get('platform_user_id')
            display_name = identity.get('display_name', 'N/A')
            print(f"    - {platform}: {platform_user_id} ({display_name})")
        print()

if __name__ == '__main__':
    main()
