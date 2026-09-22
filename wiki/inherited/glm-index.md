# glm-subagent-mcp

MCP server that delegates work to a **Claude Code** subprocess billed against a **GLM Coding Plan** (Z.ai Anthropic-compatible endpoint).

Parent agent (Claude Code on Anthropic OAuth, Codex, …) keeps its own auth. The child `claude -p` process is isolated: GLM credentials, own `CLAUDE_CONFIG_DIR`, `--strict-mcp-config` and `--setting-sources ""` so it cannot load the parent's MCP servers or settings (D9).

Sibling products:

- [deepseek-subagent-mcp](https://github.com/gaztrabisme/deepseek-subagent-mcp) — same MCP product contract; child runtime is DeepSeek Harness.
- [claude-sub-proxy](https://github.com/gaztrabisme/claude-sub-proxy) — inverse shape: a proxy in front of *this* Claude Code, not an MCP that spawns one.

See `decisions.md` for locked choices.
