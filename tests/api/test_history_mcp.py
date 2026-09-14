# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""History MCP tool-handler tests (PLAT-43).

Exercise the handlers directly by setting the per-request ContextVar, as the
agents MCP tests do. Cover: scoping to the caller's own conversation with
the named user, the ranking order, localized timestamps and local-day
windows, get_context's neighbours and id binding, get_messages paging, and
the response character cap on every tool.
"""
import json
from datetime import datetime, timedelta, UTC
from zoneinfo import ZoneInfo

import pytest

from app.api.v1 import history_mcp
from app.api.v1.history_mcp import RESPONSE_CHAR_CAP, _encode_message_id
from app.models.agent import Agent
from app.models.message import LoggedMessage
from app.models.user import PlatformIdentity, User


CALLER_ID = "agent-maggie"
OTHER_ID = "agent-sam"
TZ = "America/New_York"


@pytest.fixture
def caller(fake_firestore) -> Agent:
    a = Agent(vertex_ai_agent_id="re-maggie", display_name="Maggie the Magister", id=CALLER_ID)
    fake_firestore.add_agent(a, agent_id=CALLER_ID)
    return a


@pytest.fixture
def other(fake_firestore) -> Agent:
    a = Agent(vertex_ai_agent_id="re-sam", display_name="Sam the Som", id=OTHER_ID)
    fake_firestore.add_agent(a, agent_id=OTHER_ID)
    return a


@pytest.fixture
async def user_id(fake_firestore) -> str:
    return await fake_firestore.create_user(
        User(
            primary_name="Jonathan Cavell",
            default_timezone=TZ,
            identities=[
                PlatformIdentity(platform="discord", platform_user_id="D_001", display_name="jonathan")
            ],
        )
    )


@pytest.fixture
async def stranger_id(fake_firestore) -> str:
    return await fake_firestore.create_user(
        User(
            primary_name="Nobody Yet",
            identities=[PlatformIdentity(platform="slack", platform_user_id="U_9")],
        )
    )


@pytest.fixture
def request_ctx(fake_firestore, caller, other):
    token = history_mcp._request_ctx.set({"agent": caller, "firestore": fake_firestore})
    yield
    history_mcp._request_ctx.reset(token)


async def seed(
    fake_firestore,
    text: str,
    *,
    at: datetime,
    user_id: str,
    agent_id: str = CALLER_ID,
    direction: str = "inbound",
    platform: str = "discord",
    kind: str = "live",
    author: str | None = None,
) -> str:
    return await fake_firestore.append_message(
        LoggedMessage(
            agent_id=agent_id,
            user_id=user_id,
            platform=platform,
            direction=direction,
            author=author or ("Jonathan Cavell" if direction == "inbound" else "Maggie the Magister"),
            text=text,
            kind=kind,
            created_at=at,
        )
    )


def _ago(**kwargs) -> datetime:
    return datetime.now(UTC) - timedelta(**kwargs)


# ---- search_history ----


async def test_phrase_hit_ranks_first_with_localized_timestamp(fake_firestore, user_id, request_ctx):
    when = _ago(days=1)
    await seed(fake_firestore, "Yes — move the RTX SOW whole, don't split it.", at=when, user_id=user_id, direction="outbound")
    await seed(fake_firestore, "the RTX budget is separate from the SOW", at=_ago(hours=2), user_id=user_id)
    await seed(fake_firestore, "unrelated chatter about lunch", at=_ago(hours=1), user_id=user_id)

    result = json.loads(await history_mcp._handle_search_history(
        {"user": "Jonathan Cavell", "query": "move the RTX SOW whole"}
    ))

    assert result["user"] == "Jonathan Cavell"
    assert result["timezone"] == TZ
    assert result["total_matches"] == 2
    assert result["returned"] == 2
    assert result["truncated"] is False
    top = result["hits"][0]
    assert "move the RTX SOW whole" in top["snippet"]
    assert top["author"] == "Maggie the Magister"
    assert top["role"] == "agent"
    assert top["platform"] == "discord"
    assert top["timestamp"] == when.astimezone(ZoneInfo(TZ)).strftime("%a %Y-%m-%d %H:%M %Z")
    assert "EDT" in top["timestamp"] or "EST" in top["timestamp"]


async def test_ranking_phrase_then_all_words_then_some_then_recency(fake_firestore, user_id, request_ctx):
    await seed(fake_firestore, "some: only budget here", at=_ago(hours=5), user_id=user_id)
    await seed(fake_firestore, "all: the budget and the review, apart", at=_ago(hours=4), user_id=user_id)
    await seed(fake_firestore, "phrase: budget review is on Friday", at=_ago(hours=3), user_id=user_id)
    await seed(fake_firestore, "some, newer: review only", at=_ago(hours=1), user_id=user_id)
    await seed(fake_firestore, "nothing relevant at all", at=_ago(minutes=30), user_id=user_id)

    result = json.loads(await history_mcp._handle_search_history(
        {"user": "Jonathan Cavell", "query": "Budget Review"}
    ))

    snippets = [h["snippet"] for h in result["hits"]]
    assert snippets == [
        "phrase: budget review is on Friday",
        "all: the budget and the review, apart",
        "some, newer: review only",
        "some: only budget here",
    ]


async def test_author_filter_and_limit_clamp(fake_firestore, user_id, request_ctx):
    for i in range(30):
        await seed(
            fake_firestore, f"deploy note {i}", at=_ago(minutes=i + 1), user_id=user_id,
            direction="inbound" if i % 2 else "outbound",
        )

    only_user = json.loads(await history_mcp._handle_search_history(
        {"user": "Jonathan Cavell", "query": "deploy", "author": "user", "limit": 999}
    ))
    assert only_user["total_matches"] == 15
    assert len(only_user["hits"]) == 15
    assert all(h["role"] == "user" for h in only_user["hits"])

    everything = json.loads(await history_mcp._handle_search_history(
        {"user": "Jonathan Cavell", "query": "deploy", "limit": 999}
    ))
    assert everything["total_matches"] == 30
    assert len(everything["hits"]) == history_mcp.SEARCH_MAX_LIMIT
    assert "more matching messages not shown" in everything["note"]

    with pytest.raises(ValueError, match="author must be"):
        await history_mcp._handle_search_history(
            {"user": "Jonathan Cavell", "query": "deploy", "author": "bot"}
        )


async def test_snippet_is_centred_on_the_match(fake_firestore, user_id, request_ctx):
    text = ("a" * 400) + " the needle sentence " + ("b" * 400)
    await seed(fake_firestore, text, at=_ago(hours=1), user_id=user_id)

    result = json.loads(await history_mcp._handle_search_history(
        {"user": "Jonathan Cavell", "query": "needle sentence"}
    ))
    snippet = result["hits"][0]["snippet"]
    assert "needle sentence" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) <= history_mcp.SNIPPET_CHARS + 2


async def test_scoped_to_caller_and_named_user(fake_firestore, user_id, stranger_id, request_ctx):
    await seed(fake_firestore, "secret wine list", at=_ago(hours=1), user_id=user_id, agent_id=OTHER_ID)
    await seed(fake_firestore, "secret plan for the stranger", at=_ago(hours=1), user_id=stranger_id)

    as_caller = json.loads(await history_mcp._handle_search_history(
        {"user": "Jonathan Cavell", "query": "secret"}
    ))
    assert as_caller["hits"] == []
    assert as_caller["total_matches"] == 0

    stranger = json.loads(await history_mcp._handle_search_history(
        {"user": "Nobody Yet", "query": "secret"}
    ))
    assert [h["snippet"] for h in stranger["hits"]] == ["secret plan for the stranger"]


async def test_unknown_user_is_rejected_like_query_agent(fake_firestore, request_ctx):
    with pytest.raises(ValueError, match="No user found with name 'Ghost'"):
        await history_mcp._handle_search_history({"user": "Ghost", "query": "anything"})
    with pytest.raises(ValueError, match="user is required"):
        await history_mcp._handle_search_history({"query": "anything"})
    with pytest.raises(ValueError, match="query is required"):
        await history_mcp._handle_search_history({"user": "Ghost", "query": "  "})


async def test_bare_dates_are_the_users_local_day(fake_firestore, user_id, request_ctx):
    local_now = datetime.now(ZoneInfo(TZ))
    yesterday = (local_now - timedelta(days=1)).date()
    late_yesterday = datetime.combine(yesterday, datetime.min.time(), tzinfo=ZoneInfo(TZ)) + timedelta(hours=23, minutes=30)
    await seed(fake_firestore, "late night thought", at=late_yesterday.astimezone(UTC), user_id=user_id)

    hit = json.loads(await history_mcp._handle_search_history({
        "user": "Jonathan Cavell", "query": "late night",
        "date_from": yesterday.isoformat(), "date_to": yesterday.isoformat(),
    }))
    assert hit["total_matches"] == 1

    day_before = (yesterday - timedelta(days=1)).isoformat()
    miss = json.loads(await history_mcp._handle_search_history({
        "user": "Jonathan Cavell", "query": "late night",
        "date_from": day_before, "date_to": day_before,
    }))
    assert miss["total_matches"] == 0

    with pytest.raises(ValueError, match="date_from must be an ISO 8601"):
        await history_mcp._handle_search_history(
            {"user": "Jonathan Cavell", "query": "x", "date_from": "last tuesday"}
        )


async def test_search_respects_the_character_cap(fake_firestore, user_id, request_ctx):
    for i in range(30):
        await seed(fake_firestore, f"needle {i} " + ("x" * 1000), at=_ago(minutes=i + 1), user_id=user_id)

    raw = await history_mcp._handle_search_history(
        {"user": "Jonathan Cavell", "query": "needle", "limit": 25}
    )
    assert len(raw) <= RESPONSE_CHAR_CAP
    result = json.loads(raw)
    assert result["truncated"] is True
    assert 0 < result["returned"] < 25
    assert "response size cap" in result["note"]


# ---- get_context ----


async def _seed_numbered(fake_firestore, user_id, count: int = 12) -> list[str]:
    ids = []
    for i in range(count):
        ids.append(await seed(
            fake_firestore, f"message number {i}", at=_ago(hours=count - i), user_id=user_id,
            direction="inbound" if i % 2 == 0 else "outbound",
        ))
    return ids


async def test_get_context_returns_verbatim_neighbours(fake_firestore, user_id, request_ctx):
    ids = await _seed_numbered(fake_firestore, user_id)
    token = _encode_message_id(CALLER_ID, user_id, ids[6])

    result = json.loads(await history_mcp._handle_get_context(
        {"message_id": token, "before": 2, "after": 3}
    ))

    assert result["anchor_message_id"] == token
    assert [m["text"] for m in result["messages"]] == [
        "message number 4", "message number 5", "message number 6",
        "message number 7", "message number 8", "message number 9",
    ]
    assert result["messages"][2]["message_id"] == token
    assert result["messages"][2]["role"] == "user"
    assert result["truncated"] is False


async def test_get_context_defaults_and_clamps(fake_firestore, user_id, request_ctx, monkeypatch):
    # Lift the size cap so this test sees the before/after clamp alone.
    monkeypatch.setattr(history_mcp, "RESPONSE_CHAR_CAP", 1_000_000)
    ids = await _seed_numbered(fake_firestore, user_id, count=60)
    token = _encode_message_id(CALLER_ID, user_id, ids[30])

    default = json.loads(await history_mcp._handle_get_context({"message_id": token}))
    assert len(default["messages"]) == 11

    wide = json.loads(await history_mcp._handle_get_context(
        {"message_id": token, "before": 500, "after": 500}
    ))
    assert len(wide["messages"]) == 2 * history_mcp.CONTEXT_MAX + 1


async def test_get_context_refuses_ids_from_other_conversations(fake_firestore, user_id, request_ctx):
    foreign_doc = await seed(fake_firestore, "sam's message", at=_ago(hours=1), user_id=user_id, agent_id=OTHER_ID)

    with pytest.raises(ValueError, match="not in your history"):
        await history_mcp._handle_get_context(
            {"message_id": _encode_message_id(OTHER_ID, user_id, foreign_doc)}
        )
    # Re-labelling the token with the caller's id still finds nothing: the
    # document lives in Sam's conversation, not Maggie's.
    with pytest.raises(ValueError, match="No message with that id"):
        await history_mcp._handle_get_context(
            {"message_id": _encode_message_id(CALLER_ID, user_id, foreign_doc)}
        )
    with pytest.raises(ValueError, match="not a message id from this server"):
        await history_mcp._handle_get_context({"message_id": "garbage"})


async def test_get_context_respects_the_character_cap(fake_firestore, user_id, request_ctx):
    ids = []
    for i in range(21):
        ids.append(await seed(fake_firestore, f"{i} " + ("y" * 900), at=_ago(minutes=21 - i), user_id=user_id))
    token = _encode_message_id(CALLER_ID, user_id, ids[10])

    raw = await history_mcp._handle_get_context({"message_id": token, "before": 10, "after": 10})
    assert len(raw) <= RESPONSE_CHAR_CAP
    result = json.loads(raw)
    assert result["truncated"] is True
    assert any(m["message_id"] == token for m in result["messages"])
    assert "fewer with before/after" in result["note"]


# ---- get_messages ----


async def test_get_messages_pages_a_full_day_under_the_cap(fake_firestore, user_id, request_ctx):
    local_now = datetime.now(ZoneInfo(TZ))
    day = (local_now - timedelta(days=1)).date()
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo(TZ)).astimezone(UTC)
    for i in range(150):
        await seed(
            fake_firestore, f"line {i:03d} " + ("z" * 150), at=midnight + timedelta(minutes=5 * i),
            user_id=user_id, direction="inbound" if i % 2 == 0 else "outbound",
        )
    # Noise outside the day must not appear.
    await seed(fake_firestore, "day after", at=midnight + timedelta(days=1, hours=1), user_id=user_id)

    collected: list[str] = []
    cursor = None
    pages = 0
    while True:
        raw = await history_mcp._handle_get_messages({
            "user": "Jonathan Cavell", "start": day.isoformat(), "end": day.isoformat(),
            "cursor": cursor, "limit": 100,
        })
        assert len(raw) <= RESPONSE_CHAR_CAP
        page = json.loads(raw)
        pages += 1
        collected.extend(m["text"] for m in page["messages"])
        if page["next_cursor"] is None:
            break
        assert page["note"]
        cursor = page["next_cursor"]

    assert pages > 1
    assert len(collected) == 150
    assert collected == sorted(collected)
    assert len(set(collected)) == 150
    assert not any(t == "day after" for t in collected)


async def test_get_messages_validation(fake_firestore, user_id, request_ctx):
    with pytest.raises(ValueError, match="start and end are required"):
        await history_mcp._handle_get_messages({"user": "Jonathan Cavell", "start": "2026-09-01"})
    with pytest.raises(ValueError, match="start must not be after end"):
        await history_mcp._handle_get_messages(
            {"user": "Jonathan Cavell", "start": "2026-09-02", "end": "2026-09-01"}
        )
    with pytest.raises(ValueError, match="cursor must be"):
        await history_mcp._handle_get_messages(
            {"user": "Jonathan Cavell", "start": "2026-09-01", "end": "2026-09-02", "cursor": "nope"}
        )


async def test_get_messages_marks_scheduled_and_attachments(fake_firestore, user_id, request_ctx):
    at = _ago(hours=1)
    await fake_firestore.append_message(LoggedMessage(
        agent_id=CALLER_ID, user_id=user_id, platform="slack", direction="inbound",
        author="morning brief", text="Summarise overnight.", kind="scheduled", created_at=at,
    ))
    await fake_firestore.append_message(LoggedMessage(
        agent_id=CALLER_ID, user_id=user_id, platform="slack", direction="inbound",
        author="Jonathan Cavell", text="look at this", kind="live", created_at=at + timedelta(minutes=1),
        attachments=["[image: image/png]"],
    ))
    start = (at - timedelta(hours=1)).isoformat()
    end = datetime.now(UTC).isoformat()

    result = json.loads(await history_mcp._handle_get_messages(
        {"user": "Jonathan Cavell", "start": start, "end": end}
    ))
    first, second = result["messages"]
    assert first["kind"] == "scheduled" and first["author"] == "morning brief"
    assert "kind" not in second
    assert second["attachments"] == ["[image: image/png]"]
    assert result["next_cursor"] is None


# ---- tool registration ----


def test_three_tools_are_registered():
    assert [t.name for t in history_mcp.TOOLS] == ["search_history", "get_context", "get_messages"]
