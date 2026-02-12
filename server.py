#!/usr/bin/env python3
"""Doc-Writing Bidi Agent Server.

Architecture:
  Browser (WebSocket) <-> FastAPI <-> BidiAgent (Nova Sonic 2)
                                         |-- file_read, file_write, editor, shell
                                         |-- MCP tools (GitHub, fetch, etc.)
                                         |-- stop_conversation
                                         |
                                         v
                                   write_document (tool) -> Writer Agent (Opus 4.6)
                                     - file_read, file_write, editor, shell
                                     - MCP servers (GitHub, fetch, etc.)

The bidi agent has full tool access — it can read files, look up GitHub PRs,
fetch web pages, etc. during conversation. When you're ready to write, it calls
write_document with the FULL conversation context, and the writer subagent
(Opus 4.6) produces the document.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from strands import Agent, tool
from strands.experimental.bidi import BidiAgent
from strands.experimental.bidi.models import BidiNovaSonicModel
from strands.experimental.bidi.tools import stop_conversation
from strands.experimental.bidi.types.events import BidiOutputEvent
from strands.experimental.bidi.types.io import BidiInput, BidiOutput
from strands.models.bedrock import BedrockModel

# MCP imports
from mcp import stdio_client, StdioServerParameters
from strands.tools.mcp import MCPClient
from strands_tools import file_read, file_write, editor, shell

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- MCP Config ---
DEFAULT_MCP_CONFIG = os.getenv("MCP_CONFIG_PATH", str(Path.home() / ".kiro" / "settings" / "mcp.json"))


def load_mcp_config(config_path: str | None = None) -> dict:
    path = Path(config_path or DEFAULT_MCP_CONFIG).expanduser()
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load MCP config: {e}")
        return {}


def create_mcp_clients(mcp_config: dict) -> list[MCPClient]:
    servers = mcp_config.get("mcpServers", {})
    clients = []
    for name, cfg in servers.items():
        if cfg.get("disabled", False):
            continue
        command = cfg.get("command")
        if not command:
            continue
        args = cfg.get("args", [])
        env = {}
        for k, v in cfg.get("env", {}).items():
            env[k] = os.path.expandvars(v) if isinstance(v, str) else v

        def make_transport(cmd, arguments, environment):
            return lambda: stdio_client(
                StdioServerParameters(command=cmd, args=arguments, env=environment or None)
            )

        try:
            client = MCPClient(make_transport(command, args, env))
            clients.append(client)
            logger.info(f"Created MCP client: {name}")
        except Exception as e:
            logger.error(f"Failed to create MCP client {name}: {e}")
    return clients


# --- Writer Subagent (agent-as-tool) ---

WRITER_SYSTEM_PROMPT = """\
You are a senior technical writer and document creator. You write clear,
well-structured documents based on the context provided to you.

Rules:
- Write in the user's voice and style (infer from context).
- Include all references, links, and file paths provided.
- Use markdown formatting.
- Be thorough but concise.
- Save the document to the specified path using file_write.
- If no path specified, save to ~/docs/ with a descriptive filename.
- You have access to file tools and MCP servers (GitHub, fetch, etc.)
  to look up additional info if needed.
"""

# Global MCP clients (initialized once, shared by both bidi and writer agents)
_mcp_clients: list[MCPClient] = []


def get_writer_agent() -> Agent:
    """Create a fresh writer agent (Opus 4.6) with MCP tools."""
    model = BedrockModel(model_id="global.anthropic.claude-opus-4-6-v1")
    tools = [file_read, file_write, editor, shell]
    tools.extend(_mcp_clients)
    return Agent(model=model, tools=tools, system_prompt=WRITER_SYSTEM_PROMPT)


@tool
def write_document(prompt: str, output_path: str = "") -> str:
    """Write a document using a powerful writer agent (Claude Opus 4.6).

    The writer agent has access to file_read, file_write, editor, shell, and
    MCP servers (GitHub, fetch, etc.). Pass the FULL conversation context as
    the prompt — everything the user said, all references read, all opinions
    discussed, all links mentioned. Do NOT summarize or abstract — give the
    writer the complete picture so it can produce the best document.

    Args:
        prompt: The complete prompt for the writer agent. This should include:
                - The full conversation context (what was discussed, decided)
                - All file contents that were read via file_read
                - The user's opinions, preferences, and style notes
                - Specific writing instructions (structure, audience, tone)
                - Any links, references, or data to include
                - The desired output path
        output_path: Optional file path to save the document. If empty, the
                     writer will choose an appropriate path under ~/docs/.

    Returns:
        The written document content and the path where it was saved.
    """
    try:
        writer = get_writer_agent()
        if output_path:
            prompt += f"\n\nSave the document to: {output_path}"
        else:
            prompt += "\n\nChoose an appropriate path under ~/docs/ and save the document there."
        prompt += "\n\nWrite the complete document and save it. Return the full content and confirm where it was saved."
        result = writer(prompt)
        return str(result)
    except Exception as e:
        return f"Error writing document: {e}"



# --- Bidi Agent System Prompt ---

BIDI_SYSTEM_PROMPT = """\
You are a document writing assistant. Your role is to help the user think through
and write documents.

