#!/usr/bin/env python3
"""Doc-Writing Bidi Agent Server.

Architecture:
  Browser (WebSocket) <-> FastAPI <-> BidiAgent (Nova Sonic 2)
                                         |
                                         v
                                   writer_agent (Opus 4.6, agent-as-tool)
                                     - file_read, file_write, editor, shell
                                     - MCP servers (GitHub, fetch, etc.)

The bidi agent is the conversational front-end. It explores your thinking,
asks questions, and gathers context. When you're ready to write, it calls
the writer subagent with the accumulated context and references.
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
"""

# Global MCP clients (initialized once)
_mcp_clients: list[MCPClient] = []


def get_writer_agent() -> Agent:
    """Create a fresh writer agent with Opus 4.6 and MCP tools."""
    model = BedrockModel(
        model_id="global.anthropic.claude-opus-4-6-v1",
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
    )
    tools = [file_read, file_write, editor, shell]
    tools.extend(_mcp_clients)
    return Agent(model=model, tools=tools, system_prompt=WRITER_SYSTEM_PROMPT)


@tool
def write_document(context: str, instructions: str, output_path: str = "") -> str:
    """Write a document based on conversation context and instructions.

    This tool delegates to a powerful writer agent (Claude Opus 4.6) that has
    access to file system tools and MCP servers (GitHub, fetch, etc.).

    Args:
        context: The accumulated context from the conversation - what the doc
                 is about, key points, opinions, references, file paths, links.
        instructions: Specific writing instructions - style, structure, length,
                      audience, format preferences.
        output_path: Optional file path to save the document. If empty, the
                     writer will choose an appropriate path under ~/docs/.

    Returns:
        The written document content and the path where it was saved.
    """
    try:
        writer = get_writer_agent()
        prompt = f"""Write a document based on the following:

## Context
{context}

## Instructions
{instructions}

## Output Path
{output_path if output_path else "Choose an appropriate path under ~/docs/"}

Write the complete document and save it to the specified path.
Return the full document content and confirm where it was saved.
"""
        result = writer(prompt)
        return str(result)
    except Exception as e:
        return f"Error writing document: {e}"


@tool
def read_reference(file_path: str) -> str:
    """Read a file to gather context for document writing.

    Use this when the user mentions a file they want to reference or include
    in the document.

    Args:
        file_path: Path to the file to read (supports ~ expansion).

    Returns:
        The file contents.
    """
    try:
        path = Path(file_path).expanduser()
        if not path.exists():
            return f"File not found: {file_path}"
        return path.read_text(encoding="utf-8")[:50000]  # Cap at 50k chars
    except Exception as e:
        return f"Error reading {file_path}: {e}"


# --- Bidi Agent System Prompt ---

BIDI_SYSTEM_PROMPT = """\
You are a document writing assistant. Your role is to help the user think through
and write documents.

## Your Workflow:
1. EXPLORE: Ask questions to understand what the user wants to write. What's the
   topic? Who's the audience? What's the goal? What style?
2. GATHER: Help the user identify references - files to read, links to include,
   data to reference. Use read_reference to pull in file contents.
3. DISCUSS: Explore the user's opinions and ideas. Push back gently, suggest
   structure, identify gaps.
4. WRITE: When the user is ready, use write_document to create the document.
   Pass ALL the context you've gathered and the user's specific instructions.
5. ITERATE: After writing, discuss the output. For small changes, describe them.
   For big changes, call write_document again with updated instructions.

## Important:
- Be conversational and natural. Ask one question at a time.
- Keep your spoken responses SHORT (1-3 sentences). You're talking, not writing.
- When calling write_document, be THOROUGH in the context you pass - include
  everything discussed, all references, all opinions, all links.
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
        model = BidiNovaSonicModel(
            model_id="amazon.nova-sonic-v1:0",
            provider_config={
                "audio": {"voice": "tiffany"},
            },
            client_config={"region": "us-east-1"},
        )

        agent = BidiAgent(
            model=model,
            tools=[write_document, read_reference, stop_conversation],
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
