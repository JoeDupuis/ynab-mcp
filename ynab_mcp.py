#!/usr/bin/env python3
import json
import os
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import ynab
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, model_validator, ConfigDict

mcp = FastMCP("ynab_mcp")

CHARACTER_LIMIT = 25000
OUTPUT_DIR = Path(os.environ.get("YNAB_OUTPUT_DIR", "/tmp/ynab-mcp"))


def get_api_client() -> ynab.ApiClient:
    api_key = os.environ.get("YNAB_API_KEY")
    if not api_key:
        raise ValueError("YNAB_API_KEY environment variable is required")
    configuration = ynab.Configuration(access_token=api_key)
    return ynab.ApiClient(configuration)


def milliunits_to_dollars(milliunits: int) -> str:
    dollars = milliunits / 1000
    if dollars < 0:
        return f"-${abs(dollars):,.2f}"
    return f"${dollars:,.2f}"


def dollars_to_milliunits(dollars: float) -> int:
    return int(dollars * 1000)


def transform_amount_fields(obj: dict, fields: list[str]) -> dict:
    result = dict(obj)
    for field in fields:
        if field in result and result[field] is not None:
            milliunits = result[field]
            result[f"{field}_milliunits"] = milliunits
            result[field] = milliunits_to_dollars(milliunits)
    return result


def transform_account(account: dict) -> dict:
    return transform_amount_fields(
        account,
        ["balance", "cleared_balance", "uncleared_balance"]
    )


def transform_category(category: dict) -> dict:
    return transform_amount_fields(
        category,
        ["budgeted", "activity", "balance", "goal_target", "goal_overall_left"]
    )


def transform_transaction(txn: dict) -> dict:
    return transform_amount_fields(txn, ["amount"])


def transform_month(month: dict) -> dict:
    result = transform_amount_fields(
        month,
        ["income", "budgeted", "activity", "to_be_budgeted"]
    )
    if "categories" in result:
        result["categories"] = [transform_category(c) for c in result["categories"]]
    return result


def write_to_file(data: Any, prefix: str, output_path: Optional[str] = None) -> str:
    if output_path:
        filepath = Path(output_path)
        filepath.parent.mkdir(parents=True, exist_ok=True)
    else:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{prefix}_{timestamp}.json"
        filepath = OUTPUT_DIR / filename
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return str(filepath)


def handle_api_error(e: Exception) -> str:
    if isinstance(e, ynab.ApiException):
        if e.status == 401:
            return "Error: Invalid API key. Check YNAB_API_KEY environment variable."
        elif e.status == 403:
            return "Error: Access forbidden. You don't have permission for this resource."
        elif e.status == 404:
            return "Error: Resource not found. Check the ID is correct."
        elif e.status == 429:
            return "Error: Rate limit exceeded. Wait before making more requests."
        return f"Error: YNAB API error {e.status}: {e.reason}"
    return f"Error: {type(e).__name__}: {str(e)}"


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


class BudgetIdInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(
        ...,
        description="The budget ID. Use 'last-used' to get the last accessed budget.",
        min_length=1
    )


class GetBudgetsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    include_accounts: bool = Field(
        default=False,
        description="Include account info in the response"
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )


