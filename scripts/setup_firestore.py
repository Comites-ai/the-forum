# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

#!/usr/bin/env python3
"""
Firestore initialization script.

Creates Firestore collections and sets up indexes if needed.
"""
import argparse
import sys

from google.cloud import firestore


def setup_firestore(project_id: str) -> bool:
    """
    Initialize Firestore collections.

    Args:
        project_id: GCP project ID

    Returns:
        True if successful, False otherwise
    """
    try:
        client = firestore.Client(project=project_id)

        print(f"Setting up Firestore for project: {project_id}")
        print("=" * 60)

        # Create agents collection (if it doesn't exist)
        print("\n1. Setting up 'agents' collection...")
        agents_ref = client.collection("agents")

        # Add a placeholder document (will be replaced by actual agents)
        # This ensures the collection exists
        test_doc = {
            "_placeholder": True,
            "note": "This is a placeholder. Delete after adding real agents.",
        }
        agents_ref.document("_placeholder").set(test_doc)
        print("✓ Agents collection created")

        # Create sessions collection
        print("\n2. Setting up 'sessions' collection...")
        sessions_ref = client.collection("sessions")

        # Add a placeholder document
        sessions_ref.document("_placeholder").set(test_doc)
        print("✓ Sessions collection created")

        # Create scheduled_jobs collection
        print("\n3. Setting up 'scheduled_jobs' collection...")
        scheduled_jobs_ref = client.collection("scheduled_jobs")

        # Add a placeholder document
        scheduled_jobs_ref.document("_placeholder").set(test_doc)
        print("✓ Scheduled jobs collection created")

        print("\n" + "=" * 60)
        print("✓ Firestore setup complete!")
        print("\nCollections created:")
        print("  - agents")
        print("  - sessions")
        print("  - scheduled_jobs")
        print("\nNote: Delete placeholder documents after adding real data.")
        print("\nNext steps:")
        print("  1. Deploy agents using scripts/deploy_agent.py")
        print(
            '  2. Or manually add agent documents to the "agents" collection'
        )

        return True

    except Exception as e:
        print(f"\n✗ Error setting up Firestore: {e}")
        return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Initialize Firestore collections")
    parser.add_argument(
        "--project-id",
        help="GCP project ID (defaults to env var GCP_PROJECT_ID)",
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

    success = setup_firestore(project_id)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
