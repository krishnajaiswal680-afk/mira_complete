1. Updated Backend (app.py)
python
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
from redis import Redis
from redis_entraid.cred_provider import create_from_service_principal
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

# Redis Configuration - USING THE SAME CONFIG AS YOUR WEATHER MCP
REDIS_HOST = "occh-uamr01.centralindia.redis.azure.net"
REDIS_PORT = 10000
REDIS_CLIENT_ID = os.getenv("REDIS_CLIENT_ID")
REDIS_CLIENT_SECRET = os.getenv("REDIS_CLIENT_SECRET")
REDIS_TENANT_ID = os.getenv("REDIS_TENANT_ID")

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Redis client initialization - EXACTLY LIKE YOUR WEATHER MCP
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

# Redis namespace configuration - UPDATED FOR FLIGHT_MCP
NAMESPACE = "non-prod"
PROJECT = "occhub"
MODULE = "flight_mcp"
HISTORY_TTL_SECONDS = 60 * 60 * 24  # 1 day
MAX_HISTORY_MESSAGES = 20  # last 20 messages (user+assistant)

def make_history_key(session_id: str) -> str:
    """Create Redis key for session history"""
    return f"{NAMESPACE}:{PROJECT}:{MODULE}:history:{session_id}"

def make_session_key(session_id: str) -> str:
    """Create Redis key for session metadata"""
    return f"{NAMESPACE}:{PROJECT}:{MODULE}:session:{session_id}"

async def append_to_history(session_id: str, role: str, content: str, metadata: dict = None) -> None:
    """Append a message to session history"""
    if not redis_client:
        logger.debug("append_to_history: redis_client is not available, skipping")
        return
    
    try:
        key = make_history_key(session_id)
        message_data = {
            "role": role,
            "content": content,
            "timestamp": time.time(),
            "metadata": metadata or {}
        }
        
        # Use pipeline for atomic operations
        pipe = redis_client.pipeline()
        pipe.rpush(key, json.dumps(message_data, ensure_ascii=False))
        pipe.ltrim(key, -MAX_HISTORY_MESSAGES, -1)  # Keep only last N messages
        pipe.expire(key, HISTORY_TTL_SECONDS)
        pipe.execute()
        
        logger.debug(f"💾 Saved message to history for session: {session_id}")
    except Exception as e:
        logger.warning("append_to_history failed for key=%s: %s", key, e)

async def load_history(session_id: str, max_messages: int = MAX_HISTORY_MESSAGES) -> List[dict]:
    """Load session history from Redis"""
    if not redis_client:
        logger.debug("load_history: redis_client is not available")
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

async def generate_session_title(session_id: str) -> str:
    """Generate a title based on the first user message"""
    if not redis_client:
        return f"Session {session_id[-8:]}"
    
    try:
        history_key = make_history_key(session_id)
        first_message = redis_client.lindex(history_key, 0)  # Get first message
        
        if first_message:
            msg_data = json.loads(first_message)
            if msg_data["role"] == "user":
                user_content = msg_data["content"][:50]  # Use first 50 chars
                return f"Chat: {user_content}..."
        
        return f"Session {session_id[-8:]}"
    except:
        return f"Session {session_id[-8:]}"

