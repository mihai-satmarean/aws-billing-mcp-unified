# AWS Billing MCP — Unified (Agent Context)

Extends `awslabs/billing-cost-management-mcp-server` with Skylite-specific tools.

## Upstream

- Upstream repo: `awslabs/mcp` → `src/billing-cost-management-mcp-server/`
- Upstream remote: `https://github.com/awslabs/mcp.git` (fetch-only, no push)
- Our fork: `https://github.com/mihai-satmarean/aws-billing-mcp-unified`

## Architecture

- **Entry point**: `awslabs/billing_cost_management_mcp_server/server.py`
- **Upstream tools**: `awslabs/billing_cost_management_mcp_server/tools/` (don't modify — sync from upstream)
- **Custom tools**: `awslabs/billing_cost_management_mcp_server/tools/skylite_tools.py` ← EDIT HERE
- **Archive**: `../archive/skylitetek-original/` — original custom server for reference

## Custom tools (skylite_tools.py)

| Tool | Description |
|---|---|
| `get_cost_by_tag` | Cost filtered by tag key/value + NetUnblendedCost (credits-aware) |
| `get_monthly_costs` | Monthly costs by service, N months back |
| `get_credits_analysis` | Credits per month + breakdown by service |
| `read_credits_from_cur` | Credit details from CUR S3 export |
| `get_invoice_detail` | Cost breakdown for a specific month |
| `list_aws_invoices` | List invoice IDs with dates and amounts |
| `get_invoice_pdf_url` | Pre-signed download URL for invoice PDF |
| `download_invoice_pdf` | Download invoice PDF to local file |
| `get_cost_by_account` | Costs grouped by linked account |

## Sync upstream changes

```bash
# Fetch latest from awslabs
git fetch upstream main

# See what changed in the billing server
git diff HEAD upstream/main -- src/billing-cost-management-mcp-server/

# Cherry-pick or merge specific upstream changes (NOT our skylite_tools.py)
git checkout upstream/main -- src/billing-cost-management-mcp-server/awslabs/
git checkout HEAD -- src/billing-cost-management-mcp-server/awslabs/billing_cost_management_mcp_server/tools/skylite_tools.py

# Push update to our fork
git add . && git commit -m "chore: sync upstream billing-cost-management-mcp-server" && git push origin main
```

## Contribute back to upstream

1. Fork `awslabs/mcp` on GitHub (separate from this repo)
2. Copy `skylite_tools.py` logic into a PR targeting `awslabs/mcp`
3. Ensure it follows upstream coding standards (ruff, pyright, Google docstrings)

## Development

```bash
cd /Users/mihai/Documents/tech_workspace/shared_tech/ai_workspace/mcps/aws_billing_unified_mcp/aws-billing
uv pip install -e ".[dev]"
AWS_PROFILE=mihai_skylitetek_root uv run aws-billing-mcp-unified
```

## Cursor MCP config

```json
"aws-billing": {
  "command": "/usr/local/bin/uv",
  "args": ["run", "--directory",
    "/Users/mihai/Documents/tech_workspace/shared_tech/ai_workspace/mcps/aws_billing_unified_mcp/aws-billing",
    "aws-billing-mcp-unified"],
  "env": {
    "AWS_PROFILE": "mihai_skylitetek_root",
    "FASTMCP_LOG_LEVEL": "ERROR"
  }
}
```
