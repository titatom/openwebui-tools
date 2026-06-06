# Firefly III tool for Open WebUI

An Open WebUI **Tool** that lets an LLM read from and (optionally) write to a self-hosted
[Firefly III](https://www.firefly-iii.org/) personal finance instance via its REST API.

## Features

**Read (always available):**
- `test_connection` – verify URL + token, returns version and user email
- `get_basic_summary` – net worth, spent, earned, balance, bills, left to spend
- `get_expense_insight_by_category` / `get_income_insight_by_category`
- `list_accounts`, `get_account`
- `list_transactions`, `search_transactions`, `get_transaction`
- `list_budgets`, `list_categories`, `list_bills`, `list_piggy_banks`

**Write (guarded — disabled by default, confirmation required):**
- `create_transaction`
- `update_transaction`
- `delete_transaction`

## Setup

1. In Open WebUI: **Workspace → Tools → +**, paste the contents of
   [`firefly_iii_tool.py`](./firefly_iii_tool.py), and save. (`httpx` is a dependency.)
2. Set the global **`base_url`** in the tool's Valves, e.g. `https://firefly.example.com`
   (no trailing slash, no `/api`).
3. Each user sets their **`personal_access_token`** in the tool's **User Valves**.
   Create one in Firefly III under **Options → Profile → OAuth → Personal Access Tokens**.
4. (Optional) To allow writes, flip **`enable_write_operations`** to `True`. Every
   create/update/delete still prompts the user for confirmation.

## Configuration

### Valves (global / admin)
| Valve | Default | Description |
|---|---|---|
| `base_url` | `""` | Base URL of the Firefly III instance |
| `verify_ssl` | `True` | Set `False` only for self-signed certs |
| `request_timeout` | `30` | HTTP timeout in seconds |
| `default_page_limit` | `25` | Items per page for list endpoints |
| `max_items` | `100` | Hard cap across paginated results |
| `enable_write_operations` | `False` | Master switch for create/update/delete |

### User Valves (per-user)
| Valve | Description |
|---|---|
| `personal_access_token` | Per-user Firefly III PAT (password-masked) |
| `base_url_override` | Optional per-user base URL override |

## Notes

- Authentication uses the Firefly III **Bearer token** scheme (Personal Access Token).
- Built for Open WebUI **Native (Agentic) function-calling mode**: output is returned
  directly, progress is shown via `status` events, and writes use `confirmation` events.
- Amounts are handled as strings to avoid floating-point rounding issues.
- For production / multi-worker deployments, pre-install `httpx` in your image rather
  than relying on the `requirements` frontmatter line.

## Possible future additions (v2)

Rules, recurring transactions, webhooks, attachments, account creation, and
piggy-bank top-ups are not included in v1.

## License

MIT
