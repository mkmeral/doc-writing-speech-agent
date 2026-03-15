#!/usr/bin/env python3
"""Doc-Writing Bidi Agent Server.

Architecture:
  Browser (WebSocket) <-> FastAPI <-> BidiAgent (Nova Sonic 2)
                                         |-- file_read, file_write, editor, shell
                                         |-- MCP tools (GitHub, fetch, etc.)
                                         |-- stop_conversation
                                         |
                                         v
                                   use_agent (tool) -> Agent (Opus 4.6)
                                     - file_read, file_write, editor, shell
                                     - MCP servers (GitHub, fetch, etc.)

The bidi agent has full tool access — it can read files, look up GitHub PRs,
fetch web pages, etc. during conversation. It delegates complex tasks (writing,
analysis, multi-file edits) to a more powerful Opus 4.6 agent via use_agent.
"""

import asyncio
import base64
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from strands import Agent, tool
from strands.experimental.bidi import BidiAgent
from strands.experimental.bidi.models import BidiNovaSonicModel
from strands.experimental.bidi.tools import stop_conversation
from strands.experimental.bidi.types.events import BidiAudioInputEvent, BidiOutputEvent, BidiTextInputEvent
from strands.experimental.bidi.types.io import BidiInput, BidiOutput
from strands.models.bedrock import BedrockModel
from strands.session.file_session_manager import FileSessionManager

# MCP imports
from mcp import stdio_client, StdioServerParameters
from strands.tools.mcp import MCPClient
from strands_tools import file_read, file_write, editor, shell, http_request
from tools.use_github import use_github

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class AgentRefreshRequested(Exception):
    """Raised by WebSocketBidiInput when the client requests an agent refresh."""
    pass

# --- MCP Config ---
DEFAULT_MCP_CONFIG = os.getenv("MCP_CONFIG_PATH", str(Path.home() / ".kiro" / "settings" / "mcp.json"))
SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", str(Path(__file__).parent / "sessions")))
SESSIONS_DIR.mkdir(exist_ok=True)


def _notebook_path(session_id: str) -> Path:
    """Get the notebook file path for a session."""
    session_dir = SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / "notebook.json"


def _load_notebook(session_id: str) -> list[dict[str, str]]:
    """Load notebook entries from disk."""
    path = _notebook_path(session_id)
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load notebook for session {session_id}: {e}")
    return []


def _save_notebook(session_id: str, entries: list[dict[str, str]]) -> None:
    """Save notebook entries to disk."""
    path = _notebook_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(entries, f, indent=2)


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

DEFAULT_AGENT_SYSTEM_PROMPT = """\
You are a powerful general-purpose assistant (Claude Opus 4.6). You receive full
context from a conversational agent and execute complex tasks — writing documents,
analyzing code, researching topics, making multi-file edits, etc.

Rules:
- Infer the user's voice and style from context.
- Use markdown formatting for documents.
- Be thorough but concise.
- When writing documents, save with file_write. Default path: ~/docs/.
- Use http_request to fetch web pages, APIs, or any URL when needed.
- You have access to file tools, GitHub, and MCP servers for additional lookups.
"""

AGENT_SYSTEM_PROMPT = os.getenv("AGENT_SYSTEM_PROMPT", DEFAULT_AGENT_SYSTEM_PROMPT)
AGENT_CONTEXT = os.getenv("AGENT_CONTEXT", "")

# Global MCP clients (initialized once, shared by both bidi and writer agents)
_mcp_clients: list[MCPClient] = []


def get_agent() -> Agent:
    """Create a fresh agent (Opus 4.6) with MCP tools."""
    model = BedrockModel(model_id="global.anthropic.claude-opus-4-6-v1")
    tools = [file_read, file_write, editor, shell, use_github, http_request]
    tools.extend(_mcp_clients)
    system_prompt = AGENT_SYSTEM_PROMPT
    if AGENT_CONTEXT:
        system_prompt += f"\n\n--- USER CONTEXT ---\n{AGENT_CONTEXT}\n--- END USER CONTEXT ---\n"
    return Agent(model=model, tools=tools, system_prompt=system_prompt)


@tool
def use_agent(prompt: str) -> str:
    """Delegate a task to a powerful agent (Claude Opus 4.6).

    This agent has access to file_read, file_write, editor, shell, and
    MCP servers (GitHub, fetch, etc.). Use it for complex tasks that benefit
    from a more capable model — writing documents, analyzing code, researching
    topics, making multi-file edits, etc.

    Pass the FULL conversation context as the prompt — everything the user said,
    all references read, all opinions discussed, all links mentioned. Do NOT
    summarize or abstract — give the agent the complete picture.

    Args:
        prompt: The complete prompt for the agent. Include all relevant context:
                conversation history, file contents, user preferences, instructions,
                links, references, and desired output.

    Returns:
        The agent's response.
    """
    try:
        agent = get_agent()
        # Automatically include notebook contents for context
        if _notebook_entries:
            nb_lines = [f"- [{e['category']}] {e['content']}" for e in _notebook_entries]
            notebook_context = "\n\n--- NOTEBOOK (shared context from conversation) ---\n" + "\n".join(nb_lines) + "\n--- END NOTEBOOK ---\n"
            prompt = notebook_context + "\n" + prompt
        result = agent(prompt)
        return str(result)
    except Exception as e:
        return f"Error from agent: {e}"