## Your Tools:
You have full access to: file_read, file_write, editor, shell, MCP tools
(GitHub, fetch, etc.), and write_document (delegates to a powerful writer agent).

## Your Workflow:
1. EXPLORE: Ask questions to understand what the user wants to write. What's the
   topic? Who's the audience? What's the goal? What style?
2. GATHER: Use file_read to pull in files the user references. Use MCP tools
   to look up GitHub PRs, issues, web pages, etc.
3. DISCUSS: Explore the user's opinions and ideas. Push back gently, suggest
   structure, identify gaps.
4. WRITE: When the user is ready, use write_document to create the document.
   Pass the FULL conversation context as the prompt — everything discussed,
   every file read, every opinion, every link. Do NOT summarize or abstract.
   The writer agent needs the complete picture to produce the best document.
5. ITERATE: After writing, discuss the output. Use file_read to read what was
   written, use editor for small edits, or call write_document again for rewrites.

## Important:
- Be conversational and natural. Ask one question at a time.
- Keep your spoken responses SHORT (1-3 sentences). You're talking, not writing.
- When calling write_document, dump EVERYTHING into the prompt field — the full
  conversation, all file contents, all opinions, all references. More context = better doc.
- Don't try to write the document yourself in speech. Use the write_document tool.
"""


# --- WebSocket I/O ---

class WebSocketBidiInput(BidiInput):
    """Read text input from WebSocket client."""

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self._running = True

    async def start(self, agent: BidiAgent) -> None:
        pass

    async def __call__(self):
        while self._running:
            try:
                data = await self.websocket.receive_json()
                return data
            except WebSocketDisconnect:
                self._running = False
                raise
            except Exception as e:
                logger.error(f"WebSocket input error: {e}")
                raise

    async def stop(self) -> None:
        self._running = False


class WebSocketBidiOutput(BidiOutput):
    """Send output events to WebSocket client."""

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket

    async def start(self, agent: BidiAgent) -> None:
        pass

    async def __call__(self, event: BidiOutputEvent) -> None:
        try:
            # Convert event to JSON-serializable dict
            event_data = dict(event)
            # Handle bytes in audio data
            if "audio" in event_data and isinstance(event_data["audio"], bytes):
                import base64
                event_data["audio"] = base64.b64encode(event_data["audio"]).decode("utf-8")
            await self.websocket.send_json(event_data)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"WebSocket output error: {e}")

    async def stop(self) -> None:
        pass


# --- FastAPI App ---

app = FastAPI(title="Doc Writing Bidi Agent")

# Serve static files (UI)
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket client connected")

    try:
        model = BidiNovaSonicModel()

        agent = BidiAgent(
            model=model,
            tools=[file_read, file_write, editor, shell, write_document, stop_conversation] + _mcp_clients,
            system_prompt=BIDI_SYSTEM_PROMPT,
        )

        ws_input = WebSocketBidiInput(websocket)
        ws_output = WebSocketBidiOutput(websocket)

        await agent.run(inputs=[ws_input], outputs=[ws_output])

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.on_event("startup")
async def startup():
    global _mcp_clients
    mcp_config = load_mcp_config()
    _mcp_clients = create_mcp_clients(mcp_config)
    logger.info(f"Initialized {len(_mcp_clients)} MCP clients")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="info")
