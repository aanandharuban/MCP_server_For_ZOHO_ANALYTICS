"""
Standalone script — NO MCP stack needed.
Fetches a fresh access token and lists all columns in a Zoho view.

Run from your project folder:
  cd "C:/Users/ruban/OneDrive/Desktop/zoho analytics/MCP_server_with_edit_file"
  .venv/Scripts/python check_columns.py
"""

import asyncio
import json
import httpx

# ── credentials (copied from .env) ───────────────────────────────────────────
CLIENT_ID     = "1000.0DXC1Z6UTJBBO9YOFFNWTTBCUI3MFP"
CLIENT_SECRET = "651b90ea76f639deb27f0cbe70eeba982c34cc5065"
REFRESH_TOKEN = "1000.f1396184142015210dcb34445599b4b1.ad7d04bda3e37b123cbfb2989c8ceec2"
ACCOUNTS_URL  = "https://accounts.zoho.in"
BASE_URL      = "https://analyticsapi.zoho.in"
ORG_ID        = "60073680710"

WORKSPACE_ID  = "498732000000002017"
VIEW_ID       = "498732000000002005"


async def get_token(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{ACCOUNTS_URL}/oauth/v2/token",
        data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
            "grant_type":    "refresh_token",
        },
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in response: {resp.text}")
    return token


async def zoho_get(client: httpx.AsyncClient, token: str, path: str) -> dict:
    resp = await client.get(
        f"{BASE_URL}{path}",
        headers={
            "Authorization":  f"Zoho-oauthtoken {token}",
            "ZANALYTICS-ORGID": ORG_ID,
        },
    )
    if not resp.is_success:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
    return resp.json()


async def main():
    async with httpx.AsyncClient(timeout=30) as client:
        print("Getting access token...")
        token = await get_token(client)
        print(f"Token OK ({token[:20]}...)\n")

        # ── 1. view metadata ──────────────────────────────────────────────
        print(f"Fetching view metadata for view {VIEW_ID}...")
        try:
            view_resp = await zoho_get(
                client, token,
                f"/restapi/v2/workspaces/{WORKSPACE_ID}/views/{VIEW_ID}"
            )
            vdata = view_resp.get("data", {})
            view_name = vdata.get("viewName") or vdata.get("name") or "unknown"
            view_type = vdata.get("viewType") or vdata.get("type") or "unknown"
            print(f"  View name : {view_name}")
            print(f"  View type : {view_type}\n")
        except Exception as e:
            print(f"  Could not get view metadata: {e}\n")
            view_name = "unknown"

        # ── 2. columns ────────────────────────────────────────────────────
        print(f"Fetching columns for view {VIEW_ID}...")
        try:
            col_resp = await zoho_get(
                client, token,
                f"/restapi/v2/workspaces/{WORKSPACE_ID}/views/{VIEW_ID}/columns"
            )
        except Exception as e:
            print(f"  columns endpoint failed ({e}), trying view columns via workspace...")
            col_resp = await zoho_get(
                client, token,
                f"/restapi/v2/workspaces/{WORKSPACE_ID}/views"
            )

        # normalise — different API versions nest differently
        data = col_resp.get("data", {})
        columns = (
            data.get("columns")
            or data.get("column")
            or (data if isinstance(data, list) else [])
        )

        if not columns:
            print("\nNo columns found. Raw response:")
            print(json.dumps(col_resp, indent=2))
            return

        print(f"\n{'#':<4} {'Column Name (EXACT)':<40} {'Data Type':<15}")
        print("─" * 62)
        for i, col in enumerate(columns, 1):
            name  = col.get("columnName") or col.get("name", "?")
            dtype = col.get("dataType")   or col.get("type", "?")
            print(f"{i:<4} {name:<40} {dtype:<15}")

    print("""
─────────────────────────────────────────────
Valid operations per data type
  String  → actual | count | distinctCount
  Numeric → sum | min | max | average | count | distinctCount
  Date    → year | monthYear | quarterYear | fullDate | count

Use EXACT column names (copy-paste) in chart_details:
  {
    "chartType": "bar",
    "x_axis": {"columnName": "<EXACT NAME>", "operation": "actual"},
    "y_axis": {"columnName": "<EXACT NAME>", "operation": "count"}
  }
─────────────────────────────────────────────
""")


asyncio.run(main())