# --- Shared Notebook ---
# Per-session notebook: list of entries. The active websocket and session_id are
# stored so the notebook tool can push updates to the UI and persist to disk.

_notebook_entries: list[dict[str, str]] = []
_active_websocket: WebSocket | None = None
_active_session_id: str | None = None


@tool
def notebook(action: str, category: str = "", content: str = "") -> str:
    """Manage the shared notebook — a running scratchpad for tracking what to write.

    Use this to keep notes throughout the conversation. The notebook is shared with
    the Opus agent when you call use_agent, so it has full context.

    Actions:
        - "add": Add a new entry. Requires category and content.
        - "read": Return all notebook entries.
        - "clear": Clear all entries.

    Categories (for "add"):
        - "topic": What the document is about
        - "audience": Who it's for
        - "reference": A file, link, PR, or source read during conversation
        - "decision": Something the user decided or an opinion expressed
        - "structure": Outline, section ideas, or organization notes
        - "style": Tone, voice, formatting preferences
        - "todo": Something still to figure out or look up
        - "note": General note

    Args:
        action: One of "add", "read", "clear"
        category: Category tag for the entry (required for "add")
        content: The note content (required for "add")

    Returns:
        Confirmation or the full notebook contents.
    """
    global _notebook_entries, _active_websocket, _active_session_id

    if action == "add":
        entry = {"category": category, "content": content}
        _notebook_entries.append(entry)
        # Persist to disk
        if _active_session_id:
            _save_notebook(_active_session_id, _notebook_entries)
        # Push update to UI
        if _active_websocket:
            try:
                asyncio.get_event_loop().create_task(
                    _active_websocket.send_json({
                        "type": "notebook_update",
                        "entries": _notebook_entries,
                    })
                )
            except Exception:
                pass
        return f"Added [{category}]: {content}"

    elif action == "read":
        if not _notebook_entries:
            return "Notebook is empty."
        lines = []
        for i, e in enumerate(_notebook_entries, 1):
            lines.append(f"{i}. [{e['category']}] {e['content']}")
        return "\n".join(lines)

    elif action == "clear":
        _notebook_entries = []
        # Persist to disk
        if _active_session_id:
            _save_notebook(_active_session_id, _notebook_entries)
        if _active_websocket:
            try:
                asyncio.get_event_loop().create_task(
                    _active_websocket.send_json({
                        "type": "notebook_update",
                        "entries": [],
                    })
                )
            except Exception:
                pass
        return "Notebook cleared."

    return f"Unknown action: {action}. Use 'add', 'read', or 'clear'."


# --- Bidi Agent System Prompt ---

DEFAULT_BIDI_SYSTEM_PROMPT = """\
You are a conversational AI assistant. You help the user with anything — writing,
research, coding, brainstorming, analysis, or just thinking things through.

## Style:
- Talk naturally, like a smart colleague. Short sentences.
- Keep responses to 1-3 sentences unless the user asks for detail.
- Never produce long lists or paragraphs unprompted. Be concise.
- Ask clarifying questions when the request is ambiguous.

## Your Tools:
- file_read, file_write, editor, shell — file and system access
- http_request — fetch any URL, API, or web page
- use_github — query GitHub (PRs, issues, repos, etc.)
- notebook — shared scratchpad to track context (topics, references, decisions, todos)
- use_agent — delegate complex tasks to a powerful Opus 4.6 agent
- MCP tools — additional integrations (Perplexity, Slack, etc.)

## Notebook:
Use the notebook tool to jot down important context as you go — topics, references,
decisions, todos. It persists across refreshes and is automatically shared with the
Opus agent when you delegate tasks.

## Delegation:
For complex tasks (writing long documents, multi-file edits, deep analysis), use
use_agent. Pass the FULL conversation context — everything discussed, every file
read, every opinion. The notebook is auto-included, but pass conversation context too.

## Important:
- Be conversational. Don't lecture.
- Use tools proactively — read files, look things up, fetch URLs.
- Use notebook(action="add") to remember things as you learn them.
- Don't write long documents yourself in speech. Delegate with use_agent.
"""

BIDI_SYSTEM_PROMPT = os.getenv("BIDI_SYSTEM_PROMPT", DEFAULT_BIDI_SYSTEM_PROMPT)


# --- WebSocket I/O ---

