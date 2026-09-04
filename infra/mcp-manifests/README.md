# MCP manifests (allowlist)

Repository-managed allowlist location for approved STDIO MCP server manifests.

## Rules

- Repository-managed allowlist only
- Arbitrary command forbidden
- Actual STDIO execution occurs in `mcp-worker` only (not in the API process)
- Manifest YAML files will be added with real MCP integration work

Do not place unapproved or ad-hoc executable manifests here during Infra Baseline.
