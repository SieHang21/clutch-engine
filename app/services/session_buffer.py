"""
Clutch (ক্ল্যাচ) - Session Accumulator & Debounce Buffer
Handles multi-message bursts from customers across a sliding window (default 75s TTL),
aggregates fragmented order messages, detects order completeness, and provides
an Admin Presence Sensor to mute the bot for 45 minutes during human takeover.
Supports Redis with thread-safe in-memory fallback.
"""

from typing import List, Dict, Any, Optional, Tuple
import os
import time
import threading
import logging
from app.core.sanitizer import (
    normalize_bd_phone,
    resolve_zone,
    sanitize_order_payload,
)

logger = logging.getLogger(__name__)

# Constants as defined in Clutch Master Specification
DEFAULT_DEBOUNCE_TTL = 75          # 75-second sliding debounce window
DEFAULT_TAKEOVER_TTL = 2700        # 45 minutes (2700 seconds) admin takeover mute
MAX_MESSAGING_WINDOW = 85800       # 23 hours 50 minutes (85,800 seconds) safety window


def is_order_complete(text: str) -> bool:
    """
    Check if the aggregated text contains both a valid Bangladeshi phone number
    and a recognized delivery zone / address keyword.
    """
    if not text:
        return False
    phones = normalize_bd_phone(text)
    if not phones:
        return False
    zone_res = resolve_zone(text)
    return zone_res.matched_keyword is not None


def is_within_messaging_window(last_message_timestamp: float, max_seconds: int = MAX_MESSAGING_WINDOW) -> bool:
    """
    Meta 24-Hour Messaging Window Safety Guard:
    Checks if the time elapsed since the customer's last interaction is within the 23h 50m window.
    After 23h 50m, automated replies are suppressed to prevent Meta policy violations.
    """
    if not last_message_timestamp or last_message_timestamp <= 0:
        return False
    elapsed = time.time() - float(last_message_timestamp)
    return 0 <= elapsed <= max_seconds


