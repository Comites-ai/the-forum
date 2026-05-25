# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

#!/usr/bin/env python3
"""Check user identities and help with linking Google Chat and Slack accounts.

Reads from the Forum's Firestore. Set the GCP_PROJECT_ID env var or pass
--project-id to point at your Forum project.
"""
import argparse
import os
import sys

from google.cloud import firestore


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        '--project-id',
        default=os.environ.get('GCP_PROJECT_ID'),
        help='The Forum GCP project ID. Defaults to the GCP_PROJECT_ID env var.'
    )
    args = parser.parse_args()
    if not args.project_id:
        parser.error('--project-id is required (or set the GCP_PROJECT_ID env var)')

    db = firestore.Client(project=args.project_id, database='(default)')

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
