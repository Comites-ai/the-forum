# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Conversation-history MCP server (Streamable HTTP) — PLAT-43.

Lets an agent read back the conversations it has actually had with one
user, verbatim, instead of relying on whatever it wrote into its own memory
afterwards. Three tools:

  - search_history:  ranked hits for a phrase or keywords (snippets only)
  - get_context:     one hit expanded into the messages around it, in full
  - get_messages:    a verbatim time range, oldest first, paged

Scoping is structural. The caller's identity comes from its API key (same
key and header as the scheduler and agents MCP servers), the user is named
on every call and validated against the user registry, and the log lives
in one Firestore subcollection per agent-user pair — so the only
conversation any query can reach is the caller's own with that user.
Message ids handed back to the agent are opaque tokens that name the
conversation they came from; a token minted for another agent is rejected.

Search is a scan of the window with in-memory scoring: exact phrase first,
then every word, then some words, recency breaking ties. One week of one
agent-user pair is a few thousand short documents at most, and substring
matching is what settles "didn't you tell me to move the SOW whole?" — a
token index cannot do that. Nothing is summarised on the way out, and
every response is capped at RESPONSE_CHAR_CAP characters so a sloppy query
cannot flood the agent's context window.

Retrieval is pull only. The Forum never injects history into a turn.