# -------------------------------------------------------------------------
# In-Memory Session Buffer (Thread-safe Fallback)
# -------------------------------------------------------------------------
class InMemorySessionBuffer:
    """
    Thread-safe in-memory sliding window accumulator used when Redis is unavailable.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # session_key -> {"messages": [str, ...], "expires_at": float, "last_updated": float}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        # takeover_key -> expires_at: float
        self._takeovers: Dict[str, float] = {}

    def _cleanup_expired(self, now: float):
        """Purge expired entries."""
        expired_sessions = [k for k, v in self._sessions.items() if now > v.get("expires_at", 0)]
        for k in expired_sessions:
            del self._sessions[k]

        expired_takeovers = [k for k, exp in self._takeovers.items() if now > exp]
        for k in expired_takeovers:
            del self._takeovers[k]

    def append_message(self, session_key: str, text: str, ttl: int = DEFAULT_DEBOUNCE_TTL) -> List[str]:
        with self._lock:
            now = time.time()
            self._cleanup_expired(now)

            if session_key not in self._sessions or now > self._sessions[session_key]["expires_at"]:
                self._sessions[session_key] = {
                    "messages": [],
                    "expires_at": now + ttl,
                    "last_updated": now,
                }
            else:
                # Sliding window: reset TTL on every incoming message
                self._sessions[session_key]["expires_at"] = now + ttl
                self._sessions[session_key]["last_updated"] = now

            self._sessions[session_key]["messages"].append(text)
            return list(self._sessions[session_key]["messages"])

    def get_messages(self, session_key: str) -> List[str]:
        with self._lock:
            now = time.time()
            self._cleanup_expired(now)
            session = self._sessions.get(session_key)
            if not session:
                return []
            return list(session["messages"])

    def flush_session(self, session_key: str) -> List[str]:
        with self._lock:
            session = self._sessions.pop(session_key, None)
            if not session:
                return []
            return list(session["messages"])

    def set_takeover(self, takeover_key: str, ttl: int = DEFAULT_TAKEOVER_TTL) -> None:
        with self._lock:
            now = time.time()
            self._cleanup_expired(now)
            self._takeovers[takeover_key] = now + ttl

    def is_takeover_active(self, takeover_key: str) -> bool:
        with self._lock:
            now = time.time()
            self._cleanup_expired(now)
            exp = self._takeovers.get(takeover_key, 0)
            return now < exp

    def clear_takeover(self, takeover_key: str) -> None:
        with self._lock:
            self._takeovers.pop(takeover_key, None)

    def clear_all(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._takeovers.clear()


# -------------------------------------------------------------------------
# Redis Session Buffer Backend
# -------------------------------------------------------------------------
class RedisSessionBufferBackend:
    """
    Redis implementation of the sliding window accumulator and admin takeover sensor.
    """

    def __init__(self, redis_client):
        self.r = redis_client

    def append_message(self, session_key: str, text: str, ttl: int = DEFAULT_DEBOUNCE_TTL) -> List[str]:
        pipe = self.r.pipeline()
        pipe.rpush(session_key, text)
        pipe.expire(session_key, ttl)
        pipe.lrange(session_key, 0, -1)
        results = pipe.execute()
        raw_list = results[2]
        return [item.decode("utf-8") if isinstance(item, bytes) else str(item) for item in raw_list]

    def get_messages(self, session_key: str) -> List[str]:
        raw_list = self.r.lrange(session_key, 0, -1)
        return [item.decode("utf-8") if isinstance(item, bytes) else str(item) for item in raw_list]

    def flush_session(self, session_key: str) -> List[str]:
        pipe = self.r.pipeline()
        pipe.lrange(session_key, 0, -1)
        pipe.delete(session_key)
        results = pipe.execute()
        raw_list = results[0]
        return [item.decode("utf-8") if isinstance(item, bytes) else str(item) for item in raw_list]

    def set_takeover(self, takeover_key: str, ttl: int = DEFAULT_TAKEOVER_TTL) -> None:
        self.r.set(takeover_key, 1, ex=ttl)

    def is_takeover_active(self, takeover_key: str) -> bool:
        return bool(self.r.exists(takeover_key))

    def clear_takeover(self, takeover_key: str) -> None:
        self.r.delete(takeover_key)

    def clear_all(self) -> None:
        # Flush current DB in test environments
        pass


# -------------------------------------------------------------------------
# Unified Session Accumulator Service
# -------------------------------------------------------------------------
class SessionBuffer:
    """
    High-level Session Accumulator managing customer message bursts,
    sliding window TTL debounce, completeness detection, and human admin takeover.
    Automatically connects to Redis or falls back to thread-safe in-memory buffer.
    """

    def __init__(self, redis_client=None, redis_url: Optional[str] = None):
        self._memory_backend = InMemorySessionBuffer()
        self._redis_backend: Optional[RedisSessionBufferBackend] = None

        if redis_client is not None:
            self._redis_backend = RedisSessionBufferBackend(redis_client)
        elif redis_url or os.getenv("REDIS_URL"):
            url = redis_url or os.getenv("REDIS_URL")
            try:
                import redis
                client = redis.Redis.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5)
                client.ping()
                self._redis_backend = RedisSessionBufferBackend(client)
                logger.info("SessionBuffer connected to Redis successfully.")
            except Exception as e:
                logger.warning("Redis unavailable (%s). Falling back to InMemorySessionBuffer.", e)
                self._redis_backend = None
        else:
            # Default to thread-safe in-memory buffer when REDIS_URL is not configured
            self._redis_backend = None

    @property
    def backend(self):
        return self._redis_backend if self._redis_backend is not None else self._memory_backend

    @property
    def is_using_redis(self) -> bool:
        return self._redis_backend is not None

    def _format_session_key(self, sender_id: str, page_id: Optional[str] = None) -> str:
        """
        Builds Redis key according to spec: `session:{page_id}:{sender_id}` or `session:{sender_id}`.
        """
        if page_id:
            return f"session:{page_id}:{sender_id}"
        return f"session:{sender_id}"

    def _format_takeover_keys(self, sender_id: str, page_id: Optional[str] = None) -> List[str]:
        """
        Returns keys for checking/setting admin presence to satisfy both spec conventions:
        - `takeover:{page_id}:{sender_id}`
        - `admin_active:{sender_id}`
        """
        keys = [f"admin_active:{sender_id}"]
        if page_id:
            keys.append(f"takeover:{page_id}:{sender_id}")
        return keys

    def add_message(
        self,
        sender_id: str,
        text: str,
        page_id: Optional[str] = None,
        ttl: int = DEFAULT_DEBOUNCE_TTL,
    ) -> Dict[str, Any]:
        """
        Append an incoming message to the customer's session buffer and reset the sliding TTL.
        Evaluates whether a complete phone + address pattern is ready for normalization.
        """
        session_key = self._format_session_key(sender_id, page_id)
        messages = self.backend.append_message(session_key, text, ttl=ttl)
        combined_text = " ".join(messages)

        complete = is_order_complete(combined_text)
        sanitized = sanitize_order_payload(combined_text) if complete else None

        return {
            "session_key": session_key,
            "sender_id": sender_id,
            "page_id": page_id,
            "messages": messages,
            "message_count": len(messages),
            "combined_text": combined_text,
            "is_complete": complete,
            "order": sanitized,
            "ttl": ttl,
        }

    def get_buffered_messages(self, sender_id: str, page_id: Optional[str] = None) -> List[str]:
        """Retrieve current unpopped messages for the session."""
        session_key = self._format_session_key(sender_id, page_id)
        return self.backend.get_messages(session_key)

    def flush_session(self, sender_id: str, page_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Pop all buffered messages once debounce window expires, concatenate into a single
        text block, and pass to the core normalizer engine.
        """
        session_key = self._format_session_key(sender_id, page_id)
        messages = self.backend.flush_session(session_key)
        combined_text = " ".join(messages) if messages else ""

        order_result = sanitize_order_payload(combined_text) if combined_text else None

        return {
            "session_key": session_key,
            "sender_id": sender_id,
            "page_id": page_id,
            "flushed": True,
            "messages": messages,
            "message_count": len(messages),
            "combined_text": combined_text,
            "order": order_result,
        }

    def set_admin_takeover(
        self,
        sender_id: str,
        page_id: Optional[str] = None,
        ttl: int = DEFAULT_TAKEOVER_TTL,
    ) -> None:
        """
        Flag user in Redis for 45 minutes (2700s) to mute automated responses
        when a human page admin manually takes over the conversation.
        """
        for key in self._format_takeover_keys(sender_id, page_id):
            self.backend.set_takeover(key, ttl=ttl)

    def is_admin_takeover_active(self, sender_id: str, page_id: Optional[str] = None) -> bool:
        """
        Check if automated responses should be suppressed due to active human admin takeover.
        """
        for key in self._format_takeover_keys(sender_id, page_id):
            if self.backend.is_takeover_active(key):
                return True
        return False

    def clear_admin_takeover(self, sender_id: str, page_id: Optional[str] = None) -> None:
        """Clear the admin takeover flag (re-enabling bot responses)."""
        for key in self._format_takeover_keys(sender_id, page_id):
            self.backend.clear_takeover(key)


# Global default singleton instance
default_session_buffer = SessionBuffer()
