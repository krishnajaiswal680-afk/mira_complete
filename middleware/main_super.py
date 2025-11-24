import os
import json
import asyncio
import time
import uuid
from typing import AsyncGenerator, List
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from client import FlightOpsMCPClient

# ----------------- Redis CONFIG -----------------
import logging
from redis import Redis
from redis_entraid.cred_provider import create_from_service_principal

logger = logging.getLogger(__name__)

REDIS_HOST = "occh-uamr01.centralindia.redis.azure.net"
REDIS_PORT = 10000

REDIS_CLIENT_ID = os.getenv("REDIS_CLIENT_ID")
REDIS_CLIENT_SECRET = os.getenv("REDIS_CLIENT_SECRET")
REDIS_TENANT_ID = os.getenv("REDIS_TENANT_ID")

NAMESPACE = "non-prod"
PROJECT = "occhub"
MODULE = "flight_mcp"

HISTORY_TTL_SECONDS = 60 * 60 * 24
MAX_HISTORY_MESSAGES = 50

# Credential provider
try:
    redis_credential_provider = create_from_service_principal(
        REDIS_CLIENT_ID,
        REDIS_CLIENT_SECRET,
        REDIS_TENANT_ID,
    )
except Exception as e:
    logger.error(f"Failed to create Redis credential provider: {e}")
    redis_credential_provider = None

# DEV disable SSL verification
_dev_disable_ssl_verify = os.getenv("DISABLE_REDIS_SSL_VERIFY", "1") == "1"
if _dev_disable_ssl_verify:
    logger.warning("Redis SSL verification DISABLED (development)")

redis_client = None

# Initialize Redis client
try:
    if redis_credential_provider:
        redis_client = Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            ssl=True,
            credential_provider=redis_credential_provider,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            ssl_cert_reqs=None if _dev_disable_ssl_verify else "required",
            ssl_check_hostname=False if _dev_disable_ssl_verify else True,
        )

        try:
            ok = redis_client.ping()
            logger.info(f"Redis ping → {ok}")
        except Exception as e:
            logger.warning(f"Redis ping failed: {e}")
    else:
        logger.warning("Redis credential provider missing")
except Exception as e:
    logger.error(f"Failed to init Redis client: {e}")
    redis_client = None


# ---- Redis helpers ----
def make_history_key(session_id: str) -> str:
    return f"{NAMESPACE}:{PROJECT}:{MODULE}:history:{session_id}"


def append_turn_to_history(session_id: str, user_msg: str, assistant_msg: str):
    if not redis_client:
        logger.debug("Redis unavailable — skipping save")
        return

    key = make_history_key(session_id)

    user_entry = json.dumps({"role": "user", "content": user_msg}, ensure_ascii=False)
    assistant_entry = json.dumps({"role": "assistant", "content": assistant_msg}, ensure_ascii=False)

    try:
        pipe = redis_client.pipeline()
        pipe.rpush(key, user_entry, assistant_entry)
        pipe.expire(key, HISTORY_TTL_SECONDS)
        pipe.execute()
    except Exception as e:
        logger.warning(f"append_turn_to_history failed → {e}")


def load_history_messages(session_id: str):
    if not redis_client:
        logger.debug("Redis unavailable — skipping load")
        return []

    key = make_history_key(session_id)

    try:
        length = redis_client.llen(key)
    except Exception as e:
        logger.warning(f"Redis llen failed → {e}")
        return []

    if not length:
        return []

    start = max(0, length - MAX_HISTORY_MESSAGES)

    try:
        raw_msgs = redis_client.lrange(key, start, -1)
    except Exception as e:
        logger.warning(f"Redis lrange failed → {e}")
        return []

    results = []
    for raw in raw_msgs:
        try:
            results.append(json.loads(raw))
        except:
            continue

    return results


# ---------------- FASTAPI APP -------------------

app = FastAPI(title="FlightOps — AG-UI Adapter")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

mcp_client = FlightOpsMCPClient()


def sse_event(data: dict) -> str:
    return f"data: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


async def ensure_mcp_connected():
    if not mcp_client.session:
        await mcp_client.connect()


@app.on_event("startup")
async def startup_event():
    try:
        await ensure_mcp_connected()
    except Exception:
        pass


@app.get("/")
async def root():
    return {"message": "FlightOps AG-UI Adapter running", "status": "ok"}


@app.get("/health")
async def health():
    try:
        await ensure_mcp_connected()
        return {"status": "healthy", "mcp_connected": True}
    except Exception as e:
        return {"status": "unhealthy", "mcp_connected": False, "error": str(e)}


def chunk_text(txt: str, max_len: int = 200) -> List[str]:
    txt = txt or ""
    parts = []
    buf = ""

    def flush():
        nonlocal buf
        if buf:
            parts.append(buf)
            buf = ""

    for ch in txt:
        buf += ch
        if ch in ".!?\n" and len(buf) >= max_len // 2:
            flush()
        elif len(buf) >= max_len:
            flush()

    flush()
    return parts


# ---------------------- AGENT ENDPOINT ---------------------------

