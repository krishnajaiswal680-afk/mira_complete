# middleware/main.py
"""
Redis middleware for FlightOps — history + helper utilities.

Place this file at: <project_root>/middleware/main.py

This module provides:
- Redis client initialization (Azure credential provider optional)
- make_history_key(session_id)
- append_turn_to_history(session_id, user_msg, assistant_msg)
- load_history_messages(session_id, max_messages=...)
- small helper: clear_history(session_id)  (admin/debug)

It loads environment from the repository root .env (one level up from middleware).
"""

import os
import json
import logging
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv

# If you have redis_entraid credential provider available (Azure Enterprise)
try:
    from redis_entraid.cred_provider import create_from_service_principal
except Exception:
    create_from_service_principal = None

# redis-py
try:
    from redis import Redis
except Exception as e:
    Redis = None

# load .env from project root (one level up from this middleware folder)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
ENV_PATH = os.path.join(ROOT, ".env")
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)
else:
    # Also try loading default .env if present
    load_dotenv()

# Logging
logger = logging.getLogger("flightops.middleware.redis")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# Config (defaults can be overridden in .env)
REDIS_HOST = os.getenv("REDIS_HOST", "").strip() or None
REDIS_PORT = int(os.getenv("REDIS_PORT", "10000"))
REDIS_CLIENT_ID = os.getenv("REDIS_CLIENT_ID")
REDIS_CLIENT_SECRET = os.getenv("REDIS_CLIENT_SECRET")
REDIS_TENANT_ID = os.getenv("REDIS_TENANT_ID")
DISABLE_REDIS_SSL_VERIFY = os.getenv("DISABLE_REDIS_SSL_VERIFY", "0") == "1"

# Key namespace
NAMESPACE = os.getenv("CACHE_NAMESPACE", "nonprod")
PROJECT = os.getenv("CACHE_PROJECT", "flightops")
MODULE = os.getenv("CACHE_MODULE", "mcp_adapter")

# History settings
HISTORY_TTL_SECONDS = int(os.getenv("HISTORY_TTL_SECONDS", str(60 * 60 * 24)))  # default 1 day
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))

# Redis client will be created below
redis_client: Optional[Redis] = None

def init_redis_client() -> Optional[Redis]:
    global redis_client
    if redis_client is not None:
        return redis_client

    if not REDIS_HOST:
        logger.info("REDIS_HOST not set — running without Redis")
        return None

    if Redis is None:
        logger.error("redis package not installed. Install `redis` to enable caching.")
        return None

    try:
        if create_from_service_principal and REDIS_CLIENT_ID and REDIS_CLIENT_SECRET and REDIS_TENANT_ID:
            try:
                cred = create_from_service_principal(REDIS_CLIENT_ID, REDIS_CLIENT_SECRET, REDIS_TENANT_ID)
                redis_client = Redis(
                    host=REDIS_HOST,
                    port=REDIS_PORT,
                    ssl=True,
                    credential_provider=cred,
                    decode_responses=True,
                    socket_timeout=5,
                    socket_connect_timeout=5,
                    ssl_cert_reqs=None if DISABLE_REDIS_SSL_VERIFY else "required",
                    ssl_check_hostname=False if DISABLE_REDIS_SSL_VERIFY else True,
                )
                logger.info("Initialized Redis client with Azure credential provider")
            except Exception as e:
                logger.warning("Failed to init Azure credential provider Redis client: %s", e)
                redis_client = None
        else:
            # fallback plain redis (useful for local dev or docker)
            redis_client = Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
            )
            logger.info("Initialized plain Redis client (no credential provider)")

        # try ping (best-effort — non-fatal)
        try:
            ok = redis_client.ping()
            logger.info("Redis ping -> %s", ok)
        except Exception as e:
            logger.warning("Redis ping failed (will continue without cache): %s", e)

        return redis_client

    except Exception as e:
        logger.exception("Could not initialize Redis client: %s", e)
        redis_client = None
        return None

# initialize on import (optional)
init_redis_client()


# ---------------- Key helpers ----------------
def make_history_key(session_id: str) -> str:
    """
    <namespace>:<project>:<module>:history:<session_id>
    Keeps same shape as your weather reference.
    """
    # sanitize session id a bit
    sid = str(session_id).replace(" ", "_")
    return f"{NAMESPACE}:{PROJECT}:{MODULE}:history:{sid}"


# ---------------- History helpers ----------------
def append_turn_to_history(session_id: str, user_msg: str, assistant_msg: str) -> None:
    """
    Store a user+assistant turn as two entries in a Redis list:
      RPUSH key user_entry assistant_entry
    Sets TTL on the key (HISTORY_TTL_SECONDS).
    Defensive — no exception bubbles up.
    """
    r = init_redis_client()
    if not r:
        logger.debug("append_turn_to_history: redis not configured; skipping")
        return

    key = make_history_key(session_id)
    user_entry = json.dumps({"role": "user", "content": user_msg}, ensure_ascii=False)
    assistant_entry = json.dumps({"role": "assistant", "content": assistant_msg}, ensure_ascii=False)

    try:
        pipe = r.pipeline()
        pipe.rpush(key, user_entry, assistant_entry)
        pipe.expire(key, HISTORY_TTL_SECONDS)
        pipe.execute()
        logger.debug("Appended turn to history key=%s", key)
    except Exception as e:
        logger.warning("append_turn_to_history failed for key=%s: %s", key, e)


def load_history_messages(session_id: str, max_messages: int = MAX_HISTORY_MESSAGES) -> List[Dict[str, Any]]:
    """
    Load last N JSON messages from Redis and return as list[dict].
    Defensive: returns [] on any error or when redis not present.
    """
    r = init_redis_client()
    if not r:
        logger.debug("load_history_messages: redis not configured; returning empty list")
        return []

    key = make_history_key(session_id)
    try:
        length = r.llen(key)
    except Exception as e:
        logger.warning("Redis llen failed for key=%s: %s", key, e)
        return []

    if not length:
        return []

    start = max(0, length - max_messages)
    try:
        raw_msgs = r.lrange(key, start, -1)
    except Exception as e:
        logger.warning("Redis lrange failed for key=%s: %s", key, e)
        return []

    messages = []
    for raw in raw_msgs:
        try:
            messages.append(json.loads(raw))
        except Exception:
            # ignore bad entries
            continue
    logger.debug("Loaded %d history messages for key=%s", len(messages), key)
    return messages


def clear_history(session_id: str) -> bool:
    """
    Delete the history key — useful for testing/admin.
    Returns True if deleted or key didn't exist, False on error.
    """
    r = init_redis_client()
    if not r:
        logger.debug("clear_history: redis not configured")
        return False

    key = make_history_key(session_id)
    try:
        r.delete(key)
        logger.info("Cleared history key=%s", key)
        return True
    except Exception as e:
        logger.warning("clear_history failed for key=%s: %s", key, e)
        return False


# ---------------- Quick CLI test when run directly ----------------
if __name__ == "__main__":
    # quick smoke test
    sid = "test-session-1"
    print("Using .env at:", ENV_PATH)
    print("Redis host:", REDIS_HOST, "port:", REDIS_PORT)
    r = init_redis_client()
    if not r:
        print("Redis client not configured or failed to init.")
    else:
        print("Redis ping:", r.ping())
        print("Clearing test key:", make_history_key(sid))
        clear_history(sid)
        append_turn_to_history(sid, "hello user", "hello assistant")
        msgs = load_history_messages(sid, max_messages=10)
        print("Loaded messages:", msgs)
