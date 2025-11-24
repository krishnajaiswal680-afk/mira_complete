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

logger = logging.getLogger("ag_ui_adapter.redis")
logger.setLevel(logging.INFO)

REDIS_HOST = "occh-uamr01.centralindia.redis.azure.net"
REDIS_PORT = 10000

REDIS_CLIENT_ID = os.getenv("REDIS_CLIENT_ID")
REDIS_CLIENT_SECRET = os.getenv("REDIS_CLIENT_SECRET")
REDIS_TENANT_ID = os.getenv("REDIS_TENANT_ID")

# Namespace as requested
NAMESPACE = "non-prod"
PROJECT = "occhub"
MODULE = "flight_mcp"

HISTORY_TTL_SECONDS = 60 * 60 * 24  # 1 day
MAX_HISTORY_MESSAGES = 50

# create credential provider only (do not instantiate Redis yet)
try:
    redis_credential_provider = create_from_service_principal(
        REDIS_CLIENT_ID,
        REDIS_CLIENT_SECRET,
        REDIS_TENANT_ID,
    )
except Exception as e:
    logger.error("Failed to create redis credential provider: %s", e)
    redis_credential_provider = None

# DEV: create SSL flags that disable verification (ONLY FOR DEV)
_dev_disable_ssl_verify = os.getenv("DISABLE_REDIS_SSL_VERIFY", "1") == "1"
if _dev_disable_ssl_verify:
    logger.warning("Redis SSL verification DISABLED (development)")

redis_client = None

try:
    if redis_credential_provider:
        # NOTE: redis-py versions differ in accepted SSL params.
        # Many versions do NOT accept ssl_context in Redis(...), so use ssl_cert_reqs and ssl_check_hostname.
        redis_client = Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            ssl=True,
            credential_provider=redis_credential_provider,
            decode_responses=True,
            socket_timeout=5,            # read timeout
            socket_connect_timeout=5,    # connect timeout
            ssl_cert_reqs=None if _dev_disable_ssl_verify else "required",
            ssl_check_hostname=False if _dev_disable_ssl_verify else True,
        )
        # quick non-fatal ping to surface connectivity issues early
        try:
            ok = redis_client.ping()
            logger.info("Redis ping -> %s", ok)
        except Exception as e:
            logger.warning("Redis ping failed (will continue): %s", e)
    else:
        redis_client = None
except TypeError as te:
    logger.error("Redis client init TypeError (unsupported arg): %s", te)
    redis_client = None
except Exception as e:
    logger.error("Failed to create redis_client: %s", e)
    redis_client = None

def make_history_key(session_id: str) -> str:
    return f"{NAMESPACE}:{PROJECT}:{MODULE}:history:{session_id}"

def append_turn_to_history(session_id: str, user_msg: str, assistant_msg: str) -> None:
    """
    Store one full turn (user + assistant) in Redis List.
    Defensive: returns immediately if Redis is unavailable.
    """
    if not redis_client:
        logger.debug("append_turn_to_history: redis_client is not available, skipping")
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
        logger.warning("append_turn_to_history failed for key=%s: %s", key, e)

def load_history_messages(session_id: str, max_messages: int = MAX_HISTORY_MESSAGES):
    """
    Load last N messages from Redis and return as list of dicts.
    Defensive: returns empty list if Redis is unavailable or on error.
    """
    if not redis_client:
        logger.debug("load_history_messages: redis_client is not available")
        return []

    key = make_history_key(session_id)
    try:
        length = redis_client.llen(key)
    except Exception as e:
        logger.warning("Redis llen failed for key=%s: %s", key, e)
        return []

    if not length:
        return []

    start = max(0, length - max_messages)
    try:
        raw_msgs = redis_client.lrange(key, start, -1)
    except Exception as e:
        logger.warning("Redis lrange failed for key=%s: %s", key, e)
        return []

    messages = []
    for raw in raw_msgs:
        try:
            messages.append(json.loads(raw))
        except Exception:
            continue
    return messages
# ----------------- End Redis CONFIG -----------------

