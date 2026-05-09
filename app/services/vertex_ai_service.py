# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Vertex AI Reasoning Engine service."""
import logging
from typing import Optional
import uuid
import asyncio
import json

import vertexai
from vertexai.preview import reasoning_engines
from google.cloud.aiplatform_v1beta1.types import reasoning_engine_execution_service as res_types
from google.protobuf import struct_pb2
from google.api_core.exceptions import ResourceExhausted

from app.config import get_settings
from app.core.exceptions import ResourceExhaustedError, AgentStreamError

logger = logging.getLogger(__name__)


class VertexAIResponse:
    """Wrapper for Vertex AI agent response with diagnostic metadata."""

    def __init__(
        self,
        text: str,
        chunk_count: int = 0,
        breakdown: Optional[dict] = None,
        function_names: Optional[list] = None,
        function_errors: Optional[list] = None,
    ):
        """
        Initialize response.

        Args:
            text: Response text from the agent (may be empty).
            chunk_count: Number of chunks received from the stream.
            breakdown: Per-part-type counts (text/function_call/...).
            function_names: Names of every function_call part observed,
                in stream order. Used by MessageProcessorV2 to surface a
                "broken tool" message naming the tool the agent got stuck
                on when it never produced final text.
            function_errors: List of dicts with error info extracted from
                function_response parts. Each dict has 'tool_name' and
                'error_type' (e.g., 'rate_limit', 'error', 'unknown').
        """
        self.text = text
        self.chunk_count = chunk_count
        self.breakdown = breakdown or {}
        self.function_names = function_names or []
        self.function_errors = function_errors or []

    @property
    def has_rate_limit_error(self) -> bool:
        """Check if any function_response indicated a rate limit error."""
        return any(e.get("error_type") == "rate_limit" for e in self.function_errors)

    @property
    def has_unanswered_function_calls(self) -> bool:
        """
        Check if there were function calls without corresponding responses.

        This indicates the tool failed to execute at all, often due to
        permission issues or crashes.
        """
        call_count = self.breakdown.get("function_call", 0)
        response_count = self.breakdown.get("function_response", 0)
        return call_count > 0 and response_count == 0


