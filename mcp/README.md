# Per-role MCP allowlists

A role uses an MCP config only when its `mcp` key in `pipeline.toml` names a file here (`<name>.json`, Claude Code `--mcp-config` format). Sessions always run with `--strict-mcp-config`, so nothing else loads — not the target repo's `.mcp.json`, not user config.

Rules:
- Read-only servers only. Never a server that can write to GitHub or act outside the workspace; all writes go through the `publish` stage.
- A token placed in the agent container is visible to the model. Use read-only tokens that are safe to leak.
- Each remote server needs a matching egress rule (chart value `egress.extraFQDNs` / `egress.extraServices`).
