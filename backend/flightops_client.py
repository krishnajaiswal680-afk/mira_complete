import os
import json
import logging
from typing import Any, Dict

from dotenv import load_dotenv
from openai import AzureOpenAI
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from tool_registry import TOOLS

load_dotenv()
logger = logging.getLogger("FlightOps.MCPClient")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:9000").rstrip("/")

AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
AZURE_API_VERSION = os.getenv("AZURE_API_VERSION", "2024-12-01-preview")

if not AZURE_OPENAI_KEY:
    raise RuntimeError("❌ AZURE_OPENAI_KEY not set in environment")

client_azure = AzureOpenAI(
    api_key=AZURE_OPENAI_KEY,
    api_version=AZURE_API_VERSION,
    azure_endpoint=AZURE_OPENAI_ENDPOINT
)

def _build_tool_prompt() -> str:
    lines = []
    for name, meta in TOOLS.items():
        arg_str = ", ".join(meta["args"])
        lines.append(f"- {name}({arg_str}): {meta['desc']}")
    return "\n".join(lines)

SYSTEM_PROMPT_PLAN = f"""
You are an assistant that converts user questions into MCP tool calls.

Available tools:
{_build_tool_prompt()}

### Tool selection logic
1. Use `run_aggregated_query` for counts/sums/averages/min/max...
2. Use `raw_mongodb_query` to list/show/find filtered data (with projection).
3. Use single-flight tools if flight number & date present.
4. Always return JSON with "plan": [...]
5. No invented fields, exclude _id in projections.
6. StartTimeOffset & EndTimeOffset -> run_aggregated_query.
7. latest date in DB is 2025-05-30 consider as today
"""

SYSTEM_PROMPT_SUMMARIZE = """
You are an assistant that summarizes tool outputs into concise bullets.
Be factual and helpful.
"""

class FlightOpsMCPClient:
    def __init__(self, base_url: str = None):
        self.base_url = (base_url or MCP_SERVER_URL).rstrip("/")
        self.session: ClientSession | None = None
        self._client_context = None

    async def connect(self):
        logger.info(f"Connecting to MCP server at {self.base_url}")
        self._client_context = streamablehttp_client(self.base_url)
        read_stream, write_stream, _ = await self._client_context.__aenter__())
        self.session = ClientSession(read_stream, write_stream)
        await self.session.__aenter__()
        await self.session.initialize()
        logger.info("✅ Connected to MCP server")

    async def ensure(self):
        if not self.session:
            await self.connect()

    async def disconnect(self):
        if self.session:
            await self.session.__aexit__(None, None, None)
        if self._client_context:
            await self._client_context.__aexit__(None, None, None)

    def _call_azure_openai(self, messages: list, temperature: float = 0.2, max_tokens: int = 2048) -> str:
        try:
            completion = client_azure.chat.completions.create(
                model=AZURE_OPENAI_DEPLOYMENT,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return completion.choices[0].message.content
        except Exception as e:
            logger.error(f"Azure OpenAI API error: {e}")
            return json.dumps({"error": str(e)})

    def plan_tools(self, user_query: str) -> dict:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_PLAN},
            {"role": "user", "content": user_query},
        ]
        content = self._call_azure_openai(messages, temperature=0.1) or ""
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
            cleaned = cleaned.replace("```", "").strip()
        try:
            plan = json.loads(cleaned)
            if isinstance(plan, dict) and "plan" in plan:
                return plan
            return {"plan": []}
        except json.JSONDecodeError:
            return {"plan": []}

    async def list_tools(self) -> dict:
        await self.ensure()
        tools_list = await self.session.list_tools()
        return {
            "tools": {
                t.name: {"description": t.description, "inputSchema": t.inputSchema}
                for t in tools_list.tools
            }
        }

    async def invoke_tool(self, tool_name: str, args: dict) -> dict:
        await self.ensure()
        result = await self.session.call_tool(tool_name, args)
        if result.content:
            items = []
            for item in result.content:
                if hasattr(item, "text"):
                    try:
                        items.append(json.loads(item.text))
                    except json.JSONDecodeError:
                        items.append(item.text)
            if len(items) == 1:
                return items[0]
            return {"results": items}
        return {"error": "No content in response"}

    def summarize_results(self, user_query: str, plan: list, results: list) -> dict:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_SUMMARIZE},
            {"role": "user", "content": f"Question:\n{user_query}"},
            {"role": "assistant", "content": f"Plan:\n{json.dumps(plan, indent=2)}"},
            {"role": "assistant", "content": f"Results:\n{json.dumps(results, indent=2)}"},
        ]
        summary = self._call_azure_openai(messages, temperature=0.3)
        return {"summary": summary}
