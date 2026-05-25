# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

#!/usr/bin/env python3
"""Provision a scheduler MCP API key for an agent.

Generates a fresh random API key, writes its SHA-256 hash to the agent's
Firestore document under `scheduler_api_key_hash`, and prints the plaintext
key ONCE so the operator can vault it. The plaintext is never written
anywhere — the operator is responsible for storing it (e.g. Secret Manager
in the agent's project) so the agent can read it at runtime.

Re-running for the same agent rotates the key: the new hash overwrites the
old one in Firestore. Old plaintext keys stop working immediately.

Usage:
  python scripts/provision_scheduler_api_key.py --agent-id <FIRESTORE_DOC_ID>
  python scripts/provision_scheduler_api_key.py --agent-id <ID> --project-id my-proj
"""
import argparse
import hashlib
import os
import secrets
import sys

from google.cloud import firestore


def hash_api_key(plaintext: str) -> str:
    """Match scheduler_mcp.hash_api_key — must stay in sync."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--agent-id",
        required=True,
        help="Firestore document ID of the agent (e.g. 'UneGKRAUpYvrjqzAHui9')",
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("GCP_PROJECT_ID"),
        help="The Forum's GCP project ID. Defaults to the GCP_PROJECT_ID env var.",
    )
    args = parser.parse_args()

    if not args.project_id:
        parser.error("--project-id is required (or set the GCP_PROJECT_ID env var)")

    db = firestore.Client(project=args.project_id, database="(default)")
    agent_ref = db.collection("agents").document(args.agent_id)
    agent_doc = agent_ref.get()
    if not agent_doc.exists:
        print(f"ERROR: Agent {args.agent_id!r} not found in {args.project_id}", file=sys.stderr)
        return 1

    agent_data = agent_doc.to_dict() or {}
    display_name = agent_data.get("display_name", "(no display_name)")
    rotating = bool(agent_data.get("scheduler_api_key_hash"))

    # 32 bytes of entropy, urlsafe-base64 encoded → ~43 char token
    plaintext = secrets.token_urlsafe(32)
    key_hash = hash_api_key(plaintext)

    agent_ref.update({"scheduler_api_key_hash": key_hash})

    print()
    print("=" * 70)
    print(f"  {'ROTATED' if rotating else 'PROVISIONED'} scheduler MCP API key")
    print("=" * 70)
    print(f"  Agent:       {display_name} ({args.agent_id})")
    print(f"  Project:     {args.project_id}")
    print(f"  Hash stored: {key_hash}")
    print()
    print("  Plaintext key (shown ONCE — vault it now):")
    print()
    print(f"      {plaintext}")
    print()
    print("  Next steps for the agent operator:")
    print("    1. Store the plaintext in your agent's Secret Manager, e.g.:")
    print("         echo -n '<KEY>' | gcloud secrets versions add scheduler-mcp-key \\")
    print("           --data-file=- --project=YOUR_AGENT_PROJECT")
    print("    2. Have your agent fetch it at startup and send as 'X-API-Key'")
    print("       on every request to /api/v1/mcp/scheduler.")
    if rotating:
        print()
        print("  NOTE: a previous key existed for this agent and is now invalid.")
    print("=" * 70)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
