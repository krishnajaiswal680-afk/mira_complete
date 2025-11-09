import os
import json
import time
import asyncio
import logging
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from dotenv import load_dotenv

from flightops_client import FlightOpsMCPClient

load_dotenv()
logger = logging.getLogger("flightops.fastapi")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI(title="FlightOps SSE API")

# CORS: Vite dev server at 5173
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = FlightOpsMCPClient()

@app.get("/health")
async def health():
    return {"ok": True}

def sse_format(data: dict, event: str | None = None, id_: str | None = None, retry_ms: int | None = None) -> str:
    """
    Build SSE text lines. We use plain `data:` so front-end `onmessage` works.
    """
    lines = []
    if event:
        lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False))
    if id_:
        lines.append(f"id: {id_}")
    if retry_ms:
        lines.append(f"retry: {retry_ms}")
    lines.append("")  # blank line ends the event
    return "\n".join(lines)

@app.get("/sse")
async def sse(request: Request, q: str = Query(..., description="User query")) -> StreamingResponse:
    """
    SSE endpoint. Connect with EventSource(`${API}/sse?q=...`)
    Streams AG-UI-like events in `data:` JSON.
    """
    logger.info(f"SSE request: q={q!r}")

    async def event_generator() -> AsyncGenerator[bytes, None]:
        # HEARTBEAT every 15s while we work
        last_heartbeat = time.time()

        # 1) RUN_STARTED
        yield sse_format({"type": "RUN_STARTED", "ts": time.time()}).encode("utf-8")

        try:
            # Planning
            plan_obj = client.plan_tools(q)
            plan = plan_obj.get("plan", [])
            yield sse_format({"type": "PLAN", "plan": plan}).encode("utf-8")

            # If no plan, finish
            if not plan:
                yield sse_format({"type": "RUN_FINISHED", "reason": "NO_PLAN"}).encode("utf-8")
                return

            # Execute tools sequentially and stream results
            results = []
            for idx, step in enumerate(plan, start=1):
                if await request.is_disconnected():
                    logger.info("Client disconnected; stopping stream.")
                    return

                tool = step.get("tool")
                args = step.get("arguments", {}) or {}

                # Clean args a bit
                args = {k: v for k, v in args.items() if v and str(v).strip().lower() != "unknown"}

                yield sse_format({"type": "TOOL_START", "index": idx, "tool": tool, "args": args}).encode("utf-8")

                resp = await client.invoke_tool(tool, args)
                results.append({tool: resp})

                yield sse_format({"type": "TOOL_RESULT", "index": idx, "tool": tool, "result": resp}).encode("utf-8")

                # Heartbeat if needed
                if time.time() - last_heartbeat > 15:
                    yield sse_format({"type": "HEARTBEAT", "ts": time.time()}).encode("utf-8")
                    last_heartbeat = time.time()

            # Summarize
            summary = client.summarize_results(q, plan, results)
            yield sse_format({"type": "SUMMARY", "summary": summary}).encode("utf-8")

            # Finished
            yield sse_format({"type": "RUN_FINISHED"}).encode("utf-8")

        except Exception as e:
            logger.exception("Error in SSE stream")
            yield sse_format({"type": "RUN_ERROR", "error": str(e)}).encode("utf-8")

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# Optional: classic POST (non-SSE) if you need it
@app.post("/run")
async def run(payload: dict):
    q = payload.get("query", "")
    if not q:
        return JSONResponse({"ok": False, "error": "query missing"}, status_code=400)
    plan_obj = client.plan_tools(q)
    plan = plan_obj.get("plan", [])
    if not plan:
        return {"ok": True, "plan": [], "results": [], "summary": None}

    results = []
    for step in plan:
        tool = step.get("tool")
        args = step.get("arguments", {}) or {}
        resp = await client.invoke_tool(tool, args)
        results.append({tool: resp})
    summary = client.summarize_results(q, plan, results)
    return {"ok": True, "plan": plan, "results": results, "summary": summary}
