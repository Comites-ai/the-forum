# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Discord Gateway Worker — multi-tenant.

Holds one Discord Gateway WebSocket connection per Discord-enabled agent,
all in a single Python process. For each DM that arrives, normalizes it
to the shape the Forum's /api/v1/discord/events/{agent_id} endpoint
expects, and POSTs it over HTTPS using a Google-issued OIDC token minted
from the VM's service account.

Agents are discovered from Firestore (the same `agents` collection the
Forum uses) by looking for documents whose `platforms` array contains an
enabled `discord` entry. The worker reconciles its set of live Gateway
connections against that list on a periodic interval — adding new bots,
dropping deconfigured ones, and restarting clients whose tokens have
rotated.

See docs/DISCORD_WORKER.md for architecture, cost guidance, and the
operational runbook.

Environment variables:
    FORUM_URL                     Base URL of the Forum service
                                  (e.g. https://the-forum-xxx-uc.a.run.app)
    FIRESTORE_PROJECT_ID          GCP project ID holding the agents collection.
                                  Typically the Forum's project.
    OIDC_AUDIENCE                 (Optional) override audience for the OIDC
                                  token. Defaults to FORUM_URL.
    AGENT_REFRESH_INTERVAL_SECONDS (Optional) how often to re-sync agent
                                  list from Firestore. Default 300 (5 min).
    LOG_LEVEL                     (Optional) Python logging level. Default INFO.
"""
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Optional

import discord
import google.auth.transport.requests
import google.oauth2.id_token
import requests
from google.cloud import firestore, secretmanager


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"FATAL: required env var {name} is unset", file=sys.stderr)
        sys.exit(2)
    return value


FORUM_URL = _require_env("FORUM_URL").rstrip("/")
FIRESTORE_PROJECT_ID = _require_env("FIRESTORE_PROJECT_ID")
OIDC_AUDIENCE = os.environ.get("OIDC_AUDIENCE", FORUM_URL)
REFRESH_INTERVAL_S = int(os.environ.get("AGENT_REFRESH_INTERVAL_SECONDS", "300"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("discord-worker")

# Privileged intent: required so the bot receives message bodies in DMs.
# Toggle this on in the Discord Developer Portal (Bot → Privileged Gateway
# Intents → MESSAGE CONTENT INTENT) for every Discord application this
# worker connects on behalf of.
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.dm_messages = True


@dataclass(frozen=True)
class AgentBotConfig:
    """The slice of an agent's Firestore doc we need to run its bot."""
    agent_id: str
    bot_token_secret: str
    bot_token_project_id: str
    # We capture the token itself so reconcile() can detect rotations by
    # comparing values across refreshes. Sensitive — do not log.
    bot_token: str


def fetch_discord_agents() -> list[AgentBotConfig]:
    """
    Query Firestore for agents that currently have Discord enabled, and
    return one AgentBotConfig per agent.

    A Firestore query on nested array fields is awkward, so we stream the
    full agents collection and filter in Python. Agent counts are small
    (dozens at most) — this is fine.
    """
    db = firestore.Client(project=FIRESTORE_PROJECT_ID)
    out: list[AgentBotConfig] = []
    sm_client = secretmanager.SecretManagerServiceClient()

    for doc in db.collection("agents").stream():
        data = doc.to_dict() or {}
        platforms = data.get("platforms") or []
        for p in platforms:
            if p.get("platform") != "discord" or not p.get("enabled"):
                continue
            secret_name = p.get("discord_bot_token_secret")
            project_id = p.get("discord_bot_token_project_id")
            if not secret_name or not project_id:
                logger.warning(
                    "Agent %s has discord enabled but missing "
                    "discord_bot_token_secret or discord_bot_token_project_id",
                    doc.id,
                )
                continue
            try:
                resource = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
                response = sm_client.access_secret_version(request={"name": resource})
                token = response.payload.data.decode("UTF-8").strip()
            except Exception:
                logger.exception(
                    "Failed to fetch bot token for agent %s (secret=%s in %s)",
                    doc.id, secret_name, project_id,
                )
                continue
            out.append(AgentBotConfig(
                agent_id=doc.id,
                bot_token_secret=secret_name,
                bot_token_project_id=project_id,
                bot_token=token,
            ))
            break  # one discord platform per agent
    return out


def mint_oidc_token() -> str:
    """Mint an OIDC identity token for the Forum URL.

    Uses google-auth's metadata server flow when running on GCE; the VM's
    attached service account is the issuer. Tokens last ~1 hour; we mint
    on each forward to keep the code simple — Discord DMs aren't high
    enough volume for that to matter.
    """
    auth_request = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_request, OIDC_AUDIENCE)