async def create_or_update_session(session_id: str, user_query: str = None) -> None:
    """Create or update session metadata"""
    if not redis_client:
        return
    
    try:
        key = make_session_key(session_id)
        
        # Generate title based on first message
        title = await generate_session_title(session_id)
        
        session_data = {
            "session_id": session_id,
            "title": title,
            "created_at": time.time(),
            "last_activity": time.time(),
            "message_count": 0,
            "last_query": user_query or ""
        }
        
        # Update existing or create new
        existing = redis_client.get(key)
        if existing:
            try:
                existing_data = json.loads(existing)
                session_data["created_at"] = existing_data.get("created_at", time.time())
                session_data["message_count"] = existing_data.get("message_count", 0) + 1
                # Keep existing title unless it's the default
                if existing_data.get("title", "").startswith("Session "):
                    session_data["title"] = title
                else:
                    session_data["title"] = existing_data.get("title", title)
            except json.JSONDecodeError:
                pass
        
        redis_client.setex(key, HISTORY_TTL_SECONDS, json.dumps(session_data, ensure_ascii=False))
        logger.debug(f"💾 Updated session metadata: {session_id}")
    except Exception as e:
        logger.warning(f"Failed to update session {session_id}: {e}")

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
        redis_status = "connected" if redis_client and redis_client.ping() else "disconnected"
        return {
            "status": "healthy", 
            "mcp_connected": True,
            "redis_connected": redis_status
        }
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
    
    # Use thread_id as session_id for history tracking
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

    # Save user message to history and update session (Redis integration)
    await append_to_history(session_id, "user", user_query, {"run_id": run_id})
    await create_or_update_session(session_id, user_query)

    async def event_stream() -> AsyncGenerator[str, None]:
        last_heartbeat = time.time()

        # --- RUN STARTED
        yield sse_event({"type": "RUN_STARTED", "thread_id": thread_id, "run_id": run_id})
        
        # Load conversation history for context (Redis integration)
        history_messages = await load_history(session_id)
        
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
            plan_data = await loop.run_in_executor(None, mcp_client.plan_tools, user_query)
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
            error_msg = "I couldn't generate a valid plan for your query. Please try rephrasing."
            yield sse_event({
                "type": "TEXT_MESSAGE_CONTENT", 
                "message": {
                    "id": f"msg-{uuid.uuid4().hex[:8]}",
                    "role": "assistant",
                    "content": error_msg
                }
            })
            # Save assistant response to history (Redis integration)
            await append_to_history(session_id, "assistant", error_msg, {"run_id": run_id, "error": True})
            
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
            
            # AG-UI compatible tool call events
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
                "type": "TOOL_CALL_END", 
                "toolCallId": tool_call_id
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

        # Save assistant response to history (Redis integration)
        await append_to_history(session_id, "assistant", assistant_text, {
            "run_id": run_id, 
            "tools_used": [step.get("tool") for step in plan],
            "results_count": len(results)
        })

        # Stream summary as chunks - AG-UI compatible format
        msg_id = f"msg-{uuid.uuid4().hex[:8]}"
        
        # Start message
        yield sse_event({
            "type": "TEXT_MESSAGE_START",
            "message": {
                "id": msg_id,
                "role": "assistant",
                "content": ""
            }
        })

        # Stream content chunks
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

        # End message
        yield sse_event({
            "type": "TEXT_MESSAGE_END",
            "messageId": msg_id
        })

        # Final state with history info (Redis integration)
        yield sse_event({
            "type": "STATE_SNAPSHOT", 
            "snapshot": {
                "plan": plan, 
                "results": results,
                "session_id": session_id,
                "history_count": len(history_messages) + 2  # +2 for current exchange
            }
        })
        yield sse_event({
            "type": "STATE_UPDATE", 
            "state": {
                "phase": "finished", 
                "progress_pct": 100,
                "message": f"✅ Analysis complete (Session: {session_id})"
            }
        })
        yield sse_event({"type": "RUN_FINISHED", "run_id": run_id})

    return StreamingResponse(event_stream(), media_type="text/event-stream")

# New endpoints for session management (Redis integration)
@app.get("/sessions/{session_id}/history")
async def get_session_history(session_id: str, limit: int = 20):
    """Get conversation history for a session"""
    history = await load_history(session_id, limit)
    return {
        "session_id": session_id,
        "history": history,
        "count": len(history)
    }

@app.get("/sessions/{session_id}")
async def get_session_info(session_id: str):
    """Get session metadata"""
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis not available")
    
    try:
        key = make_session_key(session_id)
        session_data = redis_client.get(key)
        if session_data:
            return json.loads(session_data)
        else:
            raise HTTPException(status_code=404, detail="Session not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving session: {e}")

