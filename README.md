# YNAB MCP

MCP server for [YNAB (You Need A Budget)](https://www.ynab.com/) API.

## Installation

**1. Clone and install:**
```bash
git clone https://github.com/YOUR_USERNAME/ynab-mcp.git
cd ynab-mcp
uv sync
```

**2. Get your YNAB API key:**
- Go to https://app.ynab.com/settings
- Scroll to "Developer Settings"
- Create a new Personal Access Token

**3. Add to Claude Code config** (`~/.claude.json`):
```json
{
  "mcpServers": {
    "ynab": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/ynab-mcp", "python", "ynab_mcp.py"],
      "env": {
        "YNAB_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

**4. Restart Claude Code**

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `YNAB_API_KEY` | Yes | Your YNAB Personal Access Token |
| `YNAB_OUTPUT_DIR` | No | Directory for transaction file output (default: `/tmp/ynab-mcp`) |

## Available Tools

### Budgets
- `ynab_get_budgets` - List all budgets
- `ynab_get_budget_summary` - Get budget overview with accounts and category groups

### Accounts
- `ynab_get_accounts` - List all accounts in a budget
- `ynab_get_account` - Get single account details

### Categories
- `ynab_get_categories` - List all categories
- `ynab_get_category` - Get single category details
- `ynab_update_category_budget` - Update budgeted amount for a category

### Transactions
- `ynab_get_transactions` - Get transactions with optional filters
- `ynab_get_transaction` - Get single transaction
- `ynab_create_transaction` - Create a new transaction
- `ynab_update_transaction` - Update an existing transaction
- `ynab_search_transactions` - Search transactions by payee name or memo

### Payees
- `ynab_get_payees` - List all payees

### Monthly Budgets
- `ynab_get_month_budget` - Get budget details for a specific month

### Scheduled Transactions
- `ynab_get_scheduled_transactions` - List scheduled transactions
- `ynab_create_scheduled_transaction` - Create a scheduled transaction

## Notes

### Amounts
All monetary values use YNAB's milliunits format (1000 = $1.00). The tools accept either:
- `amount_milliunits` (recommended for precision)
- `amount_dollars` (convenience)

Responses include both formats:
```json
{
  "balance_milliunits": 125000,
  "balance": "$125.00"
}
```

### Large Results
Transaction queries can return large amounts of data. Options to manage this:
- `output_to_file=True` (default) - Writes results to a file, returns path
- `output_path` - Specify custom file path
- `summary_only=True` - Returns only count and total, no transaction details
- `since_date` - Filter transactions by date
