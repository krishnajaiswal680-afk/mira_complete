import React, { useState, useRef, useEffect } from "react";
import { API_BASE } from "./api";

export default function App() {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("idle");
  const [events, setEvents] = useState([]); // all raw events we receive
  const [plan, setPlan] = useState([]);
  const [toolResults, setToolResults] = useState([]);
  const [summary, setSummary] = useState("");
  const esRef = useRef(null);

  // cleanup on unmount
  useEffect(() => {
    return () => {
      if (esRef.current) esRef.current.close();
    };
  }, []);

  const startSSE = async () => {
    if (!query.trim()) {
      alert("Please enter a question.");
      return;
    }

    // reset UI
    setStatus("connecting");
    setEvents([]);
    setPlan([]);
    setToolResults([]);
    setSummary("");

    // Close previous connection if any
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }

    const url = `${API_BASE}/sse?q=${encodeURIComponent(query)}`;
    const es = new EventSource(url, { withCredentials: false });
    esRef.current = es;

    es.onopen = () => {
      setStatus("streaming");
      appendEvent({ type: "INFO", msg: "SSE connection opened" });
    };

    es.onerror = (err) => {
      console.error("SSE error", err);
      setStatus("error");
      appendEvent({ type: "ERROR", msg: "SSE error / connection closed" });
      es.close();
    };

    es.onmessage = (evt) => {
      // We are using plain `data:` messages from server
      try {
        const data = JSON.parse(evt.data);
        handleEvent(data);
      } catch (e) {
        console.warn("Non-JSON data:", evt.data);
      }
    };
  };

  const appendEvent = (e) => setEvents((prev) => [...prev, e]);

  const handleEvent = (e) => {
    appendEvent(e);
    switch (e.type) {
      case "RUN_STARTED":
        break;

      case "PLAN":
        setPlan(e.plan || []);
        break;

      case "TOOL_START":
        // Optional: show spinner or highlight current tool
        break;

      case "TOOL_RESULT":
        setToolResults((prev) => [...prev, { index: e.index, tool: e.tool, result: e.result }]);
        break;

      case "SUMMARY":
        setSummary(e.summary?.summary || "");
        break;

      case "RUN_FINISHED":
        setStatus("done");
        break;

      case "RUN_ERROR":
        setStatus("error");
        break;

      case "HEARTBEAT":
      default:
        // ignore or log
        break;
    }
  };

  const stopSSE = () => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
      appendEvent({ type: "INFO", msg: "SSE connection closed by client" });
      setStatus("idle");
    }
  };

  return (
    <div style={{ maxWidth: 1000, margin: "20px auto", fontFamily: "Inter, system-ui, Arial" }}>
      <h1>✈️ FlightOps — React + SSE (FastAPI)</h1>
      <p>Ask any flight operations question. Backend plans tools via Azure OpenAI → MCP executes → summary streamed via SSE.</p>

      <div style={{ display: "grid", gap: 8, gridTemplateColumns: "1fr auto" }}>
        <textarea
          rows={4}
          placeholder="e.g. Why was flight 6E215 delayed on June 23, 2024?"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{ width: "100%", padding: 12, fontSize: 14 }}
        />
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <button onClick={startSSE} style={btnStyle}>Ask</button>
          <button onClick={stopSSE} style={{ ...btnStyle, background: "#eee", color: "#333" }}>Stop</button>
        </div>
      </div>

      <StatusBar status={status} />

      {!!plan.length && (
        <section>
          <h2>🗂️ LLM Tool Plan</h2>
          <pre style={preStyle}>{JSON.stringify(plan, null, 2)}</pre>
        </section>
      )}

      {!!toolResults.length && (
        <section>
          <h2>🔧 MCP Tool Results</h2>
          {toolResults.map((tr) => (
            <div key={tr.index} style={cardStyle}>
              <strong>Tool:</strong> <code>{tr.tool}</code>
              <pre style={preStyle}>{JSON.stringify(tr.result, null, 2)}</pre>
            </div>
          ))}
        </section>
      )}

      {!!summary && (
        <section>
          <h2>📝 Final Summary</h2>
          <div style={cardStyle}>{summary}</div>
        </section>
      )}

      <details style={{ marginTop: 16 }}>
        <summary>📦 Raw Events</summary>
        <pre style={preStyle}>{JSON.stringify(events, null, 2)}</pre>
      </details>
    </div>
  );
}

function StatusBar({ status }) {
  const color = status === "streaming" ? "#1976d2"
    : status === "done" ? "#2e7d32"
    : status === "error" ? "#d32f2f"
    : "#555";
  const text = status === "streaming" ? "Streaming..."
    : status === "done" ? "Done"
    : status === "error" ? "Error"
    : "Idle";
  return (
    <div style={{ margin: "12px 0", padding: "8px 12px", background: "#fafafa", border: "1px solid #eee" }}>
      <strong>Status:</strong> <span style={{ color }}>{text}</span>
    </div>
  );
}

const btnStyle = {
  padding: "10px 14px",
  fontSize: 14,
  cursor: "pointer",
  border: "1px solid #ddd",
  background: "#111",
  color: "#fff",
  borderRadius: 8
};

const preStyle = {
  background: "#0b1020",
  color: "#e6f1ff",
  padding: "10px",
  borderRadius: 8,
  overflowX: "auto"
};

const cardStyle = {
  border: "1px solid #eee",
  background: "#f9fafb",
  padding: 12,
  borderRadius: 8,
  marginBottom: 12
};