class VertexAIService:
    """Handles Vertex AI Reasoning Engine operations."""

    def __init__(self):
        """Initialize Vertex AI client."""
        settings = get_settings()
        vertexai.init(
            project=settings.gcp_project_id, location=settings.gcp_location
        )
        self._engines: dict = {}
        self._exec_clients: dict = {}
        logger.info(
            f"Vertex AI initialized for project: {settings.gcp_project_id}, "
            f"location: {settings.gcp_location}"
        )

    def _get_engine(self, agent_id: str) -> reasoning_engines.ReasoningEngine:
        """Get or create a Reasoning Engine instance."""
        if agent_id not in self._engines:
            engine = reasoning_engines.ReasoningEngine(agent_id)
            self._engines[agent_id] = engine
            self._exec_clients[agent_id] = engine.execution_api_client
            logger.info(f"Created ReasoningEngine instance for: {agent_id}")
        return self._engines[agent_id]

    async def create_session(self, agent_id: str, user_name: Optional[str] = None) -> str:
        """
        Create a new session in the Reasoning Engine.

        Args:
            agent_id: Vertex AI reasoning engine resource name
            user_name: Optional user's actual name to use as user_id

        Returns:
            Session ID from the Reasoning Engine
        """
        try:
            engine = self._get_engine(agent_id)

            # Use user's actual name if provided, otherwise generate a random ID
            if user_name:
                user_id = user_name
            else:
                user_id = f"user-{uuid.uuid4().hex[:12]}"

            # Create session in the Reasoning Engine
            loop = asyncio.get_event_loop()
            session = await loop.run_in_executor(
                None,
                lambda: engine.create_session(user_id=user_id)
            )

            session_id = session.get("id", f"session-{uuid.uuid4().hex[:16]}")

            # Store user_id with session_id for later queries
            # We'll encode both in the session_id we return
            combined_id = f"{user_id}:{session_id}"

            logger.info(f"Created Reasoning Engine session for user '{user_name}': {combined_id}")
            return combined_id

        except ResourceExhausted as e:
            logger.warning(f"Rate limit exceeded creating session for agent {agent_id}: {e}")
            raise ResourceExhaustedError(
                "Looks like Google won't let me think right now, try again in a minute."
            )
        except Exception as e:
            error_str = str(e).lower()
            if "429" in str(e) or "resource_exhausted" in error_str:
                logger.warning(
                    f"Rate limit exceeded (wrapped) creating session for agent {agent_id}: {e}"
                )
                raise ResourceExhaustedError(
                    "Looks like Google won't let me think right now, try again in a minute."
                )
            logger.error(f"Error creating session for agent {agent_id}: {e}")
            raise

    async def send_message(
        self, agent_id: str, session_id: str, message: str
    ) -> VertexAIResponse:
        """
        Send message to Vertex AI Reasoning Engine and get response.

        Args:
            agent_id: Vertex AI reasoning engine resource name
            session_id: Combined user_id:session_id from create_session
            message: User message text (may contain embedded image references)

        Returns:
            VertexAIResponse containing agent's response text
        """
        message_length = len(message)
        logger.info(f"Sending message to agent (length: {message_length} chars)")

        try:
            engine = self._get_engine(agent_id)
            exec_client = self._exec_clients[agent_id]

            # Parse the combined session_id
            if ":" in session_id:
                user_id, re_session_id = session_id.split(":", 1)
            else:
                # Fallback for old-style session IDs
                user_id = session_id
                re_session_id = None

            # Create input as Struct
            input_struct = struct_pb2.Struct()
            input_data = {
                "message": message,
                "user_id": user_id,
            }
            if re_session_id:
                input_data["session_id"] = re_session_id
            input_struct.update(input_data)

            # Create the request
            request = res_types.StreamQueryReasoningEngineRequest(
                name=engine.resource_name,
                input=input_struct,
                class_method="stream_query"
            )

            # Run in executor to avoid blocking. Capture mid-stream failures
            # separately from the "stream completed cleanly but yielded nothing"
            # case so the caller can distinguish them.
            loop = asyncio.get_event_loop()

            stream_state = {"partial_chunks": 0, "stream_error": None}

            def stream_query():
                responses = []
                try:
                    for chunk in exec_client.stream_query_reasoning_engine(request=request):
                        if chunk.data:
                            chunk_str = chunk.data.decode('utf-8')
                            responses.append(chunk_str)
                            stream_state["partial_chunks"] = len(responses)
                except Exception as exc:
                    stream_state["stream_error"] = exc
                return responses

            chunks = await loop.run_in_executor(None, stream_query)
            chunk_count = len(chunks)
            stream_error = stream_state["stream_error"]

            full_response, breakdown, function_names, function_errors = self._extract_text_from_chunks(
                chunks, message_length=message_length
            )

            # If the stream errored mid-flight, surface it as AgentStreamError
            # so MessageProcessorV2 can show a "lost connection" message.
            # Rate-limit errors take precedence (handled below).
            if stream_error is not None:
                error_str = str(stream_error).lower()
                if "429" in str(stream_error) or "resource_exhausted" in error_str:
                    logger.warning(
                        f"Rate limit during stream from Reasoning Engine {agent_id}, "
                        f"session {session_id} (after {chunk_count} chunks): {stream_error}"
                    )
                    raise ResourceExhaustedError(
                        "Looks like Google won't let me think right now, try again in a minute."
                    )
                logger.error(
                    f"Stream error from Reasoning Engine {agent_id}, "
                    f"session {session_id} (after {chunk_count} chunks): {stream_error}"
                )
                raise AgentStreamError(
                    f"Reasoning Engine stream failed mid-flight: {stream_error}"
                ) from stream_error

            if not full_response.strip():
                logger.warning(
                    f"Empty response from Reasoning Engine {agent_id} "
                    f"for session {session_id} "
                    f"(received {chunk_count} chunks)"
                )

            logger.info(
                f"Received response from Reasoning Engine {agent_id} "
                f"({chunk_count} chunks, {len(full_response)} chars)"
            )

            return VertexAIResponse(
                text=full_response,
                chunk_count=chunk_count,
                breakdown=breakdown,
                function_names=function_names,
                function_errors=function_errors,
            )

        except ResourceExhausted as e:
            logger.warning(
                f"Rate limit exceeded for Reasoning Engine {agent_id}, "
                f"session {session_id}: {e}"
            )
            raise ResourceExhaustedError(
                "Looks like Google won't let me think right now, try again in a minute."
            )
        except (ResourceExhaustedError, AgentStreamError):
            raise
        except Exception as e:
            error_str = str(e).lower()
            if "429" in str(e) or "resource_exhausted" in error_str:
                logger.warning(
                    f"Rate limit exceeded (wrapped) for Reasoning Engine {agent_id}, "
                    f"session {session_id}: {e}"
                )
                raise ResourceExhaustedError(
                    "Looks like Google won't let me think right now, try again in a minute."
                )
            logger.error(
                f"Error sending message to Reasoning Engine {agent_id}, "
                f"session {session_id}: {e}"
            )
            raise

    def _extract_text_from_chunks(
        self, chunks: list, message_length: int = 0
    ):
        """
        Extract text content from Reasoning Engine response chunks.

        The chunks contain JSON with various content types including
        function calls, function responses, and text content.
        We extract only the final text content.

        Also collects diagnostics (per-part-type counts and the names of
        every function_call) so MessageProcessorV2 can show a specific
        "broken tool" message when the agent looped on tools and never
        produced final text — the dominant empty-response failure mode
        in production.

        Additionally parses function_response parts for error indicators
        (rate limits, exceptions, etc.) to enable better error messaging.

        Args:
            chunks: List of JSON strings from the stream
            message_length: Length of the original message (for diagnostic logging)

        Returns:
            Tuple of (extracted_text, breakdown_dict, function_names_list, function_errors_list).
        """
        text_parts = []
        function_names = []
        function_errors = []
        function_responses_raw = []  # For logging on failure
        chunk_count = len(chunks)
        breakdown = {
            "text": 0,
            "function_call": 0,
            "function_response": 0,
            "other": 0,
            "unparseable": 0,
        }

        for i, chunk_str in enumerate(chunks):
            try:
                chunk = json.loads(chunk_str)
                content = chunk.get("content", {})
                parts = content.get("parts", [])

                for part in parts:
                    if "text" in part:
                        breakdown["text"] += 1
                        text_parts.append(part["text"])
                    elif "function_call" in part:
                        breakdown["function_call"] += 1
                        fc = part.get("function_call")
                        if isinstance(fc, dict):
                            name = fc.get("name")
                            if name:
                                function_names.append(name)
                    elif "function_response" in part:
                        breakdown["function_response"] += 1
                        fr = part.get("function_response", {})
                        function_responses_raw.append(fr)
                        # Parse function_response for error indicators
                        error_info = self._parse_function_response_for_errors(fr)
                        if error_info:
                            function_errors.append(error_info)
                    else:
                        breakdown["other"] += 1

            except json.JSONDecodeError:
                # If not valid JSON, treat as raw text but flag it.
                breakdown["unparseable"] += 1
                text_parts.append(chunk_str)
            except Exception as e:
                logger.debug(f"Error parsing chunk {i}: {e}")
                breakdown["other"] += 1
                continue

        result = "".join(text_parts)

        breakdown_str = (
            f"text={breakdown['text']} "
            f"function_call={breakdown['function_call']} "
            f"function_response={breakdown['function_response']} "
            f"other={breakdown['other']} "
            f"unparseable={breakdown['unparseable']}"
        )
        names_str = ",".join(function_names) if function_names else "(none)"
        errors_str = json.dumps(function_errors) if function_errors else "(none)"

        if not result.strip() and chunk_count > 0:
            first_chunk_preview = chunks[0][:500] if chunks else "(no chunks)"
            # Log function_response content for debugging tool failures
            fr_preview = json.dumps(function_responses_raw)[:2000] if function_responses_raw else "(none)"
            logger.warning(
                f"Empty text extracted from {chunk_count} chunks. "
                f"Breakdown: {breakdown_str}. "
                f"Functions called: {names_str}. "
                f"Function errors detected: {errors_str}. "
                f"Input message was {message_length} chars. "
                f"First chunk preview: {first_chunk_preview}"
            )
            if function_responses_raw:
                logger.info(
                    f"Function responses (for debugging): {fr_preview}"
                )
        else:
            logger.debug(
                f"Chunk breakdown ({chunk_count} chunks): {breakdown_str}, "
                f"functions: {names_str}"
            )

        return result, breakdown, function_names, function_errors

    def _parse_function_response_for_errors(self, fr: dict) -> Optional[dict]:
        """
        Parse a function_response part for error indicators.

        Looks for rate limit errors (429, resource_exhausted, quota) and
        other error patterns in the response content.

        Args:
            fr: The function_response dict from a chunk part

        Returns:
            Dict with 'tool_name' and 'error_type' if an error is detected,
            None otherwise.
        """
        if not isinstance(fr, dict):
            return None

        tool_name = fr.get("name", "unknown_tool")
        response = fr.get("response", {})

        # Convert response to string for pattern matching
        response_str = json.dumps(response).lower() if response else ""

        # Check for rate limit indicators
        rate_limit_patterns = [
            "429",
            "resource_exhausted",
            "resourceexhausted",
            "quota",
            "rate limit",
            "rate_limit",
            "ratelimit",
            "too many requests",
            "requests per minute",
            "rpm limit",
            "qpm limit",
        ]

        for pattern in rate_limit_patterns:
            if pattern in response_str:
                logger.warning(
                    f"Rate limit error detected in function_response for tool '{tool_name}': "
                    f"matched pattern '{pattern}'"
                )
                return {
                    "tool_name": tool_name,
                    "error_type": "rate_limit",
                    "pattern_matched": pattern,
                }

        # Check for permission/access denied errors
        access_denied_patterns = [
            "403",
            "forbidden",
            "permission denied",
            "permission_denied",
            "access denied",
            "access_denied",
            "unauthorized",
            "not authorized",
            "insufficient permissions",
            "insufficient_permissions",
            "iam",
            "requires permission",
        ]

        for pattern in access_denied_patterns:
            if pattern in response_str:
                logger.warning(
                    f"Access denied error detected in function_response for tool '{tool_name}': "
                    f"matched pattern '{pattern}'"
                )
                return {
                    "tool_name": tool_name,
                    "error_type": "access_denied",
                    "pattern_matched": pattern,
                }

        # Check for general error indicators
        error_patterns = [
            '"error"',
            '"exception"',
            '"failed"',
            '"failure"',
            "500",
            "502",
            "503",
            "504",
            "internal server error",
            "service unavailable",
        ]

        for pattern in error_patterns:
            if pattern in response_str:
                logger.info(
                    f"Error detected in function_response for tool '{tool_name}': "
                    f"matched pattern '{pattern}'"
                )
                return {
                    "tool_name": tool_name,
                    "error_type": "error",
                    "pattern_matched": pattern,
                }

        return None