def forward_dm(agent_id: str, message: discord.Message) -> None:
    """Forward a single DM to the Forum's /events/{agent_id} endpoint."""
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

    url = f"{FORUM_URL}/api/v1/discord/events/{agent_id}"
    try:
        # 10s connect, 30s read. The Forum returns 200 promptly and
        # processes the message in the background — if we hang here
        # we block the Gateway event loop.
        response = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=(10, 30),
        )
        if response.status_code >= 400:
            logger.error(
                "Forum rejected DM event for agent %s: status=%s body=%r",
                agent_id, response.status_code, response.text[:500],
            )
        else:
            logger.info(
                "Forwarded DM agent=%s user_id=%s msg_id=%s",
                agent_id, payload["user_id"], payload["message_id"],
            )
    except requests.RequestException:
        logger.exception("Network error forwarding DM for agent %s", agent_id)


class AgentDiscordClient(discord.Client):
    """One Discord client per agent. Forwards DMs to the agent's endpoint."""

    def __init__(self, agent_id: str, **kwargs):
        super().__init__(**kwargs)
        self.agent_id = agent_id

    async def on_ready(self) -> None:
        logger.info(
            "Agent %s online as %s (id=%s)",
            self.agent_id, self.user, getattr(self.user, "id", "?"),
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is not None:
            return  # DMs only — guild messages aren't part of this MVP
        # Run the forward in a thread so the websocket loop stays responsive.
        await asyncio.to_thread(forward_dm, self.agent_id, message)


@dataclass
class RunningClient:
    agent: AgentBotConfig
    client: discord.Client
    task: asyncio.Task


async def stop_client(running: RunningClient) -> None:
    """Cleanly close a Gateway connection. Best-effort — log and move on."""
    try:
        await running.client.close()
    except Exception:
        logger.exception("Error closing client for agent %s", running.agent.agent_id)
    try:
        await asyncio.wait_for(running.task, timeout=10)
    except (asyncio.TimeoutError, Exception):
        pass


async def start_client(agent: AgentBotConfig) -> RunningClient:
    """Open a Gateway connection for one agent."""
    client = AgentDiscordClient(agent_id=agent.agent_id, intents=INTENTS)
    # discord.py's Client.start() blocks until the client disconnects.
    # We schedule it as a background task so the main loop can keep going.
    task = asyncio.create_task(
        client.start(agent.bot_token, reconnect=True),
        name=f"discord-agent-{agent.agent_id}",
    )
    return RunningClient(agent=agent, client=client, task=task)


async def reconcile_loop() -> None:
    """Periodically fetch the desired set of bots from Firestore and
    adjust the running set to match.

    Add: agents that newly appear or whose tokens have rotated.
    Remove: agents that disappear or get disabled.
    Stop: clients whose underlying task has died (so the next pass restarts them).
    """
    running: dict[str, RunningClient] = {}

    while True:
        try:
            desired = {a.agent_id: a for a in fetch_discord_agents()}
        except Exception:
            logger.exception("Failed to fetch agents from Firestore; keeping current set")
            desired = None  # signal: don't reconcile this round

        if desired is not None:
            # Drop dead tasks so we can restart them.
            for agent_id, rc in list(running.items()):
                if rc.task.done():
                    logger.warning(
                        "Client task for agent %s exited (%s); will restart",
                        agent_id, rc.task.exception() if not rc.task.cancelled() else "cancelled",
                    )
                    del running[agent_id]

            # Stop bots that are no longer desired or whose token rotated.
            for agent_id in list(running.keys()):
                if agent_id not in desired:
                    logger.info("Stopping bot for agent %s (no longer enabled)", agent_id)
                    await stop_client(running.pop(agent_id))
                elif desired[agent_id].bot_token != running[agent_id].agent.bot_token:
                    logger.info("Restarting bot for agent %s (token rotated)", agent_id)
                    await stop_client(running.pop(agent_id))

            # Start bots that should be running but aren't.
            for agent_id, agent in desired.items():
                if agent_id not in running:
                    logger.info("Starting bot for agent %s", agent_id)
                    try:
                        running[agent_id] = await start_client(agent)
                    except Exception:
                        logger.exception("Failed to start client for agent %s", agent_id)

        await asyncio.sleep(REFRESH_INTERVAL_S)


async def main_async() -> None:
    logger.info(
        "discord-worker starting: forum=%s firestore=%s refresh=%ss",
        FORUM_URL, FIRESTORE_PROJECT_ID, REFRESH_INTERVAL_S,
    )
    await reconcile_loop()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
