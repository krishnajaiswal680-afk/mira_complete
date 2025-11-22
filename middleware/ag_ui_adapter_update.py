# ag_ui_adapter.py
"""
FlightOps AG-UI Adapter (Redis-enabled)

Features:
- Loads conversation history from middleware (Redis-based) at run start
- Caches MCP tool results in Redis (tool_name + args) for configurable TTL
- Emits AG-UI compatible SSE events (plain JSON 'data: ...\n\n')
- Best-effort: continues to work if Redis is unavailable
- Uses FlightOpsMCPClient (client.py) for planning, tool invocation, summarization
"""

import os
import json
import asyncio
import uuid
import logging
from typing import AsyncGenerator, Any, Dict

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

# existing MCP client
from client import FlightOpsMCPClient

# middleware helpers (history + redis client)
# Make sure middleware/main.py exists and is importable (I provided one earlier)
from middleware.main import (
    init_redis_client,
    load_history_messages,
    append_turn_to_history,
    make_history_key,
)

# setup logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("flightops.agui")

app = FastAPI(title="FlightOps — AG-UI Adapter (Redis Cache)")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# MCP client (shared)
mcp_client = FlightOpsMCPClient()

# Redis client from middleware
redis_client = init_redis_client()

# Cache TTL (seconds) for tool results (configurable)
REDIS_CACHE_TTL_SECONDS = int(os.getenv("REDIS_CACHE_TTL_SECONDS", str(60 * 5)))  # default 5 minutes

# Cache key namespace (consistent with middleware)
CACHE_NAMESPACE = os.getenv("CACHE_NAMESPACE", "nonprod")
CACHE_PROJECT = os.getenv("CACHE_PROJECT", "flightops")
CACHE_MODULE = os.getenv("CACHE_MODULE", "mcp_adapter")


def make_tool_cache_key(tool_name: str, args: Dict[str, Any]) -> str:
    """
    Deterministic cache key for a tool invocation.
    Uses sorted JSON of args to keep ordering stable.
    """
    try:
        args_key = json.dumps(args or {}, sort_keys=True, default=str)
    except Exception:
        args_key = json.dumps(str(args))
    # keep key reasonably sized; Redis allows long keys but avoid unnecessarily long content
    return f"{CACHE_NAMESPACE}:{CACHE_PROJECT}:{CACHE_MODULE}:tool:{tool_name}:{args_key}"


# SSE helper
def sse_event(data: dict) -> str:
    payload = json.dumps(data, default=str)
    return f"data: {payload}\n\n"


async def ensure_mcp_connected():
    if not mcp_client.session:
        await mcp_client.connect()


@app.on_event("startup")
async def startup_event():
    try:
        await ensure_mcp_connected()
    except Exception as e:
        logger.warning(f"Could not preconnect to MCP: {e}")


@app.get("/")
async def root():
    return {"message": "FlightOps AG-UI Adapter is running", "status": "ok"}


@app.get("/health")
async def health():
    try:
        await ensure_mcp_connected()
        redis_ok = None
        if redis_client:
            try:
                redis_ok = redis_client.ping()
            except Exception:
                redis_ok = False
        return {"status": "healthy", "mcp_connected": True, "redis": redis_ok}
    except Exception as e:
        return {"status": "unhealthy", "mcp_connected": False, "error": str(e)}


