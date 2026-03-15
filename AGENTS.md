# AGENTS.md

## Overview

A bidirectional voice/text document-writing assistant. Users speak or type to a conversational agent (Nova Sonic 2) through a browser, which explores their ideas, gathers context, and delegates complex tasks to a powerful subagent (Claude Opus 4.6).

## Agents

### Bidi Agent (Conversational Front-End)

- **Model:** Nova Sonic 2 (`BidiNovaSonicModel`, us-east-1)
- **Role:** Conversational facilitator. Explores the user's intent, asks clarifying questions, reads reference files, looks up external context (GitHub, web, Slack), and decides when to delegate tasks.
- **System prompt:** Configurable via `BIDI_SYSTEM_PROMPT` env var. Default guides a 5-step workflow — explore → gather → discuss → write → iterate. Keeps spoken responses short (1-3 sentences). Passes full unabridged conversation context to the subagent.
- **Tools:**
  - `file_read`, `file_write`, `editor`, `shell` (strands-agents-tools)
  - `use_github` (GitHub GraphQL API tool, requires `GITHUB_TOKEN`)
  - `notebook` (shared scratchpad — tracks topics, references, decisions, structure)
  - `use_agent` (custom tool, delegates to Opus 4.6 subagent)
  - `stop_conversation` (bidi built-in)
  - MCP clients loaded from `~/.kiro/settings/mcp.json` (GitHub, fetch, Slack, Outlook, etc.)
- **Interface:** WebSocket via FastAPI. Accepts `bidi_text_input` and `bidi_audio_input` (PCM 16-bit mono 16kHz). Streams transcript, audio, and `notebook_update` events back.

### Opus Agent (Powerful Subagent)

- **Model:** Claude Opus 4.6 (`BedrockModel`, default region)
- **Role:** Powerful general-purpose agent. Receives full conversation context from the Bidi Agent and executes complex tasks — writing documents, analyzing code, researching topics, making multi-file edits.
- **System prompt:** Configurable via `AGENT_SYSTEM_PROMPT` env var. Default: senior technical writer persona, markdown output, saves to `~/docs/`.
- **Tools:**
  - `file_read`, `file_write`, `editor`, `shell` (strands-agents-tools)
  - `use_github` (GitHub GraphQL API tool, requires `GITHUB_TOKEN`)
  - MCP clients (same config as Bidi Agent)
- **Invocation:** Called via the `use_agent` tool (agent-as-tool pattern). Not directly user-facing.

## Communication Pattern

```
User (browser) <--WebSocket--> Bidi Agent (Nova Sonic 2)
                                    |
                                    |-- reads files, fetches URLs, queries GitHub, etc.
                                    |
                                    |-- use_agent(full_context)
                                          |
                                          v
                                    Opus Agent (Opus 4.6)
                                      executes task, writes files
```

The Bidi Agent accumulates context through conversation and tool use, then passes everything unabridged to the Opus Agent. The Opus Agent is stateless per invocation — it gets one shot with full context.

## Configuration

| Env Var | Description | Default |
|---------|-------------|---------|
| `BIDI_SYSTEM_PROMPT` | System prompt for the Bidi Agent | Built-in doc-writing assistant prompt |
| `AGENT_SYSTEM_PROMPT` | System prompt for the Opus Agent | Built-in technical writer prompt |
| `MCP_CONFIG_PATH` | Path to MCP server config | `~/.kiro/settings/mcp.json` |
| `AWS_DEFAULT_REGION` | AWS region for Bedrock (Opus) | us-west-2 |
| `GITHUB_TOKEN` | GitHub personal access token for `use_github` tool | — |
| `BYPASS_TOOL_CONSENT` | Skip confirmation for GitHub mutations | `false` |
| `SESSIONS_DIR` | Directory for session storage | `./sessions/` |

## Infrastructure

- **Server:** FastAPI + Uvicorn on port 8888
- **UI:** Single-page HTML/JS app (`static/index.html`) with text chat and microphone input
- **MCP:** Shared MCP clients initialized at startup from MCP config
- **Dependencies:** `strands-agents[bidi]`, `strands-agents-tools`, `fastapi`, `uvicorn`, `websockets`, `requests`

## Session Management

Sessions persist conversation history and notebook entries across page refreshes and server restarts.

- **Session ID:** Short UUID (8 chars), passed as `?session_id=` query param on the WebSocket URL and stored in the URL hash for bookmarkability.
- **Conversation history:** Managed by Strands `FileSessionManager` — automatically persists messages, tool calls, and agent state.
- **Notebook:** Persisted as `notebook.json` per session directory.
- **UI:** Session picker overlay on load — lists existing sessions (labeled by first topic note) or creates new ones. Session badge in header allows switching.
- **REST API:**
  - `GET /api/sessions` — list all sessions with metadata
  - `POST /api/sessions` — create a new session

Storage structure:
```
sessions/
└── <session_id>/
    ├── notebook.json
    └── session_<session_id>/   (managed by FileSessionManager)
        ├── session.json
        └── agents/
```

## Agent Refresh

The 🔄 button in the header refreshes the Bidi Agent without losing context:

- Clears conversation messages (fresh BidiAgent, no session manager)
- Preserves notebook entries and files on disk
- Injects notebook contents into the system prompt so the new agent starts with full context
- Useful when the conversation gets long/confused but you want to keep your accumulated notes