@app.post("/agent", response_class=StreamingResponse)
async def run_agent(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    thread_id = body.get("thread_id") or f"thread-{uuid.uuid4().hex[:8]}"
    run_id = body.get("run_id") or f"run-{uuid.uuid4().hex[:8]}"
    messages = body.get("messages", [])

    # session_id mapped directly to thread_id
    session_id = thread_id

    # Extract latest user query
    user_query = ""
    if messages:
        last = messages[-1]
        if isinstance(last, dict) and last.get("role") == "user":
            user_query = last.get("content", "")
        elif isinstance(last, str):
            user_query = last

    if not user_query.strip():
        raise HTTPException(status_code=400, detail="No user query found")

    # ---- Load history from Redis ----
    history_messages = load_history_messages(session_id)

    # Build plain-text history to help the planner
    history_text = ""
    for h in history_messages:
        role = h.get("role")
        content = h.get("content")
        history_text += f"{role.upper()}: {content}\n"

    async def event_stream() -> AsyncGenerator[str, None]:
        last_heartbeat = time.time()

        yield sse_event({"type": "RUN_STARTED", "thread_id": thread_id, "run_id": run_id})

        # THINKING
        yield sse_event({
            "type": "STATE_UPDATE",
            "state": {
                "phase": "thinking",
                "progress_pct": 5,
                "message": "🧠 Analyzing your flight query..."
            }
        })

        try:
            await ensure_mcp_connected()
        except Exception as e:
            yield sse_event({"type": "RUN_ERROR", "error": f"MCP connect failed: {e}"})
            return

        loop = asyncio.get_event_loop()

        # PLAN
        yield sse_event({
            "type": "STATE_UPDATE",
            "state": {
                "phase": "thinking",
                "progress_pct": 15,
                "message": "📋 Planning which flight tools to use..."
            }
        })

        try:
            # Pass history + user query to planner
            plan_data = await loop.run_in_executor(
                None,
                mcp_client.plan_tools,
                f"{history_text}\nUSER: {user_query}"
            )
        except Exception as e:
            yield sse_event({"type": "RUN_ERROR", "error": f"Planner error: {e}"})
            return

        plan = plan_data.get("plan", []) if isinstance(plan_data, dict) else []
        planning_usage = plan_data.get("llm_usage", {})

        if planning_usage:
            yield sse_event({
                "type": "TOKEN_USAGE",
                "phase": "planning",
                "usage": planning_usage
            })

        yield sse_event({"type": "STATE_SNAPSHOT", "snapshot": {"plan": plan}})

        if not plan:
            yield sse_event({
                "type": "TEXT_MESSAGE_CONTENT",
                "message": {
                    "id": f"msg-{uuid.uuid4().hex[:8]}",
                    "role": "assistant",
                    "content": "I couldn't generate a valid plan for your query."
                }
            })
            yield sse_event({"type": "STATE_UPDATE", "state": {"phase": "finished", "progress_pct": 100}})
            yield sse_event({"type": "RUN_FINISHED", "run_id": run_id})
            return

        # PROCESSING TOOLS
        yield sse_event({
            "type": "STATE_UPDATE",
            "state": {
                "phase": "processing",
                "progress_pct": 20,
                "message": f"🛠️ Executing {len(plan)} flight tools..."
            }
        })

        results = []
        num_steps = max(1, len(plan))
        per_step = 60.0 / num_steps
        current_progress = 20.0

        for step_index, step in enumerate(plan):
            if await request.is_disconnected():
                return

            tool_name = step.get("tool")
            args = step.get("arguments", {})

            yield sse_event({
                "type": "STATE_UPDATE",
                "state": {
                    "phase": "processing",
                    "progress_pct": round(current_progress),
                    "message": f"🔧 Running {tool_name}..."
                }
            })

            tool_call_id = f"toolcall-{uuid.uuid4().hex[:8]}"

            yield sse_event({"type": "TOOL_CALL_START", "toolCallId": tool_call_id, "toolCallName": tool_name})
            yield sse_event({"type": "TOOL_CALL_ARGS", "toolCallId": tool_call_id, "delta": json.dumps(args)})
            yield sse_event({"type": "TOOL_CALL_END", "toolCallId": tool_call_id})

            try:
                tool_result = await mcp_client.invoke_tool(tool_name, args)
            except Exception as exc:
                tool_result = {"error": str(exc)}

            yield sse_event({
                "type": "TOOL_CALL_RESULT",
                "message": {
                    "id": f"msg-{uuid.uuid4().hex[:8]}",
                    "role": "tool",
                    "content": json.dumps(tool_result),
                    "tool_call_id": tool_call_id,
                }
            })

            results.append({tool_name: tool_result})

            yield sse_event({"type": "STEP_FINISHED", "step_index": step_index})
            current_progress = min(80.0, 20.0 + per_step * (step_index + 1))

        # TYPING PHASE
        yield sse_event({
            "type": "STATE_UPDATE",
            "state": {
                "phase": "typing",
                "progress_pct": 85,
                "message": "✍️ Generating your flight analysis..."
            }
        })

        try:
            summary_data = await loop.run_in_executor(
                None,
                mcp_client.summarize_results,
                user_query,
                plan,
                results
            )
            assistant_text = summary_data.get("summary", "")
        except Exception as e:
            assistant_text = f"Failed to summarize results: {e}"

        # ---- SAVE TO REDIS ----
        append_turn_to_history(session_id, user_query, assistant_text)

        # Stream typing chunks
        msg_id = f"msg-{uuid.uuid4().hex[:8]}"

        yield sse_event({"type": "TEXT_MESSAGE_CONTENT", "message": {"id": msg_id, "role": "assistant", "content": ""}})

        chunks = chunk_text(assistant_text, max_len=150)
        for i, chunk in enumerate(chunks):
            yield sse_event({
                "type": "TEXT_MESSAGE_CONTENT",
                "message": {"id": msg_id, "role": "assistant", "delta": chunk}
            })
            await asyncio.sleep(0.03)

        # FINISHED
        yield sse_event({"type": "STATE_UPDATE", "state": {"phase": "finished", "progress_pct": 100}})
        yield sse_event({"type": "RUN_FINISHED", "run_id": run_id})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