@app.post("/agent", response_class=StreamingResponse)
async def run_agent(request: Request):
    """
    AG-UI-compatible /agent endpoint with Redis caching + history.

    Expected JSON body (AG-UI format):
      - thread_id (optional)
      - run_id (optional)
      - messages: list (we prefer last user message)
      - tools/context (optional)
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # derive ids
    thread_id = body.get("thread_id") or str(uuid.uuid4())
    run_id = body.get("run_id") or str(uuid.uuid4())
    messages = body.get("messages", []) or []
    tools = body.get("tools", []) or []

    # prefer last user message as query
    user_query = ""
    if messages:
        last = messages[-1]
        if isinstance(last, dict) and last.get("role") == "user":
            user_query = last.get("content", "")
        elif isinstance(last, str):
            user_query = last

    if not user_query or not user_query.strip():
        raise HTTPException(status_code=400, detail="No user query in messages payload")

    # session id for history use (we use thread_id or run_id or default)
    session_id = body.get("session_id") or thread_id or run_id or "default-session"

    async def event_stream() -> AsyncGenerator[str, None]:
        # 1) RUN_STARTED
        yield sse_event({"type": "RUN_STARTED", "thread_id": thread_id, "run_id": run_id})

        # ensure MCP connected
        try:
            await ensure_mcp_connected()
        except Exception as e:
            yield sse_event({"type": "RUN_ERROR", "error": str(e)})
            return

        # 2) Build messages for planning: include conversation history loaded from Redis (if any)
        yield sse_event({"type": "TEXT_MESSAGE_CONTENT", "content": "Generating tool plan..."})

        # load history messages (list[dict]) — defensive
        history_messages = []
        try:
            history_messages = load_history_messages(session_id)
            if history_messages:
                logger.info(f"Loaded {len(history_messages)} history messages for session {session_id}")
        except Exception as e:
            logger.warning(f"Failed to load history for session {session_id}: {e}")
            history_messages = []

        # Build messages to pass to LLM planner (keep original messages structure but prepend history)
        planning_messages = []
        # system prompt and other context may exist in body; keep them if present
        if isinstance(messages, list) and messages:
            # keep everything but ensure history precedes user messages
            # We'll append history first then existing messages, to give LLM context
            planning_messages.extend(history_messages)
            planning_messages.extend(messages)
        else:
            planning_messages.extend(history_messages)
            # if no messages, just include the user's query as user role
            planning_messages.append({"role": "user", "content": user_query})

        # call planner (synchronous wrapper in client.py) in threadpool
        loop = asyncio.get_event_loop()
        try:
            plan_data = await loop.run_in_executor(None, mcp_client.plan_tools, user_query)
            plan = plan_data.get("plan", [])
        except Exception as e:
            logger.exception("Planner call failed")
            yield sse_event({"type": "RUN_ERROR", "error": f"Planner failed: {e}"})
            return

        # emit plan snapshot
        yield sse_event({"type": "STATE_SNAPSHOT", "snapshot": {"plan": plan}})

        if not plan:
            yield sse_event({"type": "TEXT_MESSAGE_CONTENT", "content": "LLM did not produce a valid plan."})
            yield sse_event({"type": "RUN_FINISHED", "run_id": run_id})
            return

        results = []

        # 3) Execute steps sequentially (with caching)
        for step_index, step in enumerate(plan):
            tool_name = step.get("tool")
            args = step.get("arguments", {}) or {}
            tool_call_id = f"toolcall-{uuid.uuid4().hex[:8]}"

            # emit tool call start / args / end
            yield sse_event({
                "type": "TOOL_CALL_START",
                "toolCallId": tool_call_id,
                "toolCallName": tool_name,
                "parentMessageId": None
            })

            args_json = json.dumps(args, default=str)
            yield sse_event({
                "type": "TOOL_CALL_ARGS",
                "toolCallId": tool_call_id,
                "delta": args_json
            })

            yield sse_event({
                "type": "TOOL_CALL_END",
                "toolCallId": tool_call_id
            })

            # Try cache read
            cache_key = make_tool_cache_key(tool_name, args)
            cached = None
            if redis_client:
                try:
                    cached = redis_client.get(cache_key)
                except Exception as e:
                    logger.warning(f"Redis read error for key={cache_key}: {e}")
                    cached = None

            if cached:
                try:
                    tool_result = json.loads(cached)
                    logger.info(f"Cache hit for {tool_name}")
                except Exception:
                    logger.warning("Corrupted cache entry - ignoring")
                    tool_result = {"error": "corrupted cache"}
            else:
                # call MCP tool asynchronously (invoke_tool is async)
                try:
                    tool_result = await mcp_client.invoke_tool(tool_name, args)
                except Exception as exc:
                    logger.exception("Tool invocation failed")
                    tool_result = {"error": str(exc)}

                # write to cache best-effort
                if redis_client:
                    try:
                        # JSON-stringify; ensure serializable
                        redis_client.set(cache_key, json.dumps(tool_result, default=str), ex=REDIS_CACHE_TTL_SECONDS)
                        logger.info(f"Cached result for {tool_name} (key={cache_key})")
                    except Exception as e:
                        logger.warning(f"Redis write failed for key={cache_key}: {e}")

            # Emit TOOL_CALL_RESULT (role: tool)
            tool_message = {
                "id": f"msg-{uuid.uuid4().hex[:8]}",
                "role": "tool",
                "content": json.dumps(tool_result, default=str),
                "tool_call_id": tool_call_id,
            }
            yield sse_event({
                "type": "TOOL_CALL_RESULT",
                "message": tool_message
            })

            results.append({tool_name: tool_result})

            # step finished
            yield sse_event({
                "type": "STEP_FINISHED",
                "step_index": step_index,
                "tool": tool_name
            })

        # 4) Summarize results via LLM (synchronous summarize_results)
        yield sse_event({"type": "TEXT_MESSAGE_CONTENT", "content": "Summarizing results..."})

        try:
            summary = await loop.run_in_executor(None, mcp_client.summarize_results, user_query, plan, results)
            assistant_text = summary.get("summary", "") if isinstance(summary, dict) else str(summary)
        except Exception as e:
            assistant_text = f"Failed to summarize results: {e}"
            logger.warning(assistant_text)

        # Stream assistant content as one chunk (frontend will handle chunking if preferred)
        yield sse_event({
            "type": "TEXT_MESSAGE_CONTENT",
            "message": {
                "id": f"msg-{uuid.uuid4().hex[:8]}",
                "role": "assistant",
                "content": assistant_text
            }
        })

        # Append the turn to Redis history (best-effort)
        try:
            append_turn_to_history(session_id, user_query, assistant_text)
            logger.info(f"Saved turn to history for session={session_id}")
        except Exception as e:
            logger.warning(f"Failed to append turn to history: {e}")

        # Final snapshot + RUN_FINISHED
        yield sse_event({"type": "STATE_SNAPSHOT", "snapshot": {"plan": plan, "results": results}})
        yield sse_event({"type": "RUN_FINISHED", "run_id": run_id})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# Expose an endpoint to clear history for a session (admin / debug)
@app.post("/admin/clear_history")
async def admin_clear_history(session_id: str):
    try:
        from middleware.main import clear_history
        ok = clear_history(session_id)
        return {"ok": ok}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting FlightOps AG-UI Adapter on http://0.0.0.0:8001")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8001")), log_level="info")
