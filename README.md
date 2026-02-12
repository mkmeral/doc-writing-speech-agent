# Doc Writing Bidi Agent

A bidirectional voice/text agent for writing documents, powered by:
- **Nova Sonic 2** — conversational front-end (bidi streaming)
- **Claude Opus 4.6** — writer subagent (agent-as-tool)

## Architecture

```
Browser (WebSocket) <-> FastAPI <-> BidiAgent (Nova Sonic 2)
                                        |
                                        v
                                  write_document (tool)
                                        |
                                        v
                                  Writer Agent (Opus 4.6)
                                   - file_read, file_write, editor, shell
                                   - MCP servers (GitHub, fetch, Slack, etc.)
```

## How It Works

1. **Talk or type** to the bidi agent through the web UI
2. **Agent explores** your thinking — asks questions, gathers context
3. **Read references** — mention files and the agent reads them
4. **Write** — when ready, the agent calls the writer subagent with all context
5. **Iterate** — review the doc, discuss changes, rewrite as needed

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Make sure portaudio is installed (macOS)
brew install portaudio

# AWS credentials must be configured (for both Nova Sonic and Opus)
export AWS_DEFAULT_REGION=us-west-2
```

## Run

```bash
python server.py
# Open http://localhost:8888
```

## MCP Config

The writer subagent loads MCP servers from `~/.kiro/settings/mcp.json`.
This gives it access to GitHub, fetch, Slack, Outlook, etc.

## Notes

- Nova Sonic requires `us-east-1` region (configured in server.py)
- Opus 4.6 uses your default AWS region for Bedrock
- The web UI supports both text chat and voice (microphone)
- Audio is streamed as PCM 16-bit mono at 16kHz
