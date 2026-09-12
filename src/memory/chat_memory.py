"""
DocuMind Chat Memory
-----------------------
Redis-backed short-term conversation memory, keyed by chat_session_id.

Each session's turns are stored as a Redis list of JSON-encoded
{"role": "user"|"assistant", "content": "..."} objects, in chronological
order. The list is trimmed to the most recent N turns and refreshed
with a TTL on every write, so idle sessions expire automatically.

This is the "hot" cache used to build LLM prompt context. The durable,
permanent record of every message lives in Postgres (ChatSession /
Message tables) — Redis is not the source of truth, it's a fast recent
window.

Redis is OPTIONAL. If REDIS_HOST is not configured (or connection fails),
all memory functions silently no-op and chat works without context carry-over.
The durable Postgres record of messages is always written regardless.
"""


from __future__ import annotations

import json
import uuid
from typing import Dict, List, Optional

import redis

from src.utils.config import get_settings
from src.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

_client: "Optional[redis.Redis]" = None
_redis_unavailable: bool = False  # flipped True once we confirm Redis is not reachable


def _is_redis_configured() -> bool:
    """Return True only when REDIS_HOST is explicitly set (non-default/non-empty)."""
    host = settings.redis.redis_host
    return bool(host) and host not in ("", "localhost", "127.0.0.1")


def get_redis_client() -> "Optional[redis.Redis]":
    """
    Return a process-wide Redis client, or None if Redis is not configured /
    not reachable. Failures are logged once and then suppressed so that the
    API continues to serve requests without chat memory.
    """
    global _client, _redis_unavailable

    if _redis_unavailable:
        return None

    if _client is not None:
        return _client

    if not _is_redis_configured():
        logger.warning(
            "Redis not configured (REDIS_HOST is localhost/empty). "
            "Chat memory disabled — conversations will have no context carry-over."
        )
        _redis_unavailable = True
        return None

    try:
        client = redis.Redis(
            host=settings.redis.redis_host,
            port=settings.redis.redis_port,
            password=settings.redis.redis_password or None,
            ssl=settings.redis.redis_ssl,
            decode_responses=True,
            protocol=2,
            socket_connect_timeout=3,  # fail fast if Redis is unreachable
        )
        client.ping()  # verify the connection right away
        _client = client
        logger.info("Redis connected", host=settings.redis.redis_host, port=settings.redis.redis_port)
    except Exception as exc:
        logger.warning(
            "Redis connection failed — chat memory disabled.",
            error=str(exc),
            host=settings.redis.redis_host,
        )
        _redis_unavailable = True
        return None

    return _client


def _key(session_id: uuid.UUID) -> str:
    return f"chat:{session_id}:turns"


def append_turn(session_id: uuid.UUID, role: str, content: str) -> None:
    """Append one turn to a session's Redis history. No-ops silently if Redis is unavailable."""
    client = get_redis_client()
    if client is None:
        return

    key = _key(session_id)
    entry = json.dumps({"role": role, "content": content})
    try:
        client.rpush(key, entry)
        # Keep only the most recent N turns (each Q+A pair = 2 entries)
        max_entries = settings.redis.redis_max_turns * 2
        client.ltrim(key, -max_entries, -1)
        client.expire(key, settings.redis.redis_chat_ttl_seconds)
    except Exception as exc:
        logger.warning("Redis write failed — skipping memory update.", error=str(exc))


def get_recent_history(session_id: uuid.UUID) -> List[Dict[str, str]]:
    """Return this session's recent turns. Returns empty list if Redis is unavailable."""
    client = get_redis_client()
    if client is None:
        return []

    try:
        raw_entries = client.lrange(_key(session_id), 0, -1)
        return [json.loads(entry) for entry in raw_entries]
    except Exception as exc:
        logger.warning("Redis read failed — returning empty history.", error=str(exc))
        return []


def clear_session(session_id: uuid.UUID) -> None:
    """Delete a session's cached history. No-ops silently if Redis is unavailable."""
    client = get_redis_client()
    if client is None:
        return

    try:
        client.delete(_key(session_id))
    except Exception as exc:
        logger.warning("Redis delete failed.", error=str(exc))