# ag_ui_adapter.py
import os
import json
import asyncio
import time
import uuid
import re
from typing import AsyncGenerator, List

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from client import FlightOpsMCPClient, extract_flight_details_from_query
import logging

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

# Setup logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# Utility to format SSE data
def sse_event(data: dict) -> str:
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"data: {payload}\n\n"

async def ensure_mcp_connected():
    if not mcp_client.session:
        await mcp_client.connect()

@app.on_event("startup")
async def startup_event():
    # Try pre-connecting to MCP so first requests are faster
    try:
        await ensure_mcp_connected()
    except Exception as e:
        logger.warning(f"Could not preconnect to MCP: {e}")

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

def determine_tool_from_query(query: str) -> str:
    """
    Determine which tool to use based on query content
    """
    query_lower = query.lower()

    if any(word in query_lower for word in ['delay', 'late', 'on-time', 'punctual']):
        return "get_delay_summary"
    elif any(word in query_lower for word in ['time', 'schedule', 'departure', 'arrival', 'operation']):
        return "get_operation_times"
    elif any(word in query_lower for word in ['equipment', 'aircraft', 'tail', 'plane']):
        return "get_equipment_info"
    elif any(word in query_lower for word in ['fuel', 'consumption']):
        return "get_fuel_summary"
    elif any(word in query_lower for word in ['passenger', 'pax']):
        return "get_passenger_info"
    elif any(word in query_lower for word in ['crew', 'pilot', 'staff']):
        return "get_crew_info"
    else:
        return "get_flight_basic_info"

# -------------------------
# Direct-flight endpoints (no LLM)
# -------------------------
@app.post("/direct-flight-query")
async def direct_flight_query(request: Request):
    """
    Direct flight query without LLM planning.
    Body: { "query": "<user text>" }
    Returns route-selection if multiple matching documents found, otherwise returns the single document result.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    user_query = body.get("query", "") or ""
    if not user_query.strip():
        raise HTTPException(status_code=400, detail="No query provided")

    # Extract flight details (carrier, flight_number, date_of_origin)
    flight_details = extract_flight_details_from_query(user_query)
    tool_name = determine_tool_from_query(user_query)

    try:
        await ensure_mcp_connected()
    except Exception as e:
        return {"success": False, "error": f"MCP connection failed: {e}"}

    # Use client's direct handler that already implements multi-doc detection and returns structured result
    try:
        result = await mcp_client.handle_flight_query_direct(user_query, tool_name, flight_details)
        return {"success": True, "data": result}
    except Exception as e:
        logger.exception("direct_flight_query failed")
        return {"success": False, "error": str(e)}

@app.post("/select-route")
async def select_route(request: Request):
    """
    Handle user route selection after direct-flight-route list was shown.
    Body: { "tool_name": "...", "tool_args": {...}, "selected_route": {"startStation": "...", "endStation": "..."} }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    tool_name = body.get("tool_name")
    tool_args = body.get("tool_args", {})
    selected_route = body.get("selected_route", {})

    if not tool_name or not selected_route:
        raise HTTPException(status_code=400, detail="tool_name and selected_route required")

    try:
        await ensure_mcp_connected()
        result = await mcp_client.get_documents_by_route_selection(tool_name, tool_args, selected_route)
        return {"success": True, "data": result}
    except Exception as e:
        logger.exception("select_route failed")
        return {"success": False, "error": str(e)}

