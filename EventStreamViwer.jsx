import React from "react";

export default function EventStreamViewer({ events }) {
  return (
    <div>
      <h3>Event Stream</h3>
      <div style={{ maxHeight: 300, overflow: "auto", background: "#f7f7f8", padding: 12 }}>
        {events.length === 0 ? <div style={{ color: "#666" }}>No events yet.</div> :
          events.map((e, i) => (
            <pre key={i} style={{ borderBottom: "1px solid #eee", paddingBottom: 8 }}>
              <strong>{e.type || e.type}</strong>
              {"\n"}
              {JSON.stringify(e, null, 2)}
            </pre>
          ))
        }
      </div>
    </div>
  );
}