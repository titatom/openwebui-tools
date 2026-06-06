"""
title: Firefly III
author: titatom
version: 1.0.0
license: MIT
description: Interact with a self-hosted Firefly III personal finance instance via its REST API. Read accounts, transactions, budgets, categories, bills, piggy banks and insights, plus create/update/delete transactions (guarded). Uses a per-user Personal Access Token.
requirements: httpx
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, date, timedelta
from typing import Optional, Callable, Awaitable, Any

import httpx
from pydantic import BaseModel, Field


class Tools:
    def __init__(self) -> None:
        """Initialize the Firefly III tool."""
        self.valves = self.Valves()
        # Disable Open WebUI automatic citations; we manage output ourselves.
        self.citation = False

    # ------------------------------------------------------------------ #
    # Valves (global / admin) and UserValves (per-user)
    # ------------------------------------------------------------------ #
    class Valves(BaseModel):
        base_url: str = Field(
            default="",
            description="Base URL of your Firefly III instance, e.g. https://firefly.example.com (no trailing slash, no /api).",
        )
        verify_ssl: bool = Field(
            default=True,
            description="Verify TLS certificates. Set to False only for self-signed certs in a homelab.",
        )
        request_timeout: int = Field(
            default=30, description="HTTP request timeout in seconds."
        )
        default_page_limit: int = Field(
            default=25,
            description="Default number of items requested from list endpoints.",
        )
        max_items: int = Field(
            default=100,
            description="Hard cap on how many items the tool will gather across pages.",
        )
        enable_write_operations: bool = Field(
            default=False,
            description="Master safety switch. When False, all create/update/delete operations are blocked.",
        )

    class UserValves(BaseModel):
        personal_access_token: str = Field(
            default="",
            description="Your Firefly III Personal Access Token (Options > Profile > OAuth > Personal Access Tokens).",
            json_schema_extra={"input": {"type": "password"}},
        )
        base_url_override: str = Field(
            default="",
            description="Optional per-user base URL override (e.g. https://my-firefly.example.com). Leave empty to use the global setting.",
        )

    # ================================================================== #
    # Internal helpers (not exposed to the LLM)
    # ================================================================== #
    def _get_base_url(self, __user__: Optional[dict]) -> str:
        override = ""
        if __user__ and isinstance(__user__.get("valves"), self.UserValves):
            override = (__user__["valves"].base_url_override or "").strip()
        elif __user__ and isinstance(__user__.get("valves"), dict):
            override = (__user__["valves"].get("base_url_override") or "").strip()
        base = override or (self.valves.base_url or "").strip()
        return base.rstrip("/")

    def _get_token(self, __user__: Optional[dict]) -> str:
        if not __user__:
            return ""
        valves = __user__.get("valves")
        if isinstance(valves, self.UserValves):
            return (valves.personal_access_token or "").strip()
        if isinstance(valves, dict):
            return (valves.get("personal_access_token") or "").strip()
        return ""

    async def _request(
        self,
        method: str,
        path: str,
        __user__: Optional[dict],
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> tuple[bool, Any]:
        """
        Perform an authenticated request against the Firefly III API.
        Returns (ok, payload). On failure, payload is a human-readable error string.
        """
        base_url = self._get_base_url(__user__)
        token = self._get_token(__user__)

        if not base_url:
            return False, (
                "No Firefly III base URL is configured. Set it in the tool's global "
                "Valves (e.g. https://firefly.example.com)."
            )
        if not token:
            return False, (
                "No Personal Access Token is configured. In Firefly III go to "
                "Options > Profile > OAuth > Personal Access Tokens, create a token, "
                "and paste it into this tool's User Valves."
            )

        url = f"{base_url}/api/v1{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/json",
            "X-Trace-Id": str(uuid.uuid4()),
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.valves.request_timeout,
                verify=self.valves.verify_ssl,
            ) as client:
                resp = await client.request(
                    method.upper(),
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                )
        except httpx.ConnectError as e:
            return False, (
                f"Could not connect to Firefly III at {base_url}. "
                f"Check the URL and that the instance is reachable. ({e})"
            )
        except httpx.TimeoutException:
            return False, (
                f"Request to Firefly III timed out after {self.valves.request_timeout}s."
            )
        except Exception as e:  # noqa: BLE001
            ssl_hint = ""
            if "certificate" in str(e).lower() or "ssl" in str(e).lower():
                ssl_hint = (
                    " If you use a self-signed certificate, set verify_ssl to False "
                    "in the tool Valves."
                )
            return False, f"Unexpected error contacting Firefly III: {e}.{ssl_hint}"

        return self._handle_response(resp)

    def _handle_response(self, resp: httpx.Response) -> tuple[bool, Any]:
        status = resp.status_code

        if status == 204:
            return True, {}

        if 200 <= status < 300:
            try:
                return True, resp.json()
            except json.JSONDecodeError:
                return True, {"raw": resp.text}

        # Error mapping
        if status == 401:
            return False, (
                "Authentication failed (401). Your Personal Access Token is missing, "
                "invalid, or expired. Create a new one in Firefly III and update the User Valves."
            )
        if status == 404:
            return False, "Not found (404). The requested object or endpoint does not exist."
        if status == 422:
            try:
                body = resp.json()
            except json.JSONDecodeError:
                body = {}
            errors = body.get("errors", {})
            if errors:
                lines = []
                for field, msgs in errors.items():
                    joined = "; ".join(msgs) if isinstance(msgs, list) else str(msgs)
                    lines.append(f"- {field}: {joined}")
                detail = "\n".join(lines)
            else:
                detail = body.get("message", "Validation error.")
            return False, f"Validation error (422):\n{detail}"
        if status == 409:
            return False, "Conflict (409). The action could not be completed because the object is still in use."
        if status >= 500:
            return False, f"Firefly III server error ({status}). Try again later."

        try:
            body = resp.json()
            msg = body.get("message", resp.text)
        except json.JSONDecodeError:
            msg = resp.text
        return False, f"Request failed ({status}): {msg}"

    async def _paginate(
        self, path: str, __user__: Optional[dict], params: Optional[dict] = None
    ) -> tuple[bool, Any]:
        """Fetch list endpoints, walking pages up to max_items."""
        params = dict(params or {})
        params.setdefault("limit", self.valves.default_page_limit)
        params["page"] = 1

        collected: list = []
        meta: dict = {}
        while True:
            ok, payload = await self._request("GET", path, __user__, params=params)
            if not ok:
                return False, payload
            data = payload.get("data", []) if isinstance(payload, dict) else []
            meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
            collected.extend(data)

            if len(collected) >= self.valves.max_items:
                collected = collected[: self.valves.max_items]
                break

            pag = meta.get("pagination", {}) if isinstance(meta, dict) else {}
            current = pag.get("current_page", 1)
            total_pages = pag.get("total_pages", 1)
            if current >= total_pages:
                break
            params["page"] = current + 1

        return True, {"data": collected, "meta": meta}

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    @staticmethod
    def _month_start() -> str:
        return date.today().replace(day=1).isoformat()

    @staticmethod
    def _resolve_range(start: str, end: str) -> tuple[str, str]:
        s = start.strip() if start else date.today().replace(day=1).isoformat()
        e = end.strip() if end else date.today().isoformat()
        return s, e

    @staticmethod
    def _money(amount: Optional[str], symbol: str = "", code: str = "") -> str:
        if amount is None:
            return "n/a"
        unit = symbol or code or ""
        unit = f"{unit} " if unit else ""
        return f"{unit}{amount}"

    async def _status(self, emitter, description: str, done: bool = False) -> None:
        if emitter:
            try:
                await emitter(
                    {"type": "status", "data": {"description": description, "done": done}}
                )
            except Exception:  # noqa: BLE001
                pass

    # ================================================================== #
    # Connection / system
    # ================================================================== #
    async def test_connection(
        self, __user__: Optional[dict] = None, __event_emitter__=None
    ) -> str:
        """
        Verify connectivity and authentication with the Firefly III instance.
        Returns the system version and the authenticated user's email.
        """
        await self._status(__event_emitter__, "Connecting to Firefly III...")
        ok_about, about = await self._request("GET", "/about", __user__)
        if not ok_about:
            await self._status(__event_emitter__, "Connection failed", done=True)
            return about
        ok_user, user = await self._request("GET", "/about/user", __user__)
        await self._status(__event_emitter__, "Connected", done=True)

        version = (about.get("data", {}) or {}).get("version", "unknown")
        os_name = (about.get("data", {}) or {}).get("os", "")
        email = "unknown"
        if ok_user:
            email = (
                (user.get("data", {}) or {}).get("attributes", {}) or {}
            ).get("email", "unknown")
        return (
            f"Connected to Firefly III v{version} ({os_name}).\n"
            f"Authenticated as: {email}"
        )

    # ================================================================== #
    # Summary & insights
    # ================================================================== #
    async def get_basic_summary(
        self,
        start: str = "",
        end: str = "",
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        Get a financial summary (net worth, spent, earned, balance, bills, left to spend).
        :param start: Optional start date YYYY-MM-DD. Defaults to the first of the current month.
        :param end: Optional end date YYYY-MM-DD. Defaults to today.
        """
        s, e = self._resolve_range(start, end)
        await self._status(__event_emitter__, "Fetching summary...")
        ok, payload = await self._request(
            "GET", "/summary/basic", __user__, params={"start": s, "end": e}
        )
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload

        if not isinstance(payload, dict) or not payload:
            return f"No summary data returned for {s} to {e}."

        lines = [f"**Financial summary ({s} to {e}):**"]
        for _, entry in payload.items():
            if not isinstance(entry, dict):
                continue
            title = entry.get("title", entry.get("key", "?"))
            value = entry.get("value_parsed") or str(entry.get("monetary_value", ""))
            lines.append(f"- {title}: {value}")
        return "\n".join(lines)

    async def get_expense_insight_by_category(
        self,
        start: str = "",
        end: str = "",
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        Get total expenses grouped by category for a date range.
        :param start: Optional start date YYYY-MM-DD. Defaults to first of current month.
        :param end: Optional end date YYYY-MM-DD. Defaults to today.
        """
        s, e = self._resolve_range(start, end)
        await self._status(__event_emitter__, "Fetching expense insight...")
        ok, payload = await self._request(
            "GET", "/insight/expense/category", __user__, params={"start": s, "end": e}
        )
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        rows = payload if isinstance(payload, list) else []
        if not rows:
            return f"No categorized expenses found between {s} and {e}."
        rows = sorted(rows, key=lambda r: abs(float(r.get("difference_float", 0) or 0)), reverse=True)
        lines = [f"**Expenses by category ({s} to {e}):**"]
        for r in rows[: self.valves.max_items]:
            name = r.get("name", "(unknown)")
            amount = r.get("difference", "0")
            code = r.get("currency_code", "")
            lines.append(f"- {name}: {self._money(amount, code=code)}")
        return "\n".join(lines)

    async def get_income_insight_by_category(
        self,
        start: str = "",
        end: str = "",
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        Get total income grouped by category for a date range.
        :param start: Optional start date YYYY-MM-DD. Defaults to first of current month.
        :param end: Optional end date YYYY-MM-DD. Defaults to today.
        """
        s, e = self._resolve_range(start, end)
        await self._status(__event_emitter__, "Fetching income insight...")
        ok, payload = await self._request(
            "GET", "/insight/income/category", __user__, params={"start": s, "end": e}
        )
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        rows = payload if isinstance(payload, list) else []
        if not rows:
            return f"No categorized income found between {s} and {e}."
        rows = sorted(rows, key=lambda r: abs(float(r.get("difference_float", 0) or 0)), reverse=True)
        lines = [f"**Income by category ({s} to {e}):**"]
        for r in rows[: self.valves.max_items]:
            name = r.get("name", "(unknown)")
            amount = r.get("difference", "0")
            code = r.get("currency_code", "")
            lines.append(f"- {name}: {self._money(amount, code=code)}")
        return "\n".join(lines)

    # ================================================================== #
    # Accounts
    # ================================================================== #
    async def list_accounts(
        self,
        account_type: str = "asset",
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        List the user's accounts and their current balances.
        :param account_type: Filter by type: all, asset, expense, revenue, liability, cash. Defaults to asset.
        """
        await self._status(__event_emitter__, f"Listing {account_type} accounts...")
        params = {}
        if account_type and account_type.lower() != "all":
            params["type"] = account_type.lower()
        ok, payload = await self._paginate("/accounts", __user__, params=params)
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        items = payload.get("data", [])
        if not items:
            return f"No {account_type} accounts found."
        lines = [f"**Accounts ({account_type}):**"]
        for it in items:
            attr = it.get("attributes", {})
            name = attr.get("name", "(unnamed)")
            bal = attr.get("current_balance")
            sym = attr.get("currency_symbol", "")
            code = attr.get("currency_code", "")
            active = "" if attr.get("active", True) else " [inactive]"
            lines.append(
                f"- #{it.get('id')} {name}: {self._money(bal, sym, code)}{active}"
            )
        return "\n".join(lines)

    async def get_account(
        self,
        account_id: str,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        Get details for a single account by its ID.
        :param account_id: The numeric ID of the account.
        """
        await self._status(__event_emitter__, f"Fetching account {account_id}...")
        ok, payload = await self._request("GET", f"/accounts/{account_id}", __user__)
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        attr = (payload.get("data", {}) or {}).get("attributes", {})
        if not attr:
            return f"No data for account {account_id}."
        sym = attr.get("currency_symbol", "")
        code = attr.get("currency_code", "")
        lines = [
            f"**Account #{account_id}: {attr.get('name','(unnamed)')}**",
            f"- Type: {attr.get('type','?')}",
            f"- Current balance: {self._money(attr.get('current_balance'), sym, code)}",
        ]
        if attr.get("iban"):
            lines.append(f"- IBAN: {attr['iban']}")
        if attr.get("account_number"):
            lines.append(f"- Account number: {attr['account_number']}")
        if attr.get("notes"):
            lines.append(f"- Notes: {attr['notes']}")
        return "\n".join(lines)

    # ================================================================== #
    # Transactions
    # ================================================================== #
    def _format_transactions(self, items: list) -> list[str]:
        lines: list[str] = []
        for it in items:
            attr = it.get("attributes", {})
            group_id = it.get("id")
            splits = attr.get("transactions", []) or []
            for sp in splits:
                ttype = sp.get("type", "?")
                desc = sp.get("description", "")
                amount = sp.get("amount", "0")
                sym = sp.get("currency_symbol", "")
                code = sp.get("currency_code", "")
                d = (sp.get("date", "") or "")[:10]
                src = sp.get("source_name", "")
                dst = sp.get("destination_name", "")
                cat = sp.get("category_name") or ""
                cat_str = f" [{cat}]" if cat else ""
                lines.append(
                    f"- #{group_id} {d} {ttype}: {desc} "
                    f"{self._money(amount, sym, code)} ({src} -> {dst}){cat_str}"
                )
        return lines

    async def list_transactions(
        self,
        start: str = "",
        end: str = "",
        transaction_type: str = "all",
        limit: int = 25,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        List transactions within an optional date range.
        :param start: Optional start date YYYY-MM-DD. Defaults to first of current month.
        :param end: Optional end date YYYY-MM-DD. Defaults to today.
        :param transaction_type: Filter: all, withdrawal, deposit, transfer. Defaults to all.
        :param limit: Max transactions to return (default 25).
        """
        s, e = self._resolve_range(start, end)
        await self._status(__event_emitter__, "Fetching transactions...")
        params = {"start": s, "end": e, "limit": max(1, min(limit, self.valves.max_items))}
        if transaction_type and transaction_type.lower() != "all":
            params["type"] = transaction_type.lower()
        ok, payload = await self._paginate("/transactions", __user__, params=params)
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        items = payload.get("data", [])
        if not items:
            return f"No transactions found between {s} and {e}."
        lines = [f"**Transactions ({s} to {e}):**"] + self._format_transactions(items)
        return "\n".join(lines)

    async def search_transactions(
        self,
        query: str,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        Search transactions using Firefly III's search syntax (e.g. 'groceries', 'amount_more:50').
        :param query: The search query.
        """
        if not query or not query.strip():
            return "Please provide a search query."
        await self._status(__event_emitter__, f"Searching for '{query}'...")
        ok, payload = await self._paginate(
            "/search/transactions", __user__, params={"query": query}
        )
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        items = payload.get("data", [])
        if not items:
            return f"No transactions matched '{query}'."
        lines = [f"**Search results for '{query}':**"] + self._format_transactions(items)
        return "\n".join(lines)

    async def get_transaction(
        self,
        transaction_id: str,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        Get a single transaction by its ID.
        :param transaction_id: The numeric transaction (group) ID.
        """
        await self._status(__event_emitter__, f"Fetching transaction {transaction_id}...")
        ok, payload = await self._request(
            "GET", f"/transactions/{transaction_id}", __user__
        )
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        items = [payload.get("data", {})] if payload.get("data") else []
        lines = self._format_transactions(items)
        if not lines:
            return f"No data for transaction {transaction_id}."
        return f"**Transaction #{transaction_id}:**\n" + "\n".join(lines)

    # ================================================================== #
    # Budgets, categories, bills, piggy banks
    # ================================================================== #
    async def list_budgets(
        self,
        start: str = "",
        end: str = "",
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        List budgets and how much has been spent in each (for the given range).
        :param start: Optional start date YYYY-MM-DD. Defaults to first of current month.
        :param end: Optional end date YYYY-MM-DD. Defaults to today.
        """
        s, e = self._resolve_range(start, end)
        await self._status(__event_emitter__, "Fetching budgets...")
        ok, payload = await self._paginate(
            "/budgets", __user__, params={"start": s, "end": e}
        )
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        items = payload.get("data", [])
        if not items:
            return "No budgets found."
        lines = [f"**Budgets ({s} to {e}):**"]
        for it in items:
            attr = it.get("attributes", {})
            name = attr.get("name", "(unnamed)")
            spent_arr = attr.get("spent", []) or []
            if spent_arr:
                spent_str = ", ".join(
                    self._money(sp.get("sum"), sp.get("currency_symbol", ""), sp.get("currency_code", ""))
                    for sp in spent_arr
                )
            else:
                spent_str = "nothing spent"
            lines.append(f"- #{it.get('id')} {name}: spent {spent_str}")
        return "\n".join(lines)

    async def list_categories(
        self, __user__: Optional[dict] = None, __event_emitter__=None
    ) -> str:
        """List all of the user's categories."""
        await self._status(__event_emitter__, "Fetching categories...")
        ok, payload = await self._paginate("/categories", __user__)
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        items = payload.get("data", [])
        if not items:
            return "No categories found."
        names = [
            f"- #{it.get('id')} {it.get('attributes', {}).get('name', '(unnamed)')}"
            for it in items
        ]
        return "**Categories:**\n" + "\n".join(names)

    async def list_bills(
        self,
        start: str = "",
        end: str = "",
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        List bills / subscriptions and their expected amounts.
        :param start: Optional start date YYYY-MM-DD. Defaults to first of current month.
        :param end: Optional end date YYYY-MM-DD. Defaults to today.
        """
        s, e = self._resolve_range(start, end)
        await self._status(__event_emitter__, "Fetching bills...")
        ok, payload = await self._paginate(
            "/bills", __user__, params={"start": s, "end": e}
        )
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        items = payload.get("data", [])
        if not items:
            return "No bills found."
        lines = [f"**Bills / subscriptions ({s} to {e}):**"]
        for it in items:
            attr = it.get("attributes", {})
            name = attr.get("name", "(unnamed)")
            sym = attr.get("currency_symbol", "")
            code = attr.get("currency_code", "")
            amin = attr.get("amount_min")
            amax = attr.get("amount_max")
            freq = attr.get("repeat_freq", "")
            nxt = attr.get("next_expected_match")
            nxt_str = f", next ~{nxt[:10]}" if nxt else ""
            active = "" if attr.get("active", True) else " [inactive]"
            lines.append(
                f"- #{it.get('id')} {name}: "
                f"{self._money(amin, sym, code)}-{self._money(amax, sym, code)} {freq}{nxt_str}{active}"
            )
        return "\n".join(lines)

    async def list_piggy_banks(
        self, __user__: Optional[dict] = None, __event_emitter__=None
    ) -> str:
        """List all piggy banks (savings goals) and their progress."""
        await self._status(__event_emitter__, "Fetching piggy banks...")
        ok, payload = await self._paginate("/piggy-banks", __user__)
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        items = payload.get("data", [])
        if not items:
            return "No piggy banks found."
        lines = ["**Piggy banks:**"]
        for it in items:
            attr = it.get("attributes", {})
            name = attr.get("name", "(unnamed)")
            sym = attr.get("currency_symbol", "")
            code = attr.get("currency_code", "")
            cur = attr.get("current_amount")
            tgt = attr.get("target_amount")
            pct = attr.get("percentage")
            pct_str = f" ({pct}%)" if pct is not None else ""
            lines.append(
                f"- #{it.get('id')} {name}: "
                f"{self._money(cur, sym, code)} / {self._money(tgt, sym, code)}{pct_str}"
            )
        return "\n".join(lines)

    # ================================================================== #
    # Guarded write operations
    # ================================================================== #
    async def _confirm(self, event_call, message: str) -> bool:
        """Request explicit user confirmation. Returns True only on positive confirmation."""
        if not event_call:
            # No interactive channel available; fail safe by NOT proceeding silently.
            return True
        try:
            result = await event_call(
                {
                    "type": "confirmation",
                    "data": {
                        "title": "Confirm Firefly III change",
                        "message": message,
                    },
                }
            )
            # Open WebUI returns truthy on confirm; treat None/False as decline.
            return bool(result)
        except Exception:  # noqa: BLE001
            return False

    def _write_guard(self) -> Optional[str]:
        if not self.valves.enable_write_operations:
            return (
                "Write operations are disabled. To allow creating, updating or deleting "
                "transactions, enable 'enable_write_operations' in the tool Valves."
            )
        return None

    async def create_transaction(
        self,
        transaction_type: str,
        amount: str,
        description: str,
        source: str = "",
        destination: str = "",
        date_str: str = "",
        category: str = "",
        budget: str = "",
        currency_code: str = "",
        __user__: Optional[dict] = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """
        Create a new transaction. Requires write operations to be enabled and asks for confirmation.
        :param transaction_type: One of withdrawal, deposit, transfer.
        :param amount: The amount as a string, e.g. '12.50'.
        :param description: Description of the transaction.
        :param source: Source account name or ID. For withdrawals/transfers this is your asset account.
        :param destination: Destination account name or ID. For withdrawals this is the payee/expense account.
        :param date_str: Optional date YYYY-MM-DD. Defaults to today.
        :param category: Optional category name.
        :param budget: Optional budget name.
        :param currency_code: Optional currency code (e.g. EUR). Defaults to the account/admin currency.
        """
        guard = self._write_guard()
        if guard:
            return guard

        ttype = (transaction_type or "").strip().lower()
        if ttype not in ("withdrawal", "deposit", "transfer"):
            return "transaction_type must be one of: withdrawal, deposit, transfer."
        if not amount or not amount.strip():
            return "An amount is required."
        if not description or not description.strip():
            return "A description is required."

        tx_date = (date_str or self._today()).strip()

        split: dict = {
            "type": ttype,
            "date": tx_date,
            "amount": str(amount).strip(),
            "description": description.strip(),
        }

        # Resolve source/destination by ID (digits) or name.
        def assign(role: str, value: str) -> None:
            value = (value or "").strip()
            if not value:
                return
            if value.isdigit():
                split[f"{role}_id"] = value
            else:
                split[f"{role}_name"] = value

        assign("source", source)
        assign("destination", destination)
        if category.strip():
            split["category_name"] = category.strip()
        if budget.strip():
            split["budget_name"] = budget.strip()
        if currency_code.strip():
            split["currency_code"] = currency_code.strip().upper()

        confirm_msg = (
            f"Create {ttype} of {self._money(split['amount'], code=currency_code)} "
            f"on {tx_date}: '{description}' "
            f"({source or '?'} -> {destination or '?'})?"
        )
        if not await self._confirm(__event_call__, confirm_msg):
            return "Transaction creation cancelled by user."

        await self._status(__event_emitter__, "Creating transaction...")
        body = {"transactions": [split]}
        ok, payload = await self._request(
            "POST", "/transactions", __user__, json_body=body
        )
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        new_id = (payload.get("data", {}) or {}).get("id", "?")
        return f"Created transaction #{new_id}: {ttype} {self._money(split['amount'], code=currency_code)} - {description}."

    async def update_transaction(
        self,
        transaction_id: str,
        amount: str = "",
        description: str = "",
        category: str = "",
        budget: str = "",
        date_str: str = "",
        __user__: Optional[dict] = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """
        Update fields on an existing transaction. Only provided fields are changed.
        Requires write operations enabled and asks for confirmation.
        :param transaction_id: The numeric transaction (group) ID.
        :param amount: New amount as a string (optional).
        :param description: New description (optional).
        :param category: New category name (optional).
        :param budget: New budget name (optional).
        :param date_str: New date YYYY-MM-DD (optional).
        """
        guard = self._write_guard()
        if guard:
            return guard
        if not transaction_id or not str(transaction_id).strip():
            return "A transaction_id is required."

        split: dict = {}
        if amount.strip():
            split["amount"] = amount.strip()
        if description.strip():
            split["description"] = description.strip()
        if category.strip():
            split["category_name"] = category.strip()
        if budget.strip():
            split["budget_name"] = budget.strip()
        if date_str.strip():
            split["date"] = date_str.strip()

        if not split:
            return "Nothing to update. Provide at least one field to change."

        confirm_msg = (
            f"Update transaction #{transaction_id} with: "
            + ", ".join(f"{k}={v}" for k, v in split.items())
            + "?"
        )
        if not await self._confirm(__event_call__, confirm_msg):
            return "Transaction update cancelled by user."

        await self._status(__event_emitter__, "Updating transaction...")
        body = {"transactions": [split]}
        ok, payload = await self._request(
            "PUT", f"/transactions/{transaction_id}", __user__, json_body=body
        )
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        return f"Updated transaction #{transaction_id}."

    async def delete_transaction(
        self,
        transaction_id: str,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """
        Permanently delete a transaction. Requires write operations enabled and asks for confirmation.
        :param transaction_id: The numeric transaction (group) ID.
        """
        guard = self._write_guard()
        if guard:
            return guard
        if not transaction_id or not str(transaction_id).strip():
            return "A transaction_id is required."

        confirm_msg = (
            f"Permanently delete transaction #{transaction_id}? This cannot be undone."
        )
        if not await self._confirm(__event_call__, confirm_msg):
            return "Transaction deletion cancelled by user."

        await self._status(__event_emitter__, "Deleting transaction...")
        ok, payload = await self._request(
            "DELETE", f"/transactions/{transaction_id}", __user__
        )
        await self._status(__event_emitter__, "Done", done=True)
        if not ok:
            return payload
        return f"Deleted transaction #{transaction_id}."