@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session and its history"""
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis not available")
    
    try:
        history_key = make_history_key(session_id)
        session_key = make_session_key(session_id)
        
        pipe = redis_client.pipeline()
        pipe.delete(history_key)
        pipe.delete(session_key)
        result = pipe.execute()
        
        deleted_count = sum(result)
        return {
            "session_id": session_id,
            "deleted": deleted_count > 0,
            "items_removed": deleted_count
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting session: {e}")

@app.get("/sessions")
async def list_sessions(limit: int = 50):
    """List all active sessions"""
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis not available")
    
    try:
        pattern = make_session_key("*")
        keys = redis_client.keys(pattern)
        sessions = []
        
        for key in keys[:limit]:
            session_data = redis_client.get(key)
            if session_data:
                sessions.append(json.loads(session_data))
        
        return {
            "sessions": sessions,
            "count": len(sessions)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error listing sessions: {e}")

@app.get("/chat-history")
async def get_chat_history():
    """Get complete chat history for sidebar - COMPATIBLE WITH FRONTEND"""
    if not redis_client:
        return {"sessions": []}
    
    try:
        # Get all sessions
        pattern = make_session_key("*")
        session_keys = redis_client.keys(pattern)
        session_keys.sort(reverse=True)  # Most recent first
        
        sessions_data = []
        
        for key in session_keys[:50]:  # Limit to 50 most recent sessions
            session_json = redis_client.get(key)
            if session_json:
                session_data = json.loads(session_json)
                
                # Get message count and last message
                history_key = make_history_key(session_data["session_id"])
                message_count = redis_client.llen(history_key)
                
                # Get last message for preview
                last_message = ""
                if message_count > 0:
                    last_msg_json = redis_client.lindex(history_key, -1)  # Get last message
                    if last_msg_json:
                        try:
                            last_msg = json.loads(last_msg_json)
                            last_message = last_msg.get("content", "")[:80] + "..." if len(last_msg.get("content", "")) > 80 else last_msg.get("content", "")
                        except:
                            pass
                
                sessions_data.append({
                    "id": session_data["session_id"],
                    "title": session_data.get("title", f"Session {session_data['session_id'][-8:]}"),
                    "created_at": session_data["created_at"],
                    "last_activity": session_data["last_activity"],
                    "message_count": message_count,
                    "last_message": last_message,
                    "user_query": session_data.get("last_query", "")
                })
        
        return {"sessions": sessions_data}
    
    except Exception as e:
        logger.error(f"Error getting chat history: {e}")
        return {"sessions": []}

@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, limit: int = 100):
    """Get all messages for a specific session - COMPATIBLE WITH FRONTEND"""
    history_messages = await load_history(session_id, limit)
    
    # Format messages for frontend
    formatted_messages = []
    for msg in history_messages:
        formatted_messages.append({
            "id": f"msg-{uuid.uuid4().hex[:8]}",  # Generate unique ID for frontend
            "role": msg["role"],
            "content": msg["content"],
            "timestamp": msg["timestamp"],
            "metadata": msg.get("metadata", {})
        })
    
    return {
        "session_id": session_id,
        "messages": formatted_messages,
        "count": len(formatted_messages)
    }
2. Updated App.jsx
jsx
import React, { useState, useEffect } from 'react';
import ChatPage from './ChatPage';
import Sidebar from './Sidebar';
import './App.css';

function App() {
  const [currentSession, setCurrentSession] = useState(null);
  const [sessions, setSessions] = useState([]);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  const fetchSessions = async () => {
    try {
      const response = await fetch('http://localhost:8000/chat-history');
      const data = await response.json();
      setSessions(data.sessions || []);
    } catch (error) {
      console.error('Error fetching sessions:', error);
    }
  };

  useEffect(() => {
    fetchSessions();
    
    // Refresh sessions every 30 seconds
    const interval = setInterval(fetchSessions, 30000);
    return () => clearInterval(interval);
  }, []);

  const handleNewChat = () => {
    const newSessionId = 'thread-' + Math.random().toString(36).substr(2, 9);
    setCurrentSession(newSessionId);
    // Sessions will be automatically created when first message is sent
  };

  const handleSessionSelect = (sessionId) => {
    setCurrentSession(sessionId);
  };

  const toggleSidebar = () => {
    setSidebarCollapsed(!sidebarCollapsed);
  };

  return (
    <div className="app-container">
      <Sidebar
        sessions={sessions}
        currentSession={currentSession}
        onSessionSelect={handleSessionSelect}
        onNewChat={handleNewChat}
        collapsed={sidebarCollapsed}
        onToggle={toggleSidebar}
      />
      
      <div className={`main-content ${sidebarCollapsed ? 'expanded' : ''}`}>
        <div className="header">
          <button className="sidebar-toggle" onClick={toggleSidebar}>
            {sidebarCollapsed ? '☰' : '✕'}
          </button>
          <h1>FlightOps Assistant</h1>
          <div className="header-actions">
            {currentSession && (
              <span className="session-id">Session: {currentSession.slice(-8)}</span>
            )}
          </div>
        </div>
        
        {currentSession ? (
          <ChatPage 
            sessionId={currentSession}
            onNewSession={handleNewChat}
          />
        ) : (
          <div className="welcome-screen">
            <div className="welcome-content">
              <div className="welcome-icon">✈️</div>
              <h1>Welcome to FlightOps Assistant</h1>
              <p>Ask me anything about flight operations — delays, fuel, passengers, aircraft details, etc.</p>
              <button className="start-chat-btn" onClick={handleNewChat}>
                Start New Chat
              </button>
              
              {sessions.length > 0 && (
                <div className="recent-sessions">
                  <h3>Recent Sessions</h3>
                  <div className="session-previews">
                    {sessions.slice(0, 3).map(session => (
                      <div 
                        key={session.id}
                        className="session-preview-card"
                        onClick={() => handleSessionSelect(session.id)}
                      >
                        <div className="session-title">{session.title}</div>
                        <div className="session-last-message">{session.last_message}</div>
                        <div className="session-time">
                          {new Date(session.last_activity * 1000).toLocaleDateString()}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default App;
3. Updated ChatPage.jsx
jsx
import React, { useState, useRef, useEffect } from "react";
import { v4 as uuidv4 } from 'uuid';
import MessageBubble from "./MessageBubble";

const AG_AGENT_ENDPOINT = import.meta.env.VITE_AGENT_ENDPOINT || "http://localhost:8000/agent";

export default function ChatPage({ sessionId, onNewSession }) {
    const [messages, setMessages] = useState([
        {
            role: "assistant",
            content: "Hello! 👋 I'm your FlightOps Agent. Ask me anything about flight operations — delays, fuel, passengers, aircraft details, etc.",
            timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        },
    ]);
    const [input, setInput] = useState("");
    const [loading, setLoading] = useState(false);
    const [agentState, setAgentState] = useState({
        phase: 'idle',
        progress: 0,
        message: ''
    });
    const [tokenUsage, setTokenUsage] = useState({
        planning: null,
        summarization: null,
        total: null
    });
    const messagesEndRef = useRef(null);

    // Load session messages when session changes
    useEffect(() => {
        if (sessionId) {
            loadSessionMessages(sessionId);
        }
    }, [sessionId]);

    const loadSessionMessages = async (sessionId) => {
        try {
            const response = await fetch(`http://localhost:8000/sessions/${sessionId}/messages`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            const data = await response.json();
            
            if (data.messages && data.messages.length > 0) {
                const formattedMessages = data.messages.map(msg => ({
                    ...msg,
                    timestamp: new Date(msg.timestamp * 1000).toLocaleTimeString([], { 
                        hour: '2-digit', 
                        minute: '2-digit' 
                    })
                }));
                setMessages(formattedMessages);
            } else {
                // Reset to welcome message for new sessions
                setMessages([
                    {
                        role: "assistant",
                        content: "Hello! 👋 I'm your FlightOps Agent. Ask me anything about flight operations — delays, fuel, passengers, aircraft details, etc.",
                        timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                    },
                ]);
            }
        } catch (error) {
            console.error('Error loading session messages:', error);
            // Fallback to empty conversation
            setMessages([
                {
                    role: "assistant", 
                    content: "Hello! 👋 I'm your FlightOps Agent. Let's start a new conversation.",
                    timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                },
            ]);
        }
    };

    const scrollToBottom = () => {
        messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    };

    useEffect(() => {
        scrollToBottom();
    }, [messages]);

    async function sendMessage() {
        if (!input.trim()) return;

        const userMessage = { 
            role: "user", 
            content: input,
            timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        };
        setMessages((prev) => [...prev, userMessage]);
        setLoading(true);
        setInput("");
        
        setAgentState({
            phase: 'thinking',
            progress: 0,
            message: 'Starting analysis...'
        });
        setTokenUsage({
            planning: null,
            summarization: null,
            total: null
        });

        const body = {
            thread_id: sessionId,
            run_id: "run-" + Date.now(),
            messages: [userMessage],
        };

        try {
            const resp = await fetch(AG_AGENT_ENDPOINT, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });

            if (!resp.ok) {
                const errTxt = await resp.text();
                throw new Error(errTxt);
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buf = "";
            let currentMessageId = null;

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buf += decoder.decode(value, { stream: true });

                let idx;
                while ((idx = buf.indexOf("\n\n")) !== -1) {
                    const chunk = buf.slice(0, idx).trim();
                    buf = buf.slice(idx + 2);
                    const lines = chunk.split("\n");

                    for (const line of lines) {
                        if (line.startsWith("data: ")) {
                            const payload = line.slice(6).trim();
                            if (!payload) continue;

                            try {
                                const event = JSON.parse(payload);
                                console.log("SSE Event:", event.type, event);
                                handleEvent(event, currentMessageId);
                            } catch (err) {
                                console.warn("Bad SSE line:", payload, err);
                            }
                        }
                    }
                }
            }
        } catch (err) {
            console.error("Error in sendMessage:", err);
            setMessages((prev) => [
                ...prev,
                { 
                    role: "assistant", 
                    content: "⚠️ Error: " + err.message,
                    timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                },
            ]);
            setAgentState({ phase: 'idle', progress: 0, message: '' });
        } finally {
            setLoading(false);
        }
    }

    function handleEvent(event, currentMessageId) {
        const timestamp = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        
        console.log("Processing event:", event.type);
        
        switch (event.type) {
            case "STATE_UPDATE":
                setAgentState(event.state);
                break;
                
            case "TEXT_MESSAGE_START":
                if (event.message && event.message.id) {
                    currentMessageId = event.message.id;
                    // Remove any existing message with same ID to avoid duplicates
                    setMessages(prev => {
                        const filtered = prev.filter(m => m.id !== currentMessageId);
                        return [...filtered, {
                            id: currentMessageId,
                            role: event.message.role,
                            content: "",
                            timestamp: timestamp
                        }];
                    });
                }
                break;
                
            case "TEXT_MESSAGE_CONTENT":
                if (event.message) {
                    if (event.message.delta) {
                        setMessages(prev => {
                            const existingIdx = prev.findIndex(m => m.id === event.message.id);
                            if (existingIdx >= 0) {
                                const updated = [...prev];
                                updated[existingIdx].content += event.message.delta;
                                updated[existingIdx].timestamp = timestamp;
                                return updated;
                            } else {
                                return [...prev, {
                                    id: event.message.id,
                                    role: event.message.role,
                                    content: event.message.delta,
                                    timestamp: timestamp
                                }];
                            }
                        });
                    } else if (event.message.content) {
                        setMessages(prev => {
                            // Remove any existing message with same ID to avoid duplicates
                            const filtered = prev.filter(m => m.id !== event.message.id);
                            return [...filtered, {
                                id: event.message.id,
                                role: event.message.role,
                                content: event.message.content,
                                timestamp: timestamp
                            }];
                        });
                    }
                } else if (event.content) {
                    setMessages(prev => [...prev, {
                        id: `msg-${uuidv4().slice(0, 8)}`,
                        role: "assistant",
                        content: event.content,
                        timestamp: timestamp
                    }]);
                }
                break;
                
            case "TEXT_MESSAGE_END":
                // Message streaming completed
                break;
                
            case "TOOL_CALL_START":
                setMessages(prev => [...prev, {
                    id: `sys-${uuidv4().slice(0, 8)}`,
                    role: "system",
                    content: `🛠️ Starting ${event.toolCallName}...`,
                    timestamp: timestamp
                }]);
                break;
                
            case "TOKEN_USAGE":
                console.log("TOKEN_USAGE event received:", event.phase, event.usage);
                setTokenUsage(prev => ({
                    ...prev,
                    [event.phase]: event.usage
                }));
                
                if (event.phase === 'total' && event.usage) {
                    setMessages(prev => [...prev, {
                        id: `token-${uuidv4().slice(0, 8)}`,
                        role: "system",
                        content: `📊 Token Usage: ${event.usage.total_tokens || 0} total (${event.usage.prompt_tokens || 0} prompt + ${event.usage.completion_tokens || 0} completion)`,
                        timestamp: timestamp
                    }]);
                }
                break;
                
            case "RUN_FINISHED":
                setAgentState({ phase: 'idle', progress: 0, message: '' });
                break;
                
            case "RUN_ERROR":
                setMessages(prev => [...prev, {
                    id: `err-${uuidv4().slice(0, 8)}`,
                    role: "assistant",
                    content: "❌ " + event.error,
                    timestamp: timestamp
                }]);
                setAgentState({ phase: 'idle', progress: 0, message: '' });
                break;
                
            default:
                console.log('Unhandled event type:', event.type);
                break;
        }
    }

    const StatusIndicator = () => {
        if (agentState.phase === 'idle' && !tokenUsage.total) return null;
        
        const phaseIcons = {
            thinking: '🧠',
            processing: '🛠️', 
            typing: '✍️',
            finished: '✅',
            idle: '✅'
        };
        
        return (
            <div className={`status-indicator ${agentState.phase}`}>
                <div className="status-content">
                    <span className="status-icon">{phaseIcons[agentState.phase]}</span>
                    <span className="status-message">{agentState.message}</span>
                    
                    {tokenUsage.total && (
                        <div className="token-usage">
                            <div className="token-breakdown">
                                <span className="token-label">Total Tokens: </span>
                                <span className="token-value">{tokenUsage.total.total_tokens || 0}</span>
                            </div>
                            <div className="token-details">
                                <span>Prompt: {tokenUsage.total.prompt_tokens || 0}</span>
                                <span>Completion: {tokenUsage.total.completion_tokens || 0}</span>
                            </div>
                        </div>
                    )}
                    
                    {agentState.progress > 0 && (
                        <div className="progress-bar">
                            <div 
                                className="progress-fill"
                                style={{ width: `${agentState.progress}%` }}
                            ></div>
                        </div>
                    )}
                </div>
            </div>
        );
    };

    return (
        <div className="chat-container">
            <div className="chat-header">
                <div className="session-info">
                    <span className="session-badge">Session: {sessionId?.slice(-8)}</span>
                </div>
                <button className="new-session-btn" onClick={onNewSession}>
                    New Chat
                </button>
            </div>

            <div className="messages-container">
                {messages.map((msg, idx) => (
                    <MessageBubble 
                        key={msg.id || `msg-${idx}`} 
                        role={msg.role} 
                        content={msg.content} 
                        timestamp={msg.timestamp}
                    />
                ))}
                
                <StatusIndicator />
                
                {loading && agentState.phase === 'idle' && (
                    <div className="loading-message">
                        <div className="loading-dots">
                            <span></span>
                            <span></span>
                            <span></span>
                        </div>
                        <div className="loading-timestamp">
                            {new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                        </div>
                    </div>
                )}
                <div ref={messagesEndRef} />
            </div>

            <div className="input-container">
                <input
                    type="text"
                    className="message-input"
                    placeholder="Ask about a flight, e.g. 'Why was 6E215 delayed on June 23, 2024?'"
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && sendMessage()}
                    disabled={loading}
                />
                <button
                    onClick={sendMessage}
                    className="send-button"
                    disabled={loading || !input.trim()}
                >
                    Send
                </button>
            </div>
        </div>
    );
}
4. Environment File (.env)
Create a .env file in your frontend root:

env
VITE_AGENT_ENDPOINT=http://localhost:8000/agent
Key Changes Made:
Fixed port mismatch - All frontend calls now use port 8000

Added uuid import in ChatPage.jsx

Improved message ID handling to prevent duplicates

Enhanced error handling for session loading

Better message streaming with proper start/end events

Consistent session ID generation pattern

To Run:
Backend: uvicorn app:app --reload --port 8000

Frontend: npm run dev

Set environment variables for Redis credentials

The integration should now work smoothly with proper session management and message handling!

give me app.css also
Here's the complete App.css file:

5. Complete App.css
css
/* Reset and base styles */
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
  background: #f8fafc;
  height: 100vh;
  overflow: hidden;
}

.app-container {
  display: flex;
  height: 100vh;
}

/* Main Content */
.main-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: #f8fafc;
  overflow: hidden;
  transition: margin-left 0.3s ease;
}

.main-content.expanded {
  margin-left: -260px;
}

.header {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: 1rem 1.5rem;
  background: white;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.05);
  border-bottom: 1px solid #e5e7eb;
}

.sidebar-toggle {
  background: none;
  border: none;
  font-size: 1.2rem;
  cursor: pointer;
  padding: 0.5rem;
  border-radius: 6px;
  color: #4f46e5;
  transition: background 0.2s;
}

.sidebar-toggle:hover {
  background: #f3f4f6;
}

.header h1 {
  font-size: 1.25rem;
  font-weight: 600;
  color: #1f2937;
  flex: 1;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 1rem;
}

.session-id {
  font-size: 0.875rem;
  color: #6b7280;
  background: #f3f4f6;
  padding: 0.25rem 0.5rem;
  border-radius: 4px;
}

/* Welcome Screen */
.welcome-screen {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  background: white;
}

.welcome-content {
  text-align: center;
  max-width: 500px;
  padding: 2rem;
}

.welcome-icon {
  font-size: 4rem;
  margin-bottom: 1.5rem;
  animation: float 3s ease-in-out infinite;
}

@keyframes float {
  0%, 100% { transform: translateY(0px); }
  50% { transform: translateY(-10px); }
}

.welcome-content h1 {
  font-size: 2rem;
  font-weight: 700;
  color: #1f2937;
  margin-bottom: 1rem;
}

.welcome-content p {
  font-size: 1.1rem;
  color: #6b7280;
  margin-bottom: 2rem;
  line-height: 1.6;
}

.start-chat-btn {
  padding: 1rem 2rem;
  background: linear-gradient(135deg, #4f46e5, #7c3aed);
  color: white;
  border: none;
  border-radius: 12px;
  font-size: 1.1rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.3s ease;
  box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
}

.start-chat-btn:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(79, 70, 229, 0.4);
}

/* Recent Sessions */
.recent-sessions {
  margin-top: 3rem;
  text-align: left;
}

.recent-sessions h3 {
  font-size: 1.1rem;
  color: #374151;
  margin-bottom: 1rem;
  text-align: center;
}

.session-previews {
  display: flex;
  gap: 1rem;
  justify-content: center;
  flex-wrap: wrap;
}

.session-preview-card {
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 1rem;
  cursor: pointer;
  transition: all 0.2s;
  width: 150px;
}

.session-preview-card:hover {
  border-color: #4f46e5;
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
}

.session-preview-card .session-title {
  font-weight: 600;
  font-size: 0.9rem;
  color: #374151;
  margin-bottom: 0.5rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.session-preview-card .session-last-message {
  font-size: 0.8rem;
  color: #6b7280;
  margin-bottom: 0.5rem;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.session-preview-card .session-time {
  font-size: 0.75rem;
  color: #9ca3af;
}

/* Chat Container */
.chat-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: white;
  margin: 1rem;
  border-radius: 12px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
  overflow: hidden;
}

.chat-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 1rem 1.5rem;
  background: white;
  border-bottom: 1px solid #e5e7eb;
}

.session-badge {
  background: #f3f4f6;
  color: #374151;
  padding: 0.25rem 0.75rem;
  border-radius: 6px;
  font-size: 0.875rem;
  font-weight: 500;
}

.new-session-btn {
  background: #f3f4f6;
  color: #374151;
  border: 1px solid #d1d5db;
  padding: 0.5rem 1rem;
  border-radius: 6px;
  font-size: 0.875rem;
  cursor: pointer;
  transition: all 0.2s;
}

.new-session-btn:hover {
  background: #e5e7eb;
}

.messages-container {
  flex: 1;
  padding: 1.5rem;
  overflow-y: auto;
  background: #f8fafc;
}

/* Message Bubbles */
.message {
  margin-bottom: 1.5rem;
  display: flex;
}

.user-message {
  justify-content: flex-end;
}

.user-message .message-content {
  background: linear-gradient(135deg, #667eea, #764ba2);
  color: white;
  border-bottom-right-radius: 4px;
}

.assistant-message .message-content {
  background: white;
  color: #374151;
  border: 1px solid #e5e7eb;
  border-bottom-left-radius: 4px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.system-message .message-content {
  background: #fef3c7;
  color: #92400e;
  border: 1px solid #fbbf24;
  border-radius: 12px;
}

.message-content {
  max-width: 70%;
  padding: 1rem 1.25rem;
  border-radius: 18px;
  position: relative;
}

.message-text {
  line-height: 1.5;
  margin-bottom: 0.5rem;
}

.message-timestamp {
  font-size: 0.75rem;
  opacity: 0.7;
  margin-top: 0.5rem;
}

.user-message .message-timestamp {
  text-align: right;
  color: rgba(255, 255, 255, 0.8);
}

.assistant-message .message-timestamp,
.system-message .message-timestamp {
  text-align: left;
  color: #6b7280;
}

/* Token Usage Styles */
.token-usage {
  margin-top: 0.75rem;
  padding: 0.75rem;
  background: rgba(255, 255, 255, 0.7);
  border-radius: 8px;
  border: 1px solid rgba(79, 70, 229, 0.2);
}

.token-breakdown {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.5rem;
}

.token-label {
  font-weight: 600;
  color: #374151;
  font-size: 0.9rem;
}

.token-value {
  font-weight: 700;
  color: #4f46e5;
  font-size: 1rem;
}

.token-details {
  display: flex;
  justify-content: space-between;
  font-size: 0.8rem;
  color: #6b7280;
}

.token-details span {
  padding: 0.25rem 0.5rem;
  background: rgba(79, 70, 229, 0.1);
  border-radius: 4px;
}

/* Status Indicator */
.status-indicator {
  padding: 1rem 1.25rem;
  margin: 1rem 0;
  border-radius: 12px;
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-left: 4px solid #4f46e5;
}

.status-indicator.thinking {
  border-left-color: #8b5cf6;
  background: #faf5ff;
}

.status-indicator.processing {
  border-left-color: #f59e0b;
  background: #fffbeb;
}

.status-indicator.typing {
  border-left-color: #10b981;
  background: #ecfdf5;
}

.status-content {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.status-icon {
  font-size: 1.25rem;
  margin-right: 0.5rem;
}

.status-message {
  font-weight: 500;
  color: #374151;
  font-size: 0.9rem;
}

.progress-bar {
  width: 100%;
  height: 6px;
  background: #e5e7eb;
  border-radius: 3px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #4f46e5, #7c3aed);
  transition: width 0.3s ease;
  border-radius: 3px;
}

/* Loading Animation */
.loading-message {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  margin-bottom: 1.5rem;
}

.loading-dots {
  display: flex;
  gap: 0.25rem;
  padding: 1rem 1.25rem;
  background: white;
  border-radius: 18px;
  border: 1px solid #e5e7eb;
}

.loading-dots span {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #9ca3af;
  animation: bounce 1.4s infinite ease-in-out;
}

.loading-dots span:nth-child(1) { animation-delay: -0.32s; }
.loading-dots span:nth-child(2) { animation-delay: -0.16s; }

@keyframes bounce {
  0%, 80%, 100% { transform: scale(0.8); opacity: 0.5; }
  40% { transform: scale(1); opacity: 1; }
}

.loading-timestamp {
  font-size: 0.75rem;
  color: #6b7280;
  margin-top: 0.5rem;
  margin-left: 1.25rem;
}

/* Input Container */
.input-container {
  display: flex;
  gap: 0.75rem;
  padding: 1.5rem;
  background: white;
  border-top: 1px solid #e5e7eb;
}

.message-input {
  flex: 1;
  padding: 0.75rem 1rem;
  border: 1px solid #d1d5db;
  border-radius: 12px;
  font-size: 0.95rem;
  outline: none;
  transition: all 0.2s;
}

.message-input:focus {
  border-color: #4f46e5;
  box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.1);
}

.send-button {
  padding: 0.75rem 1.5rem;
  background: linear-gradient(135deg, #4f46e5, #7c3aed);
  color: white;
  border: none;
  border-radius: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
}

.send-button:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
}

.send-button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
  transform: none;
}

/* Scrollbar Styling */
.messages-container::-webkit-scrollbar {
  width: 6px;
}

.messages-container::-webkit-scrollbar-track {
  background: #f1f5f9;
  border-radius: 3px;
}

.messages-container::-webkit-scrollbar-thumb {
  background: #cbd5e1;
  border-radius: 3px;
}

.messages-container::-webkit-scrollbar-thumb:hover {
  background: #94a3b8;
}

/* Responsive */
@media (max-width: 768px) {
  .message-content {
    max-width: 85%;
  }
  
  .input-container {
    padding: 1rem;
  }
  
  .session-previews {
    flex-direction: column;
    align-items: center;
  }
  
  .session-preview-card {
    width: 100%;
    max-width: 250px;
  }
  
  .chat-container {
    margin: 0.5rem;
  }
  
  .messages-container {
    padding: 1rem;
  }
  
  .header {
    padding: 1rem;
  }
  
  .header h1 {
    font-size: 1.1rem;
  }
}

@media (max-width: 480px) {
  .message-content {
    max-width: 90%;
    padding: 0.75rem 1rem;
  }
  
  .input-container {
    flex-direction: column;
    gap: 0.5rem;
  }
  
  .send-button {
    width: 100%;
  }
  
  .welcome-content {
    padding: 1rem;
  }
  
  .welcome-content h1 {
    font-size: 1.5rem;
  }
  
  .welcome-content p {
    font-size: 1rem;
  }
}

/* Animation for new messages */
@keyframes slideIn {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.message {
  animation: slideIn 0.3s ease-out;
}

/* Focus states for accessibility */
button:focus-visible,
input:focus-visible {
  outline: 2px solid #4f46e5;
  outline-offset: 2px;
}

/* High contrast mode support */
@media (prefers-contrast: high) {
  .message-content {
    border: 2px solid;
  }
  
  .user-message .message-content {
    border-color: #000;
  }
  
  .assistant-message .message-content {
    border-color: #666;
  }
}

/* Reduced motion support */
@media (prefers-reduced-motion: reduce) {
  * {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
  
  .welcome-icon {
    animation: none;
  }
}
6. Sidebar.css (for completeness)
css
/* Sidebar Styles */
.sidebar {
  width: 320px;
  height: 100vh;
  background: linear-gradient(180deg, #1e1b4b 0%, #312e81 100%);
  color: white;
  display: flex;
  flex-direction: column;
  box-shadow: 4px 0 20px rgba(0, 0, 0, 0.1);
  transition: transform 0.3s ease;
}

.sidebar-collapsed {
  width: 60px;
  height: 100vh;
  background: linear-gradient(180deg, #1e1b4b 0%, #312e81 100%);
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 1rem 0;
  gap: 1rem;
}

.sidebar-toggle {
  background: none;
  border: none;
  color: white;
  font-size: 1.2rem;
  cursor: pointer;
  padding: 0.5rem;
  border-radius: 6px;
  transition: background 0.2s;
}

.sidebar-toggle:hover {
  background: rgba(255, 255, 255, 0.1);
}

.new-chat-btn-collapsed {
  width: 40px;
  height: 40px;
  border: none;
  background: rgba(255, 255, 255, 0.2);
  color: white;
  border-radius: 50%;
  font-size: 1.2rem;
  cursor: pointer;
  transition: all 0.2s;
}

.new-chat-btn-collapsed:hover {
  background: rgba(255, 255, 255, 0.3);
  transform: scale(1.1);
}

.sidebar-header {
  padding: 1.5rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.sidebar-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 1rem;
}

.sidebar-title h2 {
  font-size: 1.25rem;
  font-weight: 600;
  color: #e0e7ff;
}

.close-sidebar {
  background: none;
  border: none;
  color: #a5b4fc;
  font-size: 1.1rem;
  cursor: pointer;
  padding: 0.25rem;
  border-radius: 4px;
  transition: background 0.2s;
}

.close-sidebar:hover {
  background: rgba(255, 255, 255, 0.1);
}

.new-chat-btn {
  width: 100%;
  padding: 0.75rem 1rem;
  background: rgba(255, 255, 255, 0.2);
  border: 1px solid rgba(255, 255, 255, 0.3);
  border-radius: 8px;
  color: white;
  cursor: pointer;
  transition: all 0.2s;
  font-weight: 500;
}

.new-chat-btn:hover {
  background: rgba(255, 255, 255, 0.3);
  transform: translateY(-1px);
}

.search-container {
  padding: 1rem 1.5rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.search-input {
  width: 100%;
  padding: 0.5rem 1rem;
  background: rgba(255, 255, 255, 0.1);
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 6px;
  color: white;
  font-size: 0.9rem;
}

.search-input::placeholder {
  color: #a5b4fc;
}

.search-input:focus {
  outline: none;
  border-color: rgba(255, 255, 255, 0.4);
}

.sessions-list {
  flex: 1;
  display: flex;
  flex-direction: column;
}

.sessions-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 1rem 1.5rem;
}

.sessions-header h3 {
  font-size: 0.9rem;
  font-weight: 600;
  color: #a5b4fc;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.session-count {
  background: rgba(255, 255, 255, 0.2);
  padding: 0.25rem 0.5rem;
  border-radius: 12px;
  font-size: 0.8rem;
  color: #e0e7ff;
}

.sessions-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 0 0.5rem;
}

.session-item {
  display: flex;
  padding: 0.75rem;
  margin: 0.25rem 0.5rem;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s;
  gap: 0.75rem;
}

.session-item:hover {
  background: rgba(255, 255, 255, 0.1);
}

.session-item.active {
  background: rgba(255, 255, 255, 0.15);
  border: 1px solid rgba(255, 255, 255, 0.2);
}

.session-icon {
  font-size: 1rem;
  margin-top: 0.25rem;
}

.session-content {
  flex: 1;
  min-width: 0;
}

.session-title {
  font-weight: 500;
  font-size: 0.9rem;
  color: white;
  margin-bottom: 0.25rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.session-preview {
  font-size: 0.8rem;
  color: #a5b4fc;
  margin-bottom: 0.5rem;
  line-height: 1.3;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.session-meta {
  display: flex;
  justify-content: space-between;
  font-size: 0.75rem;
  color: #818cf8;
}

.message-count {
  font-weight: 500;
}

.session-time {
  font-style: italic;
}

.no-sessions {
  text-align: center;
  color: #a5b4fc;
  font-style: italic;
  padding: 2rem;
  font-size: 0.9rem;
}

.sidebar-footer {
  padding: 1rem 1.5rem;
  border-top: 1px solid rgba(255, 255, 255, 0.1);
}

.user-info {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.user-avatar {
  width: 32px;
  height: 32px;
  background: rgba(255, 255, 255, 0.2);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.9rem;
}

.user-details {
  flex: 1;
}

.user-name {
  font-size: 0.9rem;
  font-weight: 500;
  color: white;
}

.user-status {
  font-size: 0.75rem;
  color: #10b981;
}

/* Scrollbar Styling */
.sessions-scroll::-webkit-scrollbar {
  width: 4px;
}

.sessions-scroll::-webkit-scrollbar-track {
  background: rgba(255, 255, 255, 0.1);
}

.sessions-scroll::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.3);
  border-radius: 2px;
}

.sessions-scroll::-webkit-scrollbar-thumb:hover {
  background: rgba(255, 255, 255, 0.5);
}

/* Responsive */
@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    left: 0;
    top: 0;
    height: 100vh;
    z-index: 1000;
    transform: translateX(-100%);
  }
  
  .sidebar.open {
    transform: translateX(0);
  }
  
  .sidebar-collapsed {
    display: none;
  }
}
7. MessageBubble.jsx (if needed)
jsx
import React from 'react';

const MessageBubble = ({ role, content, timestamp }) => {
  const isUser = role === 'user';
  const isSystem = role === 'system';
  
  return (
    <div className={`message ${isUser ? 'user-message' : isSystem ? 'system-message' : 'assistant-message'}`}>
      <div className="message-content">
        <div className="message-text">{content}</div>
        <div className="message-timestamp">{timestamp}</div>
      </div>
    </div>
  );
};

export default MessageBubble;