# -------------------------
# AG-UI /agent endpoint (streaming)
# -------------------------
@app.post("/agent", response_class=StreamingResponse)
async def run_agent(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    thread_id = body.get("thread_id") or f"thread-{uuid.uuid4().hex[:8]}"
    run_id = body.get("run_id") or f"run-{uuid.uuid4().hex[:8]}"
    messages = body.get("messages", [])

    # determine user query — prefer last user message
    user_query = ""
    if messages:
        last = messages[-1]
        if isinstance(last, dict) and last.get("role") == "user":
            user_query = last.get("content", "")
        elif isinstance(last, str):
            user_query = last

    if not user_query or not user_query.strip():
        raise HTTPException(status_code=400, detail="No user query in messages payload")

    # Determine if this is a flight query that can be handled directly
    is_flight_query = any(keyword in user_query.lower() for keyword in
                         ['flight', '6e', 'carrier', 'delay', 'time', 'equipment', 'fuel', 'passenger', 'crew'])

    if is_flight_query:
        # Use lightweight direct flow (no LLM planning) and stream back SSE events
        return await handle_direct_flight_query(user_query, thread_id, run_id)
    else:
        # Use LLM-based handling
        return await handle_llm_based_query(user_query, thread_id, run_id, request)

# ---- Direct flight SSE handler (no LLM) ----
async def handle_direct_flight_query(user_query: str, thread_id: str, run_id: str):
    async def event_stream() -> AsyncGenerator[str, None]:
        # RUN_STARTED
        yield sse_event({"type": "RUN_STARTED", "thread_id": thread_id, "run_id": run_id})

        # THINKING
        yield sse_event({
            "type": "STATE_UPDATE",
            "state": {"phase": "thinking", "progress_pct": 10, "message": "Analyzing flight query..."}
        })

        try:
            await ensure_mcp_connected()
        except Exception as e:
            yield sse_event({"type": "RUN_ERROR", "error": f"MCP connect failed: {e}"})
            return

        # extract details and pick a tool
        flight_details = extract_flight_details_from_query(user_query)
        tool_name = determine_tool_from_query(user_query)

        yield sse_event({
            "type": "STATE_UPDATE",
            "state": {"phase": "processing", "progress_pct": 30, "message": f"Checking routes for {tool_name}..."}
        })

        try:
            result = await mcp_client.handle_flight_query_direct(user_query, tool_name, flight_details)
        except Exception as e:
            yield sse_event({"type": "RUN_ERROR", "error": f"Query failed: {e}"})
            yield sse_event({"type": "RUN_FINISHED", "run_id": run_id})
            return

        # result structure from client.handle_flight_query_direct():
        # - if needs_route_selection == True: returns available_routes list
        # - if result present: returns the single document result
        if result.get("needs_route_selection"):
            yield sse_event({
                "type": "ROUTE_SELECTION_NEEDED",
                "message": result.get("message", "Multiple matching flights found. Please select one."),
                "available_routes": result.get("available_routes", []),
                "flight_details": flight_details,
                "tool_name": tool_name,
                "tool_args": result.get("original_args", {})
            })
            yield sse_event({"type": "RUN_FINISHED", "run_id": run_id})
            return
        elif result.get("result"):
            # Single doc result - stream formatted text
            yield sse_event({
                "type": "STATE_UPDATE",
                "state": {"phase": "typing", "progress_pct": 80, "message": "Preparing flight information..."}
            })

            result_text = format_flight_result(result["result"], tool_name)
            msg_id = f"msg-{uuid.uuid4().hex[:8]}"

            yield sse_event({
                "type": "TEXT_MESSAGE_CONTENT",
                "message": {"id": msg_id, "role": "assistant", "content": result_text}
            })

            yield sse_event({
                "type": "STATE_UPDATE",
                "state": {"phase": "finished", "progress_pct": 100, "message": "✅ Done"}
            })
            yield sse_event({"type": "RUN_FINISHED", "run_id": run_id})
            return
        else:
            # Error
            yield sse_event({
                "type": "TEXT_MESSAGE_CONTENT",
                "message": {"id": f"msg-{uuid.uuid4().hex[:8]}", "role": "assistant", "content": "No results found."}
            })
            yield sse_event({"type": "RUN_FINISHED", "run_id": run_id})
            return

    return StreamingResponse(event_stream(), media_type="text/event-stream")

# ---- LLM-based SSE handler (unchanged core behavior) ----
async def handle_llm_based_query(user_query: str, thread_id: str, run_id: str, request: Request):
    async def event_stream() -> AsyncGenerator[str, None]:
        last_heartbeat = time.time()

        yield sse_event({"type": "RUN_STARTED", "thread_id": thread_id, "run_id": run_id})
        yield sse_event({"type": "STATE_UPDATE", "state": {"phase": "thinking", "progress_pct": 5, "message": "Analyzing your query..."}})

        try:
            await ensure_mcp_connected()
        except Exception as e:
            yield sse_event({"type": "RUN_ERROR", "error": f"MCP connect failed: {e}"})
            return

        loop = asyncio.get_event_loop()
        yield sse_event({"type": "STATE_UPDATE", "state": {"phase": "thinking", "progress_pct": 15, "message": "Planning which tools to use..."}})

        try:
            plan_data = await loop.run_in_executor(None, mcp_client.plan_tools, user_query)
        except Exception as e:
            yield sse_event({"type": "RUN_ERROR", "error": f"Planner error: {e}"})
            return

        plan = plan_data.get("plan", []) if isinstance(plan_data, dict) else []
        planning_usage = plan_data.get("llm_usage", {})

        if planning_usage:
            yield sse_event({"type": "TOKEN_USAGE", "phase": "planning", "usage": planning_usage})

        yield sse_event({"type": "STATE_SNAPSHOT", "snapshot": {"plan": plan}})

        if not plan:
            yield sse_event({"type": "TEXT_MESSAGE_CONTENT", "message": {"id": f"msg-{uuid.uuid4().hex[:8]}", "role": "assistant", "content": "I couldn't generate a valid plan for your query. Please try rephrasing."}})
            yield sse_event({"type": "STATE_UPDATE", "state": {"phase": "finished", "progress_pct": 100}})
            yield sse_event({"type": "RUN_FINISHED", "run_id": run_id})
            return

        # Execute plan
        results = []
        num_steps = max(1, len(plan))
        per_step = 60.0 / num_steps
        current_progress = 20.0

        for step_index, step in enumerate(plan):
            try:
                if await request.is_disconnected():
                    return
            except:
                pass

            tool_name = step.get("tool")
            args = step.get("arguments", {}) or {}

            yield sse_event({"type": "STATE_UPDATE", "state": {"phase": "processing", "progress_pct": round(current_progress), "message": f"Running {tool_name}..."}})

            tool_call_id = f"toolcall-{uuid.uuid4().hex[:8]}"

            yield sse_event({"type": "TOOL_CALL_START", "toolCallId": tool_call_id, "toolCallName": tool_name, "parentMessageId": None})
            yield sse_event({"type": "TOOL_CALL_ARGS", "toolCallId": tool_call_id, "delta": json.dumps(args, ensure_ascii=False)})
            yield sse_event({"type": "TOOL_CALL_END", "toolCallId": tool_call_id})

            try:
                tool_result = await mcp_client.invoke_tool(tool_name, args)
            except Exception as exc:
                tool_result = {"error": str(exc)}

            yield sse_event({"type": "TOOL_CALL_RESULT", "message": {"id": f"msg-{uuid.uuid4().hex[:8]}", "role": "tool", "content": json.dumps(tool_result, ensure_ascii=False), "tool_call_id": tool_call_id}})
            results.append({tool_name: tool_result})

            yield sse_event({"type": "STEP_FINISHED", "step_index": step_index, "tool": tool_name})

            current_progress = min(80.0, 20.0 + per_step * (step_index + 1))
            if time.time() - last_heartbeat > 15:
                yield sse_event({"type": "HEARTBEAT", "ts": time.time()})
                last_heartbeat = time.time()

        # Summarize and send
        yield sse_event({"type": "STATE_UPDATE", "state": {"phase": "typing", "progress_pct": 85, "message": "Generating your analysis..."}})

        try:
            summary_data = await loop.run_in_executor(None, mcp_client.summarize_results, user_query, plan, results)
            assistant_text = summary_data.get("summary", "") if isinstance(summary_data, dict) else str(summary_data)
            summarization_usage = summary_data.get("llm_usage", {})
            if summarization_usage:
                yield sse_event({"type": "TOKEN_USAGE", "phase": "summarization", "usage": summarization_usage})
        except Exception as e:
            assistant_text = f"❌ Failed to summarize results: {e}"

        # Stream assistant_text in chunks
        msg_id = f"msg-{uuid.uuid4().hex[:8]}"
        yield sse_event({"type": "TEXT_MESSAGE_CONTENT", "message": {"id": msg_id, "role": "assistant", "content": ""}})
        chunks = chunk_text(assistant_text, max_len=150)
        for i, chunk in enumerate(chunks):
            yield sse_event({"type": "TEXT_MESSAGE_CONTENT", "message": {"id": msg_id, "role": "assistant", "delta": chunk}})

            typing_progress = 85 + (i / len(chunks)) * 15
            yield sse_event({"type": "STATE_UPDATE", "state": {"phase": "typing", "progress_pct": round(typing_progress), "message": "Generating your analysis..."}})
            await asyncio.sleep(0.03)

        yield sse_event({"type": "STATE_SNAPSHOT", "snapshot": {"plan": plan, "results": results}})
        yield sse_event({"type": "STATE_UPDATE", "state": {"phase": "finished", "progress_pct": 100, "message": "✅ Analysis complete"}})
        yield sse_event({"type": "RUN_FINISHED", "run_id": run_id})

    return StreamingResponse(event_stream(), media_type="text/event-stream")

# ------------------------------------------------------------------------------------
# Format flight result for direct flow (human readable)
# ------------------------------------------------------------------------------------
def format_flight_result(result: dict, tool_name: str) -> str:
    flight_data = result.get("flightLegState", {})

    if tool_name == "get_delay_summary":
        delays = flight_data.get("delays", {})
        return (
            f"**Flight {flight_data.get('carrier', '')}{flight_data.get('flightNumber', '')}**\n"
            f"Route: {flight_data.get('startStation', '')} → {flight_data.get('endStation', '')}\n"
            f"Date: {flight_data.get('dateOfOrigin', '')}\n"
            f"Total Delay: {delays.get('total', 'N/A')} minutes\n"
            f"Scheduled: {flight_data.get('scheduledStartTime', 'N/A')}"
        )

    elif tool_name == "get_operation_times":
        return (
            f"**Flight {flight_data.get('carrier', '')}{flight_data.get('flightNumber', '')}**\n"
            f"Route: {flight_data.get('startStation', '')} → {flight_data.get('endStation', '')}\n"
            f"Date: {flight_data.get('dateOfOrigin', '')}\n"
            f"Scheduled Departure: {flight_data.get('scheduledStartTime', 'N/A')}\n"
            f"Scheduled Arrival: {flight_data.get('scheduledEndTime', 'N/A')}"
        )

    else:
        return (
            f"**Flight {flight_data.get('carrier', '')}{flight_data.get('flightNumber', '')}**\n"
            f"Route: {flight_data.get('startStation', '')} → {flight_data.get('endStation', '')}\n"
            f"Date: {flight_data.get('dateOfOrigin', '')}\n"
            f"Status: {flight_data.get('flightStatus', 'N/A')}"
        )