class WebSocketBidiInput(BidiInput):
    """Read text and audio input from WebSocket client."""

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self._running = True

    async def start(self, agent: BidiAgent) -> None:
        pass

    async def __call__(self):
        while self._running:
            try:
                data = await self.websocket.receive_json()
                msg_type = data.get("type", "")

                if msg_type == "bidi_audio_input":
                    return BidiAudioInputEvent(
                        audio=data["audio"],
                        format=data.get("format", "pcm"),
                        sample_rate=data.get("sample_rate", 16000),
                        channels=data.get("channels", 1),
                    )
                elif msg_type == "bidi_text_input":
                    return BidiTextInputEvent(
                        text=data["text"],
                        role=data.get("role", "user"),
                    )
                elif msg_type == "refresh_agent":
                    raise AgentRefreshRequested()
                else:
                    logger.warning(f"Unknown input type: {msg_type}")
                    continue

            except (WebSocketDisconnect, AgentRefreshRequested):
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
async def websocket_endpoint(websocket: WebSocket, session_id: str = Query(default=None)):
    global _active_websocket, _notebook_entries, _active_session_id
    await websocket.accept()

    # Generate or reuse session ID
    if not session_id:
        session_id = str(uuid.uuid4())[:8]

    _active_session_id = session_id
    _active_websocket = websocket
    _notebook_entries = _load_notebook(session_id)

    logger.info(f"WebSocket client connected — session={session_id}")

    # Send session info + restored notebook to client
    try:
        await websocket.send_json({"type": "session_info", "session_id": session_id})
        if _notebook_entries:
            await websocket.send_json({"type": "notebook_update", "entries": _notebook_entries})
    except Exception:
        pass

    # Loop: create agent, run until refresh or disconnect
    while True:
        try:
            model = BidiNovaSonicModel()

            # Build system prompt — on refresh, prepend notebook context so the
            # fresh agent starts with awareness of prior decisions.
            system_prompt = BIDI_SYSTEM_PROMPT
            if AGENT_CONTEXT:
                system_prompt += f"\n\n--- USER CONTEXT ---\n{AGENT_CONTEXT}\n--- END USER CONTEXT ---\n"
            if _notebook_entries:
                nb_lines = [f"- [{e['category']}] {e['content']}" for e in _notebook_entries]
                notebook_preamble = (
                    "\n\n--- NOTEBOOK (context from prior conversation) ---\n"
                    + "\n".join(nb_lines)
                    + "\n--- END NOTEBOOK ---\n"
                    + "\nThe user has refreshed the agent. You have a clean conversation but the notebook "
                    + "above contains all prior context. Greet the user briefly and confirm you have the context.\n"
                )
                system_prompt = system_prompt + notebook_preamble

            agent = BidiAgent(
                model=model,
                tools=[file_read, file_write, editor, shell, use_github, http_request, notebook, use_agent, stop_conversation] + _mcp_clients,
                system_prompt=system_prompt,
            )

            ws_input = WebSocketBidiInput(websocket)
            ws_output = WebSocketBidiOutput(websocket)

            await agent.run(inputs=[ws_input], outputs=[ws_output])
            break  # Normal exit (e.g. stop_conversation)

        except AgentRefreshRequested:
            logger.info(f"Agent refresh requested — session={session_id}")
            # Reload notebook from disk in case it was updated
            _notebook_entries = _load_notebook(session_id)
            try:
                await websocket.send_json({"type": "agent_refreshed", "session_id": session_id})
            except Exception:
                pass
            continue  # Loop back to create fresh agent

        except WebSocketDisconnect:
            logger.info(f"WebSocket client disconnected — session={session_id}")
            break
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            break

    _active_websocket = None
    _active_session_id = None
    try:
        await websocket.close()
    except Exception:
        pass


@app.get("/api/sessions")
async def list_sessions():
    """List all sessions with metadata."""
    sessions = []
    for session_dir in sorted(SESSIONS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not session_dir.is_dir():
            continue
        sid = session_dir.name
        nb = _load_notebook(sid)
        # Extract a label from notebook topics if available
        topics = [e["content"] for e in nb if e.get("category") == "topic"]
        sessions.append({
            "session_id": sid,
            "label": topics[0] if topics else sid,
            "notebook_count": len(nb),
            "modified": session_dir.stat().st_mtime,
        })
    return {"sessions": sessions}


@app.post("/api/sessions")
async def create_session():
    """Create a new session and return its ID."""
    session_id = str(uuid.uuid4())[:8]
    (SESSIONS_DIR / session_id).mkdir(parents=True, exist_ok=True)
    return {"session_id": session_id}


@app.on_event("startup")
async def startup():
    global _mcp_clients
    mcp_config = load_mcp_config()
    _mcp_clients = create_mcp_clients(mcp_config)
    logger.info(f"Initialized {len(_mcp_clients)} MCP clients")


def main():
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="info")


if __name__ == "__main__":
    main()
