#!/data/data/com.termux/files/usr/bin/bash
# Setup & run doc-writing-bidi on Android (Termux)
# Copy-paste this entire script into Termux.
set -e

# ============================================================
# CONFIGURE THESE — fill in your tokens before running
# ============================================================
GITHUB_TOKEN=""
PERPLEXITY_API_KEY="pplx-"
AWS_BEARER_TOKEN_BEDROCK=""
AWS_DEFAULT_REGION="us-west-2"

AGENT_CONTEXT="User is Murat Kaan Meral (murmeral, mkmeral), a developer on Strands Agents — an open-source AI agent SDK by AWS.

Key repos (read-only, do NOT push to originals):
- sdk-python: github.com/strands-agents/sdk-python (fork: mkmeral/sdk-python)
- sdk-typescript: github.com/strands-agents/sdk-typescript (fork: mkmeral/sdk-typescript)
- tools: github.com/strands-agents/tools (fork: mkmeral/tools)
- docs: github.com/strands-agents/docs (fork: mkmeral/strands-docs)
- evals: github.com/strands-agents/evals (fork: mkmeral/evals)
- personal: github.com/mkmeral/containerized-strands-agents

Strands Agents core concepts: model + system prompt + tools. Supports Bedrock, MCP, OpenTelemetry, multi-agent patterns (swarm, graph, delegation).

Rules: be concise, never produce long lists unless asked, talk naturally."

# ============================================================
# INSTALL DEPENDENCIES
# ============================================================
echo "==> Updating packages..."
pkg update -y
pkg install -y python python-pip git nodejs-lts tmux

echo "==> Installing doc-writing-bidi..."
pip install "git+https://github.com/mkmeral/doc-writing-bidi.git" requests

# ============================================================
# MCP CONFIG
# ============================================================
echo "==> Writing MCP config..."
MCP_DIR="$HOME/.config/mcp"
mkdir -p "$MCP_DIR"

cat > "$MCP_DIR/mcp.json" << EOF
{
  "mcpServers": {
    "perplexity": {
      "command": "npx",
      "args": ["-y", "@perplexity-ai/mcp-server"],
      "env": {
        "PERPLEXITY_API_KEY": "$PERPLEXITY_API_KEY"
      }
    },
    "strands-agents": {
      "command": "uvx",
      "args": ["strands-agents-mcp-server"]
    }
  }
}
EOF

# ============================================================
# RUN IN TMUX
# ============================================================
echo "==> Starting server in tmux session 'bidi'..."
tmux kill-session -t bidi 2>/dev/null || true

tmux new-session -d -s bidi "\
  export GITHUB_TOKEN='$GITHUB_TOKEN' && \
  export AWS_BEARER_TOKEN_BEDROCK='$AWS_BEARER_TOKEN_BEDROCK' && \
  export AWS_DEFAULT_REGION='$AWS_DEFAULT_REGION' && \
  export MCP_CONFIG_PATH='$MCP_DIR/mcp.json' && \
  export BYPASS_TOOL_CONSENT='true' && \
  export AGENT_CONTEXT='$AGENT_CONTEXT' && \
  doc-writer && \
  bash"

echo ""
echo "==> Done! Server running in tmux session 'bidi'"
echo "    Open: http://localhost:8888"
echo ""
echo "    tmux attach -t bidi       # view logs"
echo "    tmux kill-session -t bidi  # stop"
