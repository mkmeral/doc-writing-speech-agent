#!/bin/bash
# Copy-paste into Android terminal. Fill in tokens first.

GITHUB_TOKEN="" && \
PERPLEXITY_API_KEY="pplx-" && \
AWS_BEARER_TOKEN_BEDROCK="" && \
AWS_DEFAULT_REGION="us-west-2" && \
AGENT_CONTEXT="User is Murat Kaan Meral (murmeral, mkmeral), a developer on Strands Agents — an open-source AI agent SDK by AWS. Key repos: sdk-python, sdk-typescript, tools, docs, evals (all under strands-agents org, forks under mkmeral). Strands core: model + system prompt + tools. Supports Bedrock, MCP, OpenTelemetry, multi-agent patterns. Rules: be concise, talk naturally, no long lists unless asked." && \
sudo apt install -y python3 python3-pip git nodejs npm tmux -qq && \
GIT_TERMINAL_PROMPT=0 pip3 install -q --break-system-packages "git+https://github.com/mkmeral/doc-writing-speech-agent.git" requests && \
mkdir -p "$HOME/.config/mcp" && \
cat > "$HOME/.config/mcp/mcp.json" << EOF
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
tmux kill-session -t bidi 2>/dev/null; \
tmux new-session -d -s bidi "\
  export GITHUB_TOKEN='$GITHUB_TOKEN' && \
  export AWS_BEARER_TOKEN_BEDROCK='$AWS_BEARER_TOKEN_BEDROCK' && \
  export AWS_DEFAULT_REGION='$AWS_DEFAULT_REGION' && \
  export MCP_CONFIG_PATH='$HOME/.config/mcp/mcp.json' && \
  export BYPASS_TOOL_CONSENT='true' && \
  export AGENT_CONTEXT='$AGENT_CONTEXT' && \
  doc-writer && bash" && \
echo "Done! http://localhost:8888 | tmux attach -t bidi"