@mcp.tool(
    name="ynab_get_budgets",
    annotations={
        "title": "List YNAB Budgets",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_get_budgets(params: GetBudgetsInput) -> str:
    """List all budgets the user has access to.

    Returns budget names and IDs. Use the budget_id in other tools.
    """
    try:
        with get_api_client() as api_client:
            api = ynab.BudgetsApi(api_client)
            response = api.get_budgets(include_accounts=params.include_accounts)
            budgets = [b.to_dict() for b in response.data.budgets]

            if params.include_accounts:
                for budget in budgets:
                    if "accounts" in budget and budget["accounts"]:
                        budget["accounts"] = [transform_account(a) for a in budget["accounts"]]

            if params.response_format == ResponseFormat.MARKDOWN:
                lines = ["# YNAB Budgets", ""]
                for b in budgets:
                    lines.append(f"## {b['name']}")
                    lines.append(f"- **ID**: `{b['id']}`")
                    if b.get("last_modified_on"):
                        lines.append(f"- **Last Modified**: {b['last_modified_on']}")
                    if params.include_accounts and b.get("accounts"):
                        lines.append("- **Accounts**:")
                        for acc in b["accounts"]:
                            lines.append(f"  - {acc['name']}: {acc['balance']}")
                    lines.append("")
                return "\n".join(lines)
            else:
                return json.dumps(budgets, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


class GetBudgetSummaryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(
        ...,
        description="The budget ID. Use 'last-used' for most recent.",
        min_length=1
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )


@mcp.tool(
    name="ynab_get_budget_summary",
    annotations={
        "title": "Get Budget Summary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_get_budget_summary(params: GetBudgetSummaryInput) -> str:
    """Get a summary of a budget including accounts and category groups.

    Returns a curated overview - not the full budget export.
    """
    try:
        with get_api_client() as api_client:
            api = ynab.BudgetsApi(api_client)
            response = api.get_budget_by_id(params.budget_id)
            budget = response.data.budget

            cats_by_group = {}
            for cat in (budget.categories or []):
                gid = cat.category_group_id
                if gid not in cats_by_group:
                    cats_by_group[gid] = []
                cats_by_group[gid].append(cat.name)

            summary = {
                "id": budget.id,
                "name": budget.name,
                "last_modified_on": str(budget.last_modified_on) if budget.last_modified_on else None,
                "currency_format": budget.currency_format.to_dict() if budget.currency_format else None,
                "accounts": [transform_account(a.to_dict()) for a in (budget.accounts or [])],
                "category_groups": []
            }

            for cg in (budget.category_groups or []):
                group = {
                    "id": cg.id,
                    "name": cg.name,
                    "hidden": cg.hidden,
                    "categories": cats_by_group.get(cg.id, [])
                }
                summary["category_groups"].append(group)

            if params.response_format == ResponseFormat.MARKDOWN:
                lines = [f"# Budget: {summary['name']}", ""]
                lines.append(f"**ID**: `{summary['id']}`")
                if summary["last_modified_on"]:
                    lines.append(f"**Last Modified**: {summary['last_modified_on']}")
                lines.append("")

                lines.append("## Accounts")
                for acc in summary["accounts"]:
                    status = "🔒" if acc.get("closed") else ""
                    lines.append(f"- **{acc['name']}** {status}: {acc['balance']} (cleared: {acc['cleared_balance']})")
                lines.append("")

                lines.append("## Category Groups")
                for cg in summary["category_groups"]:
                    if not cg["hidden"]:
                        lines.append(f"### {cg['name']}")
                        for cat in cg["categories"]:
                            lines.append(f"  - {cat}")
                        lines.append("")

                return "\n".join(lines)
            else:
                return json.dumps(summary, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


class GetAccountsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )


@mcp.tool(
    name="ynab_get_accounts",
    annotations={
        "title": "List Budget Accounts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_get_accounts(params: GetAccountsInput) -> str:
    """List all accounts in a budget with balances."""
    try:
        with get_api_client() as api_client:
            api = ynab.AccountsApi(api_client)
            response = api.get_accounts(params.budget_id)
            accounts = [transform_account(a.to_dict()) for a in response.data.accounts]

            if params.response_format == ResponseFormat.MARKDOWN:
                lines = ["# Accounts", ""]
                for acc in accounts:
                    status = " (closed)" if acc.get("closed") else ""
                    on_budget = "on-budget" if acc.get("on_budget") else "off-budget"
                    lines.append(f"## {acc['name']}{status}")
                    lines.append(f"- **ID**: `{acc['id']}`")
                    lines.append(f"- **Type**: {acc.get('type', 'unknown')} ({on_budget})")
                    lines.append(f"- **Balance**: {acc['balance']}")
                    lines.append(f"- **Cleared**: {acc['cleared_balance']}")
                    lines.append(f"- **Uncleared**: {acc['uncleared_balance']}")
                    lines.append("")
                return "\n".join(lines)
            else:
                return json.dumps(accounts, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


class GetAccountInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    account_id: str = Field(..., description="The account ID", min_length=1)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.JSON,
        description="Output format: 'markdown' or 'json'"
    )


@mcp.tool(
    name="ynab_get_account",
    annotations={
        "title": "Get Single Account",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_get_account(params: GetAccountInput) -> str:
    """Get details for a single account."""
    try:
        with get_api_client() as api_client:
            api = ynab.AccountsApi(api_client)
            response = api.get_account_by_id(params.budget_id, params.account_id)
            account = transform_account(response.data.account.to_dict())

            if params.response_format == ResponseFormat.MARKDOWN:
                lines = [f"# {account['name']}", ""]
                lines.append(f"**ID**: `{account['id']}`")
                lines.append(f"**Type**: {account.get('type', 'unknown')}")
                lines.append(f"**On Budget**: {'Yes' if account.get('on_budget') else 'No'}")
                lines.append(f"**Closed**: {'Yes' if account.get('closed') else 'No'}")
                lines.append("")
                lines.append("## Balances")
                lines.append(f"- **Balance**: {account['balance']}")
                lines.append(f"- **Cleared**: {account['cleared_balance']}")
                lines.append(f"- **Uncleared**: {account['uncleared_balance']}")
                return "\n".join(lines)
            else:
                return json.dumps(account, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


class GetCategoriesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )


@mcp.tool(
    name="ynab_get_categories",
    annotations={
        "title": "List Budget Categories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_get_categories(params: GetCategoriesInput) -> str:
    """List all categories in a budget grouped by category group."""
    try:
        with get_api_client() as api_client:
            api = ynab.CategoriesApi(api_client)
            response = api.get_categories(params.budget_id)
            groups = []
            for cg in response.data.category_groups:
                group = {
                    "id": cg.id,
                    "name": cg.name,
                    "hidden": cg.hidden,
                    "categories": [transform_category(c.to_dict()) for c in (cg.categories or [])]
                }
                groups.append(group)

            if params.response_format == ResponseFormat.MARKDOWN:
                lines = ["# Categories", ""]
                for g in groups:
                    if g["hidden"]:
                        continue
                    lines.append(f"## {g['name']}")
                    for cat in g["categories"]:
                        if cat.get("hidden"):
                            continue
                        lines.append(f"- **{cat['name']}** (`{cat['id']}`)")
                        lines.append(f"  - Budgeted: {cat['budgeted']} | Activity: {cat['activity']} | Balance: {cat['balance']}")
                    lines.append("")
                return "\n".join(lines)
            else:
                return json.dumps(groups, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


class GetCategoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    category_id: str = Field(..., description="The category ID", min_length=1)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.JSON,
        description="Output format: 'markdown' or 'json'"
    )


@mcp.tool(
    name="ynab_get_category",
    annotations={
        "title": "Get Single Category",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_get_category(params: GetCategoryInput) -> str:
    """Get details for a single category including goal info."""
    try:
        with get_api_client() as api_client:
            api = ynab.CategoriesApi(api_client)
            response = api.get_category_by_id(params.budget_id, params.category_id)
            category = transform_category(response.data.category.to_dict())

            if params.response_format == ResponseFormat.MARKDOWN:
                lines = [f"# {category['name']}", ""]
                lines.append(f"**ID**: `{category['id']}`")
                lines.append(f"**Budgeted**: {category['budgeted']}")
                lines.append(f"**Activity**: {category['activity']}")
                lines.append(f"**Balance**: {category['balance']}")
                if category.get("goal_type"):
                    lines.append("")
                    lines.append("## Goal")
                    lines.append(f"- Type: {category['goal_type']}")
                    if category.get("goal_target"):
                        lines.append(f"- Target: {category['goal_target']}")
                    if category.get("goal_percentage_complete") is not None:
                        lines.append(f"- Progress: {category['goal_percentage_complete']}%")
                return "\n".join(lines)
            else:
                return json.dumps(category, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


class UpdateCategoryBudgetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    category_id: str = Field(..., description="The category ID", min_length=1)
    month: str = Field(
        ...,
        description="The budget month in ISO format (YYYY-MM-DD, use first of month)",
        pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    amount_milliunits: Optional[int] = Field(
        None,
        description="RECOMMENDED. Budgeted amount in milliunits (1000 = $1.00). Mutually exclusive with amount_dollars."
    )
    amount_dollars: Optional[float] = Field(
        None,
        description="Budgeted amount in dollars. Mutually exclusive with amount_milliunits. Use amount_milliunits for precision."
    )

    @model_validator(mode="after")
    def exactly_one_amount(self):
        has_milli = self.amount_milliunits is not None
        has_dollar = self.amount_dollars is not None
        if has_milli == has_dollar:
            raise ValueError("Provide exactly one of amount_milliunits or amount_dollars")
        return self


@mcp.tool(
    name="ynab_update_category_budget",
    annotations={
        "title": "Update Category Budget",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_update_category_budget(params: UpdateCategoryBudgetInput) -> str:
    """Update the budgeted amount for a category in a specific month."""
    try:
        if params.amount_milliunits is not None:
            budgeted = params.amount_milliunits
        else:
            budgeted = dollars_to_milliunits(params.amount_dollars)

        with get_api_client() as api_client:
            api = ynab.CategoriesApi(api_client)
            data = ynab.PatchMonthCategoryWrapper(
                category=ynab.SaveMonthCategory(budgeted=budgeted)
            )
            response = api.update_month_category(
                params.budget_id,
                params.month,
                params.category_id,
                data
            )
            category = transform_category(response.data.category.to_dict())
            return json.dumps({
                "success": True,
                "category": category
            }, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


class GetPayeesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )


@mcp.tool(
    name="ynab_get_payees",
    annotations={
        "title": "List Payees",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_get_payees(params: GetPayeesInput) -> str:
    """List all payees in a budget."""
    try:
        with get_api_client() as api_client:
            api = ynab.PayeesApi(api_client)
            response = api.get_payees(params.budget_id)
            payees = [p.to_dict() for p in response.data.payees]

            if params.response_format == ResponseFormat.MARKDOWN:
                lines = ["# Payees", ""]
                for p in payees:
                    lines.append(f"- **{p['name']}** (`{p['id']}`)")
                return "\n".join(lines)
            else:
                return json.dumps(payees, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


class GetTransactionsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    account_id: Optional[str] = Field(None, description="Filter by account ID")
    category_id: Optional[str] = Field(None, description="Filter by category ID")
    payee_id: Optional[str] = Field(None, description="Filter by payee ID")
    since_date: Optional[str] = Field(
        None,
        description="Only return transactions on or after this date (YYYY-MM-DD)",
        pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    output_to_file: bool = Field(
        default=True,
        description="Write results to file. Recommended for large results to avoid clogging context. Set False only for small result sets."
    )
    output_path: Optional[str] = Field(
        None,
        description="Custom file path for output. If not set, writes to default location with timestamp."
    )
    summary_only: bool = Field(
        default=False,
        description="Return only count and total, without individual transactions. Useful for quick aggregations."
    )


@mcp.tool(
    name="ynab_get_transactions",
    annotations={
        "title": "Get Transactions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_get_transactions(params: GetTransactionsInput) -> str:
    """Get transactions from a budget with optional filters.

    Can return large amounts of data. Defaults to file output to avoid clogging context.
    Use since_date and filters to limit results.
    """
    try:
        with get_api_client() as api_client:
            api = ynab.TransactionsApi(api_client)

            if params.account_id:
                response = api.get_transactions_by_account(
                    params.budget_id,
                    params.account_id,
                    since_date=params.since_date
                )
            elif params.category_id:
                response = api.get_transactions_by_category(
                    params.budget_id,
                    params.category_id,
                    since_date=params.since_date
                )
            elif params.payee_id:
                response = api.get_transactions_by_payee(
                    params.budget_id,
                    params.payee_id,
                    since_date=params.since_date
                )
            else:
                response = api.get_transactions(
                    params.budget_id,
                    since_date=params.since_date
                )

            transactions = [transform_transaction(t.to_dict()) for t in response.data.transactions]

            total_milliunits = sum(t.get("amount_milliunits", 0) for t in transactions)

            if params.summary_only:
                return json.dumps({
                    "count": len(transactions),
                    "total_milliunits": total_milliunits,
                    "total": milliunits_to_dollars(total_milliunits)
                }, indent=2)

            summary = {
                "count": len(transactions),
                "total_milliunits": total_milliunits,
                "total": milliunits_to_dollars(total_milliunits),
                "transactions": transactions
            }

            if params.output_to_file:
                filepath = write_to_file(summary, "transactions", params.output_path)
                return json.dumps({
                    "count": summary["count"],
                    "total_milliunits": summary["total_milliunits"],
                    "total": summary["total"],
                    "output_file": filepath,
                    "message": f"Wrote {summary['count']} transactions to {filepath}"
                }, indent=2)
            else:
                result = json.dumps(summary, indent=2, default=str)
                if len(result) > CHARACTER_LIMIT:
                    filepath = write_to_file(summary, "transactions", params.output_path)
                    return json.dumps({
                        "count": summary["count"],
                        "total_milliunits": summary["total_milliunits"],
                        "total": summary["total"],
                        "output_file": filepath,
                        "message": f"Response too large ({len(result)} chars). Wrote to {filepath}"
                    }, indent=2)
                return result

    except Exception as e:
        return handle_api_error(e)


class GetTransactionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    transaction_id: str = Field(..., description="The transaction ID", min_length=1)


@mcp.tool(
    name="ynab_get_transaction",
    annotations={
        "title": "Get Single Transaction",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_get_transaction(params: GetTransactionInput) -> str:
    """Get details for a single transaction."""
    try:
        with get_api_client() as api_client:
            api = ynab.TransactionsApi(api_client)
            response = api.get_transaction_by_id(params.budget_id, params.transaction_id)
            transaction = transform_transaction(response.data.transaction.to_dict())
            return json.dumps(transaction, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


class CreateTransactionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    account_id: str = Field(..., description="The account ID for this transaction", min_length=1)
    date: str = Field(
        ...,
        description="Transaction date in ISO format (YYYY-MM-DD)",
        pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    amount_milliunits: Optional[int] = Field(
        None,
        description="RECOMMENDED. Amount in milliunits (1000 = $1.00). Negative = outflow, positive = inflow. Mutually exclusive with amount_dollars."
    )
    amount_dollars: Optional[float] = Field(
        None,
        description="Amount in dollars. Negative = outflow, positive = inflow. Mutually exclusive with amount_milliunits. Use amount_milliunits for precision."
    )
    payee_id: Optional[str] = Field(None, description="The payee ID")
    payee_name: Optional[str] = Field(None, description="Payee name (creates new payee if doesn't exist)")
    category_id: Optional[str] = Field(None, description="The category ID")
    memo: Optional[str] = Field(None, description="Transaction memo", max_length=200)
    cleared: Optional[str] = Field(
        default="uncleared",
        description="Cleared status: 'cleared', 'uncleared', or 'reconciled'"
    )
    approved: bool = Field(default=True, description="Whether the transaction is approved")

    @model_validator(mode="after")
    def exactly_one_amount(self):
        has_milli = self.amount_milliunits is not None
        has_dollar = self.amount_dollars is not None
        if has_milli == has_dollar:
            raise ValueError("Provide exactly one of amount_milliunits or amount_dollars")
        return self


@mcp.tool(
    name="ynab_create_transaction",
    annotations={
        "title": "Create Transaction",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
def ynab_create_transaction(params: CreateTransactionInput) -> str:
    """Create a new transaction in YNAB."""
    try:
        if params.amount_milliunits is not None:
            amount = params.amount_milliunits
        else:
            amount = dollars_to_milliunits(params.amount_dollars)

        with get_api_client() as api_client:
            api = ynab.TransactionsApi(api_client)

            txn = ynab.NewTransaction(
                account_id=params.account_id,
                date=params.date,
                amount=amount,
                payee_id=params.payee_id,
                payee_name=params.payee_name,
                category_id=params.category_id,
                memo=params.memo,
                cleared=params.cleared,
                approved=params.approved
            )

            data = ynab.PostTransactionsWrapper(transaction=txn)
            response = api.create_transaction(params.budget_id, data)

            result = {
                "success": True,
                "transaction": transform_transaction(response.data.transaction.to_dict())
            }
            return json.dumps(result, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


class UpdateTransactionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    transaction_id: str = Field(..., description="The transaction ID to update", min_length=1)
    account_id: Optional[str] = Field(None, description="Move to different account")
    date: Optional[str] = Field(
        None,
        description="New date in ISO format (YYYY-MM-DD)",
        pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    amount_milliunits: Optional[int] = Field(
        None,
        description="RECOMMENDED. New amount in milliunits. Mutually exclusive with amount_dollars."
    )
    amount_dollars: Optional[float] = Field(
        None,
        description="New amount in dollars. Mutually exclusive with amount_milliunits."
    )
    payee_id: Optional[str] = Field(None, description="New payee ID")
    payee_name: Optional[str] = Field(None, description="New payee name")
    category_id: Optional[str] = Field(None, description="New category ID")
    memo: Optional[str] = Field(None, description="New memo", max_length=200)
    cleared: Optional[str] = Field(None, description="New cleared status")
    approved: Optional[bool] = Field(None, description="New approved status")

    @model_validator(mode="after")
    def at_most_one_amount(self):
        has_milli = self.amount_milliunits is not None
        has_dollar = self.amount_dollars is not None
        if has_milli and has_dollar:
            raise ValueError("Provide at most one of amount_milliunits or amount_dollars")
        return self


@mcp.tool(
    name="ynab_update_transaction",
    annotations={
        "title": "Update Transaction",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_update_transaction(params: UpdateTransactionInput) -> str:
    """Update an existing transaction."""
    try:
        with get_api_client() as api_client:
            api = ynab.TransactionsApi(api_client)

            update_data = {}
            if params.account_id:
                update_data["account_id"] = params.account_id
            if params.date:
                update_data["date"] = params.date
            if params.amount_milliunits is not None:
                update_data["amount"] = params.amount_milliunits
            elif params.amount_dollars is not None:
                update_data["amount"] = dollars_to_milliunits(params.amount_dollars)
            if params.payee_id:
                update_data["payee_id"] = params.payee_id
            if params.payee_name:
                update_data["payee_name"] = params.payee_name
            if params.category_id:
                update_data["category_id"] = params.category_id
            if params.memo is not None:
                update_data["memo"] = params.memo
            if params.cleared:
                update_data["cleared"] = params.cleared
            if params.approved is not None:
                update_data["approved"] = params.approved

            txn = ynab.ExistingTransaction(**update_data)
            data = ynab.PutTransactionWrapper(transaction=txn)
            response = api.update_transaction(params.budget_id, params.transaction_id, data)

            result = {
                "success": True,
                "transaction": transform_transaction(response.data.transaction.to_dict())
            }
            return json.dumps(result, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


class SearchTransactionsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    query: str = Field(
        ...,
        description="Search query to match against payee name or memo",
        min_length=1
    )
    since_date: Optional[str] = Field(
        None,
        description="Only search transactions on or after this date (YYYY-MM-DD)",
        pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    output_to_file: bool = Field(
        default=True,
        description="Write results to file. Recommended for large results to avoid clogging context. Set False only for small result sets."
    )
    output_path: Optional[str] = Field(
        None,
        description="Custom file path for output. If not set, writes to default location with timestamp."
    )
    summary_only: bool = Field(
        default=False,
        description="Return only count and total, without individual transactions. Useful for quick aggregations."
    )


@mcp.tool(
    name="ynab_search_transactions",
    annotations={
        "title": "Search Transactions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_search_transactions(params: SearchTransactionsInput) -> str:
    """Search transactions by payee name or memo.

    Fetches transactions and filters in Python. Use since_date to limit scope.
    """
    try:
        with get_api_client() as api_client:
            api = ynab.TransactionsApi(api_client)
            response = api.get_transactions(params.budget_id, since_date=params.since_date)

            query_lower = params.query.lower()
            matches = []
            for t in response.data.transactions:
                payee_name = (t.payee_name or "").lower()
                memo = (t.memo or "").lower()
                if query_lower in payee_name or query_lower in memo:
                    matches.append(transform_transaction(t.to_dict()))

            total_milliunits = sum(t.get("amount_milliunits", 0) for t in matches)

            if params.summary_only:
                return json.dumps({
                    "query": params.query,
                    "count": len(matches),
                    "total_milliunits": total_milliunits,
                    "total": milliunits_to_dollars(total_milliunits)
                }, indent=2)

            summary = {
                "query": params.query,
                "count": len(matches),
                "total_milliunits": total_milliunits,
                "total": milliunits_to_dollars(total_milliunits),
                "transactions": matches
            }

            if params.output_to_file:
                filepath = write_to_file(summary, "search_transactions", params.output_path)
                return json.dumps({
                    "query": params.query,
                    "count": summary["count"],
                    "total_milliunits": summary["total_milliunits"],
                    "total": summary["total"],
                    "output_file": filepath,
                    "message": f"Found {summary['count']} matching transactions. Wrote to {filepath}"
                }, indent=2)
            else:
                result = json.dumps(summary, indent=2, default=str)
                if len(result) > CHARACTER_LIMIT:
                    filepath = write_to_file(summary, "search_transactions", params.output_path)
                    return json.dumps({
                        "query": params.query,
                        "count": summary["count"],
                        "total_milliunits": summary["total_milliunits"],
                        "total": summary["total"],
                        "output_file": filepath,
                        "message": f"Response too large. Wrote to {filepath}"
                    }, indent=2)
                return result

    except Exception as e:
        return handle_api_error(e)


class GetMonthBudgetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    month: str = Field(
        ...,
        description="The budget month (YYYY-MM-DD, use first of month)",
        pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )


@mcp.tool(
    name="ynab_get_month_budget",
    annotations={
        "title": "Get Month Budget",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_get_month_budget(params: GetMonthBudgetInput) -> str:
    """Get budget details for a specific month including category allocations and activity."""
    try:
        with get_api_client() as api_client:
            api = ynab.MonthsApi(api_client)
            response = api.get_budget_month(params.budget_id, params.month)
            month = transform_month(response.data.month.to_dict())

            if params.response_format == ResponseFormat.MARKDOWN:
                lines = [f"# Budget: {month['month']}", ""]
                lines.append(f"**Income**: {month['income']}")
                lines.append(f"**Budgeted**: {month['budgeted']}")
                lines.append(f"**Activity**: {month['activity']}")
                lines.append(f"**To Be Budgeted**: {month['to_be_budgeted']}")
                if month.get("age_of_money"):
                    lines.append(f"**Age of Money**: {month['age_of_money']} days")
                lines.append("")

                lines.append("## Categories")
                for cat in month.get("categories", []):
                    if cat.get("hidden"):
                        continue
                    lines.append(f"### {cat['name']}")
                    lines.append(f"- Budgeted: {cat['budgeted']}")
                    lines.append(f"- Activity: {cat['activity']}")
                    lines.append(f"- Balance: {cat['balance']}")
                    lines.append("")

                return "\n".join(lines)
            else:
                return json.dumps(month, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


class GetScheduledTransactionsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )


@mcp.tool(
    name="ynab_get_scheduled_transactions",
    annotations={
        "title": "List Scheduled Transactions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def ynab_get_scheduled_transactions(params: GetScheduledTransactionsInput) -> str:
    """List all scheduled (recurring) transactions in a budget."""
    try:
        with get_api_client() as api_client:
            api = ynab.ScheduledTransactionsApi(api_client)
            response = api.get_scheduled_transactions(params.budget_id)
            transactions = [transform_transaction(t.to_dict()) for t in response.data.scheduled_transactions]

            if params.response_format == ResponseFormat.MARKDOWN:
                lines = ["# Scheduled Transactions", ""]
                for t in transactions:
                    lines.append(f"## {t.get('payee_name', 'Unknown Payee')}")
                    lines.append(f"- **ID**: `{t['id']}`")
                    lines.append(f"- **Amount**: {t['amount']}")
                    lines.append(f"- **Frequency**: {t.get('frequency', 'unknown')}")
                    lines.append(f"- **Next Date**: {t.get('date_next', 'unknown')}")
                    if t.get("memo"):
                        lines.append(f"- **Memo**: {t['memo']}")
                    lines.append("")
                return "\n".join(lines)
            else:
                return json.dumps(transactions, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


class CreateScheduledTransactionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    budget_id: str = Field(..., description="The budget ID", min_length=1)
    account_id: str = Field(..., description="The account ID", min_length=1)
    date_first: str = Field(
        ...,
        description="First occurrence date (YYYY-MM-DD)",
        pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    frequency: str = Field(
        ...,
        description="Frequency: 'never', 'daily', 'weekly', 'everyOtherWeek', 'twiceAMonth', 'every4Weeks', 'monthly', 'everyOtherMonth', 'every3Months', 'every4Months', 'twiceAYear', 'yearly', 'everyOtherYear'"
    )
    amount_milliunits: Optional[int] = Field(
        None,
        description="RECOMMENDED. Amount in milliunits. Mutually exclusive with amount_dollars."
    )
    amount_dollars: Optional[float] = Field(
        None,
        description="Amount in dollars. Mutually exclusive with amount_milliunits."
    )
    payee_id: Optional[str] = Field(None, description="The payee ID")
    payee_name: Optional[str] = Field(None, description="Payee name")
    category_id: Optional[str] = Field(None, description="The category ID")
    memo: Optional[str] = Field(None, description="Memo", max_length=200)

    @model_validator(mode="after")
    def exactly_one_amount(self):
        has_milli = self.amount_milliunits is not None
        has_dollar = self.amount_dollars is not None
        if has_milli == has_dollar:
            raise ValueError("Provide exactly one of amount_milliunits or amount_dollars")
        return self


@mcp.tool(
    name="ynab_create_scheduled_transaction",
    annotations={
        "title": "Create Scheduled Transaction",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
def ynab_create_scheduled_transaction(params: CreateScheduledTransactionInput) -> str:
    """Create a new scheduled (recurring) transaction."""
    try:
        if params.amount_milliunits is not None:
            amount = params.amount_milliunits
        else:
            amount = dollars_to_milliunits(params.amount_dollars)

        with get_api_client() as api_client:
            api = ynab.ScheduledTransactionsApi(api_client)

            txn = ynab.SaveScheduledTransaction(
                account_id=params.account_id,
                date=params.date_first,
                frequency=params.frequency,
                amount=amount,
                payee_id=params.payee_id,
                payee_name=params.payee_name,
                category_id=params.category_id,
                memo=params.memo
            )

            data = ynab.PostScheduledTransactionWrapper(scheduled_transaction=txn)
            response = api.create_scheduled_transaction(params.budget_id, data)

            result = {
                "success": True,
                "scheduled_transaction": transform_transaction(response.data.scheduled_transaction.to_dict())
            }
            return json.dumps(result, indent=2, default=str)

    except Exception as e:
        return handle_api_error(e)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