Mounted as an ASGI app at /api/v1/mcp/history; the session manager's
lifecycle is managed in the FastAPI app's lifespan in app/main.py.
"""
import base64
import contextvars
import json
import logging
from datetime import date, datetime, time, timedelta, UTC
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool

from app.api.v1.scheduler_mcp import hash_api_key
from app.config import get_settings
from app.models.agent import Agent
from app.models.message import RETENTION, LoggedMessage
from app.models.user import User
from app.services.firestore_service import FirestoreService

logger = logging.getLogger(__name__)

# Hard cap on the serialised size of any response. When it bites, the
# response says so (truncated: true) and how to get the rest.
RESPONSE_CHAR_CAP = 8_000

SEARCH_DEFAULT_LIMIT = 10
SEARCH_MAX_LIMIT = 25
SNIPPET_CHARS = 200

CONTEXT_DEFAULT = 5
CONTEXT_MAX = 20

MESSAGES_DEFAULT_LIMIT = 50
MESSAGES_MAX_LIMIT = 100

TIMESTAMP_FORMAT = "%a %Y-%m-%d %H:%M %Z"


# ---------------------------------------------------------------------------
# Per-request context (same pattern as agents_mcp)
# ---------------------------------------------------------------------------
_request_ctx: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "history_mcp_request_ctx", default=None
)


def _ctx() -> dict:
    ctx = _request_ctx.get()
    if ctx is None:
        raise RuntimeError("history MCP tool called outside of an authenticated request")
    return ctx


def _caller() -> Agent:
    return _ctx()["agent"]


def _firestore() -> FirestoreService:
    return _ctx()["firestore"]


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
_USER_DESC = (
    "The human user whose conversation with you this concerns, EXACTLY as "
    "their name appears in the '[From: <name>] ...' prefix of the conversation "
    "you are working in. Required: you serve several users and each has their "
    "own history. Do not guess or paraphrase."
)
_DATE_DESC = (
    "ISO 8601 date ('2026-09-12') or datetime. A bare date or a datetime "
    "without an offset is read in the user's timezone."
)

TOOLS: list[Tool] = [
    Tool(
        name="search_history",
        description=(
            "Search your past conversation with one user for a phrase or "
            "keywords and get ranked hits with short snippets. Exact phrase "
            "matches rank first, then messages containing every word, then "
            "some words; newer messages win ties. Covers the last 7 days "
            "unless you give a date range. Use get_context on a hit's "
            "message_id to read the full text and what was said around it. "
            "Results are verbatim, never summarised."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": _USER_DESC},
                "query": {
                    "type": "string",
                    "description": "The phrase or words to look for (case-insensitive).",
                },
                "date_from": {"type": "string", "description": f"Earliest message. {_DATE_DESC}"},
                "date_to": {"type": "string", "description": f"Latest message. {_DATE_DESC}"},
                "author": {
                    "type": "string",
                    "enum": ["user", "agent"],
                    "description": "Only messages the user sent, or only ones you sent.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Hits to return, default {SEARCH_DEFAULT_LIMIT}, at most {SEARCH_MAX_LIMIT}.",
                },
            },
            "required": ["user", "query"],
        },
    ),
    Tool(
        name="get_context",
        description=(
            "Expand one search hit into the verbatim messages around it in "
            "that conversation: the hit itself in full plus the messages "
            "immediately before and after it, with timestamps and authors."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "A message_id from search_history or get_messages.",
                },
                "before": {
                    "type": "integer",
                    "description": f"Messages before the hit, default {CONTEXT_DEFAULT}, at most {CONTEXT_MAX}.",
                },
                "after": {
                    "type": "integer",
                    "description": f"Messages after the hit, default {CONTEXT_DEFAULT}, at most {CONTEXT_MAX}.",
                },
            },
            "required": ["message_id"],
        },
    ),
    Tool(
        name="get_messages",
        description=(
            "Read your conversation with one user over a time range, "
            "verbatim and in order, oldest first. For 'everything from "
            "yesterday' when there is no phrase to search for. Paged: when "
            "next_cursor is set, call again with cursor to continue."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": _USER_DESC},
                "start": {"type": "string", "description": f"Start of the range. {_DATE_DESC}"},
                "end": {"type": "string", "description": f"End of the range. {_DATE_DESC}"},
                "cursor": {
                    "type": "string",
                    "description": "next_cursor from the previous page.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Messages per page, default {MESSAGES_DEFAULT_LIMIT}, at most {MESSAGES_MAX_LIMIT}.",
                },
            },
            "required": ["user", "start", "end"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Helpers: users, time, ids
# ---------------------------------------------------------------------------
async def _resolve_user(name: Any) -> User:
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            "user is required: the name of the human user this concerns, from "
            "the '[From: <name>]' prefix of your conversation."
        )
    user = await _firestore().get_user_by_any_name(name.strip())
    if not user:
        raise ValueError(
            f"No user found with name {name!r}. Pass the exact name from the "
            f"'[From: <name>]' prefix — do not paraphrase."
        )
    return user


def _user_timezone(user: Optional[User]) -> str:
    tz_name = (user.default_timezone if user else None) or get_settings().default_user_timezone
    try:
        ZoneInfo(tz_name)
    except (KeyError, ValueError):
        logger.warning(f"Unknown timezone {tz_name!r} for user; falling back to UTC")
        return "UTC"
    return tz_name


def _format_timestamp(when: datetime, tz_name: str) -> str:
    return when.astimezone(ZoneInfo(tz_name)).strftime(TIMESTAMP_FORMAT)


def _parse_when(value: Any, field: str, tz_name: str, *, end_of_day: bool = False) -> Optional[datetime]:
    """An ISO date or datetime → aware UTC. Bare dates span the user's local day."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO 8601 date or datetime string.")
    raw = value.strip()
    tz = ZoneInfo(tz_name)
    try:
        if len(raw) == 10:
            day = date.fromisoformat(raw)
            edge = time.max if end_of_day else time.min
            return datetime.combine(day, edge, tzinfo=tz).astimezone(UTC)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"{field} must be an ISO 8601 date ('2026-09-12') or datetime; got {value!r}."
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