app = FastAPI(title="FlightOps — AG-UI Adapter")

# CORS (adjust origins for your Vite origin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

mcp_client = FlightOpsMCPClient()

def sse_event(data: dict) -> str:
    """Encode one SSE event (JSON payload)"""
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
    parts: List[str] = []
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

@app.post("/agent", response_class=StreamingResponse)
async def run_agent(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    thread_id = body.get("thread_id") or f"thread-{uuid.uuid4().hex[:8]}"
    run_id = body.get("run_id") or f"run-{uuid.uuid4().hex[:8]}"
    messages = body.get("messages", [])

    # map session to thread_id
    session_id = thread_id

    # last user message as query
    user_query = ""
    if messages:
        last = messages[-1]
        if isinstance(last, dict) and last.get("role") == "user":
            user_query = last.get("content", "") or ""
        elif isinstance(last, str):
            user_query = last

    if not user_query.strip():
        raise HTTPException(status_code=400, detail="No user query found")

    # --- LOAD HISTORY from Redis (Option A: plain-text injection)
    history_messages = load_history_messages(session_id)
    history_text = ""
    if history_messages:
        for h in history_messages:
            role = h.get("role", "unknown")
            content = h.get("content", "")
            history_text += f"{role.upper()}: {content}\n"

    async def event_stream() -> AsyncGenerator[str, None]:
        last_heartbeat = time.time()

        # --- RUN STARTED
        yield sse_event({"type": "RUN_STARTED", "thread_id": thread_id, "run_id": run_id})
        
        # THINKING: Initial analysis
        yield sse_event({
            "type": "STATE_UPDATE", 
            "state": {
                "phase": "thinking", 
                "progress_pct": 5,
                "message": "🧠 Analyzing your flight query..."
            }
        })

        # ensure MCP
        try:
            await ensure_mcp_connected()
        except Exception as e:
            yield sse_event({"type": "RUN_ERROR", "error": f"MCP connect failed: {e}"})
            return

        loop = asyncio.get_event_loop()

        # --- PLAN (THINKING phase)
        yield sse_event({
            "type": "STATE_UPDATE", 
            "state": {
                "phase": "thinking", 
                "progress_pct": 15,
                "message": "📋 Planning which flight data tools to use..."
            }
        })
        
        try:
            # Inject history_text (Option A) before the user's query for planning
            planner_input = f"{history_text}\nUSER: {user_query}" if history_text else user_query
            plan_data = await loop.run_in_executor(None, mcp_client.plan_tools, planner_input)
        except Exception as e:
            yield sse_event({"type": "RUN_ERROR", "error": f"Planner error: {e}"})
            return

        plan = plan_data.get("plan", []) if isinstance(plan_data, dict) else []
        planning_usage = plan_data.get("llm_usage", {})
        
        # DEBUG: Send token usage for planning
        print(f"DEBUG: Planning token usage: {planning_usage}")
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
                    "content": "I couldn't generate a valid plan for your query. Please try rephrasing."
                }
            })
            yield sse_event({"type": "STATE_UPDATE", "state": {"phase": "finished", "progress_pct": 100}})
            yield sse_event({"type": "RUN_FINISHED", "run_id": run_id})
            return

        # --- PROCESSING: Tool execution
        yield sse_event({
            "type": "STATE_UPDATE", 
            "state": {
                "phase": "processing", 
                "progress_pct": 20,
                "message": f"🛠️ Executing {len(plan)} flight data tools..."
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
            args = step.get("arguments", {}) or {}

            yield sse_event({
                "type": "STATE_UPDATE", 
                "state": {
                    "phase": "processing",
                    "progress_pct": round(current_progress),
                    "message": f"🔧 Running {tool_name}..."
                }
            })

            tool_call_id = f"toolcall-{uuid.uuid4().hex[:8]}"
            
            yield sse_event({
                "type": "TOOL_CALL_START",
                "toolCallId": tool_call_id,
                "toolCallName": tool_name,
                "parentMessageId": None
            })

            yield sse_event({
                "type": "TOOL_CALL_ARGS",
                "toolCallId": tool_call_id,
                "delta": json.dumps(args, ensure_ascii=False)
            })
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
                    "content": json.dumps(tool_result, ensure_ascii=False),
                    "tool_call_id": tool_call_id,
                }
            })
            results.append({tool_name: tool_result})

            yield sse_event({
                "type": "STEP_FINISHED",
                "step_index": step_index,
                "tool": tool_name
            })

            current_progress = min(80.0, 20.0 + per_step * (step_index + 1))
            
            if time.time() - last_heartbeat > 15:
                yield sse_event({"type": "HEARTBEAT", "ts": time.time()})
                last_heartbeat = time.time()

        # --- TYPING: Result generation
        yield sse_event({
            "type": "STATE_UPDATE", 
            "state": {
                "phase": "typing", 
                "progress_pct": 85,
                "message": "✍️ Generating your flight analysis..."
            }
        })

        try:
            summary_data = await loop.run_in_executor(None, mcp_client.summarize_results, user_query, plan, results)
            assistant_text = summary_data.get("summary", "") if isinstance(summary_data, dict) else str(summary_data)
            summarization_usage = summary_data.get("llm_usage", {})
            
            # DEBUG: Send token usage for summarization
            print(f"DEBUG: Summarization token usage: {summarization_usage}")
            if summarization_usage:
                yield sse_event({
                    "type": "TOKEN_USAGE",
                    "phase": "summarization", 
                    "usage": summarization_usage
                })
                
                # Calculate and send total token usage
                def safe_int(val):
                    return val if isinstance(val, int) else 0
                    
                total_usage = {
                    "prompt_tokens": safe_int(planning_usage.get('prompt_tokens', 0)) + safe_int(summarization_usage.get('prompt_tokens', 0)),
                    "completion_tokens": safe_int(planning_usage.get('completion_tokens', 0)) + safe_int(summarization_usage.get('completion_tokens', 0)),
                    "total_tokens": safe_int(planning_usage.get('total_tokens', 0)) + safe_int(summarization_usage.get('total_tokens', 0))
                }
                
                print(f"DEBUG: Total token usage: {total_usage}")
                yield sse_event({
                    "type": "TOKEN_USAGE",
                    "phase": "total",
                    "usage": total_usage
                })
                
        except Exception as e:
            assistant_text = f"❌ Failed to summarize results: {e}"

        # ----------------- Save turn to Redis -----------------
        try:
            append_turn_to_history(session_id, user_query, assistant_text)
            logger.info("Saved turn to Redis for session=%s", session_id)
        except Exception as e:
            logger.warning("Failed to save turn to Redis: %s", e)

        # Stream summary as chunks
        msg_id = f"msg-{uuid.uuid4().hex[:8]}"
        
        yield sse_event({
            "type": "TEXT_MESSAGE_CONTENT",
            "message": {
                "id": msg_id,
                "role": "assistant",
                "content": ""
            }
        })

        chunks = chunk_text(assistant_text, max_len=150)
        for i, chunk in enumerate(chunks):
            yield sse_event({
                "type": "TEXT_MESSAGE_CONTENT",
                "message": {
                    "id": msg_id,
                    "role": "assistant", 
                    "delta": chunk
                }
            })
            
            typing_progress = 85 + (i / len(chunks)) * 15
            yield sse_event({
                "type": "STATE_UPDATE",
                "state": {
                    "phase": "typing",
                    "progress_pct": round(typing_progress),
                    "message": "✍️ Generating your flight analysis..."
                }
            })
            
            await asyncio.sleep(0.03)

        # Final state
        yield sse_event({"type": "STATE_SNAPSHOT", "snapshot": {"plan": plan, "results": results}})
        yield sse_event({
            "type": "STATE_UPDATE", 
            "state": {
                "phase": "finished", 
                "progress_pct": 100,
                "message": "✅ Analysis complete"
            }
        })
        yield sse_event({"type": "RUN_FINISHED", "run_id": run_id})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
