# Zoho Analytics MCP Server

A [FastMCP](https://github.com/jlowin/fastmcp) server that exposes **Zoho Analytics** as a set of AI-callable tools. It supports creating and editing interactive chart reports, querying data via SQL, and browsing workspaces and views — all from an LLM like Claude.

---

## Features

- **Create chart reports** — bar, line, pie, scatter, bubble, and all Zoho subtypes
- **Edit chart reports** — update axis, chart type, or title on an existing report
- **Query data** — run SQL SELECT queries against any workspace table
- **List workspaces** — enumerate all workspaces in your Zoho organisation
- **Search views** — find tables, charts, and dashboards by name or type
- **Get view details** — inspect column names, data types, and axis config
- **Auto token refresh** — OAuth2 refresh-token flow with one-retry on expiry
- **Interactive chart rendering** — charts are rendered via a custom HTML viewer embedded in the MCP app

---

## Project Structure

```
MCP_server_with_edit_file/
├── server.py               # Entry point — registers and runs the MCP server
├── column.py               # Standalone debug script to inspect columns in a view
├── pyproject.toml          # Project metadata and dependencies
├── requirements.txt        # Pip-installable dependencies
├── .env                    # Credentials (not committed — see setup below)
└── src/
    ├── tools.py            # All 6 MCP tool definitions (create, edit, query, etc.)
    ├── auth.py             # OAuth2 TokenManager (refresh-token flow)
    ├── config.py           # Settings loaded from .env + singleton token manager
    ├── chart_viewer.html   # HTML app used to render charts inline in the MCP client
    ├── __init__.py
    └── utils/
        ├── zoho_client.py  # Async HTTP helpers for Zoho Analytics REST API v2
        └── __init__.py
```

---

## Prerequisites

- Python 3.11 or higher
- A Zoho Analytics account (India DC — `zoho.in`)
- A Zoho OAuth2 **Self Client** with the following scope:  
  `ZohoAnalytics.fullaccess.all`
- Your **Client ID**, **Client Secret**, and a **Refresh Token**

---

## Setup

### 1. Clone / copy the project

```bash
cd "C:/Users/ruban/OneDrive/Desktop/zoho analytics/MCP_server_with_edit_file"
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

pip install -r requirements.txt
```

### 3. Configure credentials

Create a `.env` file in the project root (next to `server.py`):

```env
ANALYTICS_CLIENT_ID=your_client_id
ANALYTICS_CLIENT_SECRET=your_client_secret
ANALYTICS_REFRESH_TOKEN=your_refresh_token
ANALYTICS_ORG_ID=your_org_id

# Optional — defaults to Zoho India DC
ANALYTICS_ACCOUNTS_URL=https://accounts.zoho.in
ANALYTICS_BASE_URL=https://analyticsapi.zoho.in
```

> **Note:** To get a refresh token, create a Self Client in the [Zoho API Console](https://api-console.zoho.in/), generate a grant code with the scope above, then exchange it for a refresh token.

---

## Running the Server

### Development mode (hot-reload + web UI on port 8080)

```bash
fastmcp dev server.py
```

### Production / stdio mode (for use with Claude Desktop or other MCP clients)

```bash
fastmcp run server.py
```

---

## Available Tools

### `create_chart_report`

Creates a new chart report in a Zoho Analytics workspace and renders it interactively.

| Parameter | Type | Description |
|---|---|---|
| `workspace_id` | `str` | Target workspace ID |
| `table_name` | `str` | Base table name (exact, case-sensitive) |
| `chart_name` | `str` | Name for the new report |
| `chart_details` | `dict` | `chartType`, `x_axis`, `y_axis` |
| `filters` | `list[dict]` _(optional)_ | Filter conditions |
| `org_id` | `str` _(optional)_ | Defaults to `ANALYTICS_ORG_ID` in `.env` |

**Example `chart_details`:**
```json
{
  "chartType": "bar",
  "x_axis": { "columnName": "Region",  "operation": "actual" },
  "y_axis": { "columnName": "Sales",   "operation": "sum"    }
}
```

---

### `edit_chart_report`

Updates an existing chart report (axis, chart type, title) using a PUT request.

| Parameter | Type | Description |
|---|---|---|
| `workspace_id` | `str` | Workspace that owns the chart |
| `view_id` | `str` | ID of the report to update |
| `chart_name` | `str` | New title for the report |
| `chart_details` | `dict` | Same structure as `create_chart_report` |
| `filters` | `list[dict]` _(optional)_ | Filter conditions |
| `org_id` | `str` _(optional)_ | Defaults to `ANALYTICS_ORG_ID` in `.env` |

---

### `query_data`

Executes a SQL SELECT query against a workspace table.

```sql
SELECT "Region", SUM("Sales") FROM "Untitled-1" GROUP BY "Region"
```

> Column and table names **must** be enclosed in double quotes.

---

### `get_workspace_list`

Returns all workspaces in the organisation with their IDs and names.

---

### `search_views`

Searches for views (tables, charts, dashboards) within a workspace.

| `view_type_ids` value | Meaning |
|---|---|
| `0` | Table |
| `2` | Chart |
| `6` | Query table |

---

### `get_view_details`

Returns full metadata for a view: column names, data types, report type, chart type, axis config, etc. Call this before `edit_chart_report` to inspect the current configuration.

---

## Debugging Column Names

Zoho column names are **case-sensitive**. Use `column.py` to list exact column names for any view:

```bash
.venv\Scripts\python column.py
```

This fetches a fresh token and prints a table of all columns with their exact names and data types. Copy-paste from this output into `chart_details`.

**Known columns in `Untitled-1`:**

| Column Name | Data Type |
|---|---|
| `Date` | Date |
| `Region` | String |
| `Product Category` | String |
| `Product` | String |
| `Customer Name` | String |
| `Sales` | Numeric |
| `Cost` | Numeric |

---

## Valid Chart Types

Some of the supported `chartType` values:

`bar`, `horizontal bar`, `stacked bar`, `line`, `smooth line`, `line with points`, `area`, `stacked area`, `pie`, `ring`, `semi pie`, `scatter`, `bubble`, `packed bubble`, `funnel`, `pyramid`, `heat map`, `combo`, `web`, `map area`, `map bubble`, `map filled`, `geo heat map`, `table chart`

---

## Valid Axis Operations

| Category | Operations |
|---|---|
| **String** | `actual`, `count`, `distinctCount` |
| **Numeric** | `sum`, `average`, `min`, `max`, `count`, `distinctCount`, `stdDev`, `median`, `variance` |
| **Date** | `year`, `monthYear`, `quarterYear`, `weekYear`, `fullDate`, `dateTime`, `quarter`, `month`, `week`, `weekDay`, `day`, `hour` |

---

## Authentication Flow

1. `config.py` loads credentials from `.env` and creates a singleton `TokenManager`.
2. Before every API call, `token_manager.ensure()` fetches and caches an access token (if not already present).
3. If Zoho returns error code `8535` (token expired) or HTTP `401`, the client calls `token_manager.refresh()` and retries the request once automatically.
4. All token refresh calls are protected by an `asyncio.Lock` so concurrent tool calls share a single refresh.

---

## Common Errors

| Error Code | Meaning | Fix |
|---|---|---|
| `8050` | Unknown column name | Use `column.py` or `get_view_details` to get exact column names |
| `7111` | Report name already exists | Use a different `chart_name` or call `search_views` to find the existing report |
| `7103` | Workspace not found | Check `workspace_id` — get it from the Zoho Analytics URL |
| `8535` | Access token expired | Handled automatically; if it persists, check your refresh token |
| `8525` | URL rule not configured | The API endpoint doesn't exist for your account/DC — check `ANALYTICS_BASE_URL` |

---

## Tech Stack

| Library | Role |
|---|---|
| [FastMCP](https://github.com/jlowin/fastmcp) | MCP server framework |
| [httpx](https://www.python-httpx.org/) | Async HTTP client for Zoho API calls |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | `.env` credential loading |
