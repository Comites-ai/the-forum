# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Discord Gateway Worker.

Holds a Discord Gateway WebSocket connection open, listens for direct
messages addressed to the bot, normalizes each one to the shape the
Forum's /api/v1/discord/events/{agent_id} endpoint expects, and POSTs it
over HTTPS using a Google-issued OIDC token minted from the VM's service
account.

This is the long-lived counterpart to the Forum's HTTP service. See
docs/DISCORD_WORKER.md for architecture, deploy, and operational notes.

Environment variables:
    FORUM_URL                    Base URL of the Forum service
                                 (e.g. https://the-forum-xxx-uc.a.run.app)
    AGENT_ID                     Firestore agent document ID this worker serves
    DISCORD_BOT_TOKEN_SECRET     Secret Manager secret name holding the bot token
    DISCORD_BOT_TOKEN_PROJECT_ID GCP project ID where that secret lives
    OIDC_AUDIENCE                (Optional) override audience for the OIDC
                                 token. Defaults to FORUM_URL.
    LOG_LEVEL                    (Optional) Python logging level. Default INFO.
"""
import asyncio
import logging
import os
import sys
from typing import Optional

import discord
import google.auth.transport.requests
import google.oauth2.id_token
import requests
from google.cloud import secretmanager


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"FATAL: required env var {name} is unset", file=sys.stderr)
        sys.exit(2)
    return value


FORUM_URL = _require_env("FORUM_URL").rstrip("/")
AGENT_ID = _require_env("AGENT_ID")
BOT_TOKEN_SECRET = _require_env("DISCORD_BOT_TOKEN_SECRET")
BOT_TOKEN_PROJECT_ID = _require_env("DISCORD_BOT_TOKEN_PROJECT_ID")
OIDC_AUDIENCE = os.environ.get("OIDC_AUDIENCE", FORUM_URL)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("discord-worker")


def fetch_bot_token() -> str:
    """Pull the Discord bot token from Secret Manager at startup."""
    client = secretmanager.SecretManagerServiceClient()
    secret_path = (
        f"projects/{BOT_TOKEN_PROJECT_ID}/secrets/{BOT_TOKEN_SECRET}/versions/latest"
    )
    response = client.access_secret_version(request={"name": secret_path})
    return response.payload.data.decode("UTF-8").strip()


def mint_oidc_token() -> str:
    """
    Mint an OIDC identity token for the Forum URL.

    Uses google-auth's metadata server flow when running on GCE/Cloud Run;
    the VM's attached service account is the issuer. Tokens last ~1 hour;
    we mint on each forward to keep the code simple — Discord DMs aren't
    high-volume enough for that to be a problem.
    """
    auth_request = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_request, OIDC_AUDIENCE)


def forward_dm(message: discord.Message) -> None:
    """Forward a Discord DM to the Forum as a normalized event."""
    payload = {
        "event_type": "dm_message",
        "user_id": str(message.author.id),
        "username": message.author.name,
        "global_name": getattr(message.author, "global_name", None),
        "channel_id": str(message.channel.id),
        "message_id": str(message.id),
        "text": message.content or "",
        "attachments": [
            {
                "url": a.url,
                "content_type": a.content_type,
                "filename": a.filename,
                "size": a.size,
            }
            for a in message.attachments
        ],
    }

    try:
        token = mint_oidc_token()
    except Exception:
        logger.exception("Failed to mint OIDC token; dropping event")
        return

    url = f"{FORUM_URL}/api/v1/discord/events/{AGENT_ID}"
    try:
        # 10s connect, 30s read — Forum returns 200 promptly and processes
        # the message in the background. If we hang here we'd block the
        # Gateway event loop.
        response = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=(10, 30),
        )
        if response.status_code >= 400:
            logger.error(
                "Forum rejected DM event: status=%s body=%r",
                response.status_code, response.text[:500],
            )
        else:
            logger.info(
                "Forwarded DM from user_id=%s (msg_id=%s) to forum",
                payload["user_id"], payload["message_id"],
            )
    except requests.RequestException:
        logger.exception("Network error forwarding DM to forum")


# Privileged intent: required so the bot receives message bodies in DMs.
# Toggle this on in the Discord Developer Portal (Bot → Privileged Gateway
# Intents → MESSAGE CONTENT INTENT).
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.dm_messages = True


class WorkerClient(discord.Client):
    async def on_ready(self) -> None:
        logger.info("Connected to Discord as %s (id=%s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        # Ignore our own messages and any other bot's messages.
        if message.author.bot:
            return
        # DMs only, for now. Channel guild_id is None for DMs.
        if message.guild is not None:
            return
        # Run the forward in a thread so the websocket loop stays responsive.
        await asyncio.to_thread(forward_dm, message)


def main() -> None:
    bot_token = fetch_bot_token()
    client = WorkerClient(intents=INTENTS)
    # discord.py manages reconnects internally; if the websocket drops it
    # will retry with exponential backoff. We rely on the container
    # supervisor (systemd or COS) to restart us on hard failure.
    client.run(bot_token, log_handler=None, reconnect=True)


if __name__ == "__main__":
    main()
