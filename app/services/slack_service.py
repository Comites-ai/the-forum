# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Slack API service."""
import logging

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
import aiohttp

logger = logging.getLogger(__name__)


class SlackService:
    """Handles Slack API operations."""

    async def open_conversation(self, token: str, user_id: str) -> str:
        """
        Open or get existing DM conversation with a user.

        This ensures we're using the canonical DM channel ID, which can
        help with message threading issues.

        Args:
            token: Slack Bot User OAuth Token
            user_id: Slack user ID to open DM with

        Returns:
            Channel ID for the DM conversation
        """
        client = AsyncWebClient(token=token)
        try:
            response = await client.conversations_open(users=[user_id])
            if response["ok"]:
                channel_id = response["channel"]["id"]
                logger.debug(f"Opened conversation with user {user_id}: {channel_id}")
                return channel_id
            else:
                logger.error(f"Failed to open conversation: {response}")
                raise Exception(f"Failed to open conversation: {response}")
        except SlackApiError as e:
            logger.error(f"Error opening conversation: {e.response.get('error')}")
            raise

    async def post_message(self, token: str, channel: str, text: str) -> dict:
        """
        Post message to Slack channel.

        Args:
            token: Slack Bot User OAuth Token (xoxb-...)
            channel: Slack channel ID or DM channel ID
            text: Message text to send

        Returns:
            Slack API response dict

        Raises:
            SlackApiError: If Slack API call fails
        """
        client = AsyncWebClient(token=token)

        try:
            logger.debug(f"Posting message to channel: {channel}")
            response = await client.chat_postMessage(channel=channel, text=text)

            if response["ok"]:
                # Log details about where the message was posted
                response_channel = response.get("channel")
                response_ts = response.get("ts")
                logger.info(
                    f"Successfully posted message to channel: {channel}, "
                    f"response_channel: {response_channel}, ts: {response_ts}"
                )

                # Check if the response channel matches what we sent
                if response_channel != channel:
                    logger.warning(
                        f"Channel mismatch! Requested: {channel}, Got: {response_channel}"
                    )
            else:
                logger.error(
                    f"Slack API returned ok=False for channel {channel}: {response}"
                )

            return response

        except SlackApiError as e:
            error_message = e.response.get("error", "unknown_error")
            logger.error(
                f"Slack API error posting to channel {channel}: {error_message}"
            )
            raise
        except Exception as e:
            logger.error(f"Unexpected error posting to Slack channel {channel}: {e}")
            raise

    async def get_user_info(self, token: str, user_id: str) -> dict:
        """
        Get Slack user profile info (display name, real name).

        Args:
            token: Slack Bot User OAuth Token
            user_id: Slack user ID (U...)

        Returns:
            User info dict from Slack API

        Raises:
            SlackApiError: If Slack API call fails
        """
        client = AsyncWebClient(token=token)
        try:
            response = await client.users_info(user=user_id)
            if response["ok"]:
                return response["user"]
            else:
                logger.error(f"Failed to get user info: {response}")
                return {}
        except SlackApiError as e:
            logger.error(f"Error getting user info for {user_id}: {e.response.get('error')}")
            return {}

    async def get_conversation_info(self, token: str, channel: str) -> dict:
        """
        Get information about a conversation/channel.

        Args:
            token: Slack Bot User OAuth Token
            channel: Channel ID to get info for

        Returns:
            Conversation info dict
        """
        client = AsyncWebClient(token=token)
        try:
            response = await client.conversations_info(channel=channel)
            return response
        except SlackApiError as e:
            logger.error(f"Error getting conversation info: {e.response.get('error')}")
            raise

    async def download_file(self, token: str, url: str) -> bytes:
        """
        Download a file from Slack's private URL.

        Slack file URLs (url_private) require authentication with the bot token.

        Args:
            token: Slack Bot User OAuth Token
            url: The url_private from a Slack file object

        Returns:
            Raw file bytes

        Raises:
            Exception: If download fails
        """
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {token}"}
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    file_bytes = await response.read()
                    logger.info(f"Downloaded file from Slack: {len(file_bytes)} bytes")
                    return file_bytes
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to download file: {response.status} - {error_text}")
                    raise Exception(f"Failed to download Slack file: {response.status}")