def _clamp(value: Any, field: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer.")
    return max(low, min(high, number))


def _encode_message_id(agent_id: str, user_id: str, doc_id: str) -> str:
    raw = f"{agent_id}|{user_id}|{doc_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_message_id(token: Any) -> tuple[str, str, str]:
    if not isinstance(token, str) or not token.strip():
        raise ValueError("message_id is required. Use one returned by search_history or get_messages.")
    padded = token.strip() + "=" * (-len(token.strip()) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded).decode("utf-8")
        agent_id, user_id, doc_id = raw.split("|", 2)
    except Exception:
        raise ValueError(f"message_id {token!r} is not a message id from this server.")
    if not (agent_id and user_id and doc_id):
        raise ValueError(f"message_id {token!r} is not a message id from this server.")
    return agent_id, user_id, doc_id


def _message_dict(message: LoggedMessage, tz_name: str, *, text: Optional[str] = None) -> dict:
    out = {
        "message_id": _encode_message_id(message.agent_id, message.user_id, message.id or ""),
        "timestamp": _format_timestamp(message.created_at, tz_name),
        "author": message.author,
        "role": "user" if message.direction == "inbound" else "agent",
        "platform": message.platform,
    }
    if message.kind == "scheduled":
        out["kind"] = "scheduled"
    if message.attachments:
        out["attachments"] = list(message.attachments)
    out["text" if text is None else "snippet"] = message.text if text is None else text
    return out


# ---------------------------------------------------------------------------
# Helpers: scoring and the character cap
# ---------------------------------------------------------------------------
def _score(text: str, phrase: str, words: list[str]) -> Optional[tuple[int, int, int]]:
    """(tier, words matched, position of the best match) or None when nothing matches."""
    haystack = text.casefold()
    position = haystack.find(phrase)
    if position >= 0:
        return (3, len(words), position)
    positions = [haystack.find(w) for w in words]
    matched = [p for p in positions if p >= 0]
    if not matched:
        return None
    tier = 2 if len(matched) == len(words) else 1
    return (tier, len(matched), min(matched))


def _snippet(text: str, position: int, width: int = SNIPPET_CHARS) -> str:
    """About ``width`` characters centred on ``position``, marked where cut."""
    if len(text) <= width:
        return text
    start = max(0, min(position - width // 2, len(text) - width))
    end = start + width
    piece = text[start:end]
    if start > 0:
        piece = "…" + piece
    if end < len(text):
        piece = piece + "…"
    return piece


# Placeholders at least as long as anything written into these fields after
# fitting, so the final response cannot creep back over the cap.
_RESERVED_NOTE = "x" * 200
_RESERVED_CURSOR = "0000-00-00T00:00:00.000000+00:00"


def _fit_under_cap(payload: dict, drop_one: Callable[[], bool]) -> None:
    """Drop items (via drop_one) until the serialised payload fits, marking truncation."""
    while len(json.dumps(payload)) > RESPONSE_CHAR_CAP:
        if not drop_one():
            break
        payload["truncated"] = True


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------
async def _handle_search_history(args: dict[str, Any]) -> str:
    caller = _caller()
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required: the phrase or words to look for.")
    phrase = " ".join(query.split()).casefold()
    words = phrase.split(" ")

    user = await _resolve_user(args.get("user"))
    tz_name = _user_timezone(user)

    author = args.get("author")
    if author not in (None, "", "user", "agent"):
        raise ValueError("author must be 'user' or 'agent'.")
    limit = _clamp(args.get("limit"), "limit", SEARCH_DEFAULT_LIMIT, 1, SEARCH_MAX_LIMIT)

    now = datetime.now(UTC)
    start = _parse_when(args.get("date_from"), "date_from", tz_name) or (now - RETENTION)
    end = _parse_when(args.get("date_to"), "date_to", tz_name, end_of_day=True) or now
    if start > end:
        raise ValueError("date_from must not be after date_to.")

    candidates = await _firestore().list_messages(caller.id, user.id, start, end)
    wanted_direction = {"user": "inbound", "agent": "outbound"}.get(author or "")
    if wanted_direction:
        candidates = [m for m in candidates if m.direction == wanted_direction]

    scored: list[tuple[tuple[int, int, int], LoggedMessage]] = []
    for message in candidates:
        score = _score(message.text, phrase, words)
        if score:
            scored.append((score, message))
    scored.sort(key=lambda item: (item[0][0], item[0][1], item[1].created_at), reverse=True)

    hits = [
        _message_dict(message, tz_name, text=_snippet(message.text, score[2]))
        for score, message in scored[:limit]
    ]
    payload: dict[str, Any] = {
        "user": user.primary_name,
        "timezone": tz_name,
        "window": {"from": _format_timestamp(start, tz_name), "to": _format_timestamp(end, tz_name)},
        "total_matches": len(scored),
        "returned": len(hits),
        "hits": hits,
        "truncated": False,
        "note": _RESERVED_NOTE,
    }
    _fit_under_cap(payload, lambda: bool(hits) and hits.pop() is not None)
    payload["returned"] = len(hits)
    del payload["note"]
    if len(scored) > len(hits):
        payload["note"] = (
            f"{len(scored) - len(hits)} more matching messages not shown"
            + (" (response size cap)" if payload["truncated"] else "")
            + ". Narrow with date_from/date_to, a more specific query, or author."
        )
    return json.dumps(payload)


async def _handle_get_context(args: dict[str, Any]) -> str:
    caller = _caller()
    agent_id, user_id, doc_id = _decode_message_id(args.get("message_id"))
    if agent_id != caller.id:
        raise ValueError("That message is not in your history.")
    before = _clamp(args.get("before"), "before", CONTEXT_DEFAULT, 0, CONTEXT_MAX)
    after = _clamp(args.get("after"), "after", CONTEXT_DEFAULT, 0, CONTEXT_MAX)

    firestore = _firestore()
    anchor = await firestore.get_message(caller.id, user_id, doc_id)
    if not anchor:
        raise ValueError("No message with that id in your history (it may have expired).")
    user = await firestore.get_user_by_id(user_id)
    tz_name = _user_timezone(user)

    preceding, following = await firestore.list_messages_around(
        caller.id, user_id, anchor, before=before, after=after
    )
    before_dicts = [_message_dict(m, tz_name) for m in preceding]
    after_dicts = [_message_dict(m, tz_name) for m in following]
    anchor_dict = _message_dict(anchor, tz_name)

    payload: dict[str, Any] = {
        "user": user.primary_name if user else user_id,
        "timezone": tz_name,
        "anchor_message_id": anchor_dict["message_id"],
        "messages": [],
        "truncated": False,
        "note": _RESERVED_NOTE,
    }

    def assemble() -> None:
        payload["messages"] = before_dicts + [anchor_dict] + after_dicts

    def drop_farthest() -> bool:
        # Shed from whichever side reaches further from the hit, keeping
        # the hit itself; the note tells the caller how to see the rest.
        if not before_dicts and not after_dicts:
            return False
        if len(after_dicts) >= len(before_dicts):
            after_dicts.pop()
        else:
            before_dicts.pop(0)
        assemble()
        return True

    assemble()
    _fit_under_cap(payload, drop_farthest)
    del payload["note"]
    if payload["truncated"]:
        payload["note"] = (
            f"Only {len(before_dicts)} messages before and {len(after_dicts)} after fit the "
            "response size cap. Ask for fewer with before/after, or page through the range "
            "with get_messages."
        )
    return json.dumps(payload)


async def _handle_get_messages(args: dict[str, Any]) -> str:
    caller = _caller()
    user = await _resolve_user(args.get("user"))
    tz_name = _user_timezone(user)

    start = _parse_when(args.get("start"), "start", tz_name)
    end = _parse_when(args.get("end"), "end", tz_name, end_of_day=True)
    if start is None or end is None:
        raise ValueError("start and end are required (ISO 8601 date or datetime).")
    if start > end:
        raise ValueError("start must not be after end.")
    limit = _clamp(args.get("limit"), "limit", MESSAGES_DEFAULT_LIMIT, 1, MESSAGES_MAX_LIMIT)

    cursor = None
    if args.get("cursor"):
        try:
            cursor = datetime.fromisoformat(str(args["cursor"]))
        except ValueError:
            raise ValueError("cursor must be a next_cursor value from a previous page.")
        if cursor.tzinfo is None:
            cursor = cursor.replace(tzinfo=UTC)

    rows = await _firestore().list_messages(
        caller.id, user.id, start, end, after=cursor, limit=limit + 1
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    messages = [_message_dict(m, tz_name) for m in rows]

    payload: dict[str, Any] = {
        "user": user.primary_name,
        "timezone": tz_name,
        "window": {"from": _format_timestamp(start, tz_name), "to": _format_timestamp(end, tz_name)},
        "returned": len(messages),
        "messages": messages,
        "truncated": False,
        "next_cursor": _RESERVED_CURSOR,
        "note": _RESERVED_NOTE,
    }
    _fit_under_cap(payload, lambda: bool(messages) and messages.pop() is not None)
    payload["returned"] = len(messages)
    payload["next_cursor"] = None
    del payload["note"]
    if messages and (has_more or payload["truncated"]):
        payload["next_cursor"] = rows[len(messages) - 1].created_at.isoformat()
    if payload["truncated"]:
        payload["note"] = (
            "Response size cap reached; pass next_cursor as cursor to continue "
            "from the last message shown."
        )
    elif has_more:
        payload["note"] = "More messages in this range; pass next_cursor as cursor to continue."
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# MCP Server registration
# ---------------------------------------------------------------------------
def _build_server() -> Server:
    server: Server = Server("history")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def _call_tool(name: str, arguments: Optional[dict[str, Any]]) -> list[TextContent]:
        args = arguments or {}
        try:
            if name == "search_history":
                result = await _handle_search_history(args)
            elif name == "get_context":
                result = await _handle_get_context(args)
            elif name == "get_messages":
                result = await _handle_get_messages(args)
            else:
                raise ValueError(f"Unknown tool: {name}")
            return [TextContent(type="text", text=result)]
        except ValueError:
            # Surface as a clean error response — MCP wraps raised exceptions as isError=True
            raise
        except Exception as e:
            logger.exception(f"Error in history MCP tool {name!r}: {e}")
            raise

    return server


mcp_server = _build_server()
session_manager = StreamableHTTPSessionManager(
    mcp_server,
    stateless=True,
    json_response=True,
)


# ---------------------------------------------------------------------------
# ASGI entry point
# ---------------------------------------------------------------------------
async def _send_json(send, status: int, body: dict) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


async def asgi_app(scope, receive, send) -> None:
    """ASGI entry point: authenticate via X-API-Key, then delegate to MCP."""
    if scope["type"] != "http":
        await _send_json(send, 405, {"error": "Method not allowed"})
        return

    headers = {k: v for k, v in scope.get("headers", [])}
    api_key_bytes = headers.get(b"x-api-key")
    if not api_key_bytes:
        await _send_json(send, 401, {"error": "Missing X-API-Key header"})
        return

    try:
        api_key = api_key_bytes.decode("utf-8")
    except UnicodeDecodeError:
        await _send_json(send, 400, {"error": "Invalid X-API-Key encoding"})
        return

    state = scope.get("state", {})
    firestore: Optional[FirestoreService] = state.get("firestore")
    if firestore is None:
        app = scope.get("app")
        firestore = getattr(getattr(app, "state", None), "firestore", None)
    if firestore is None:
        logger.error("history MCP: FirestoreService not available in ASGI scope")
        await _send_json(send, 500, {"error": "Server misconfigured"})
        return

    agent = await firestore.get_agent_by_scheduler_api_key_hash(hash_api_key(api_key))
    if not agent:
        await _send_json(send, 401, {"error": "Invalid API key"})
        return

    token = _request_ctx.set({"agent": agent, "firestore": firestore})
    try:
        await session_manager.handle_request(scope, receive, send)
    finally:
        _request_ctx.reset(token)
