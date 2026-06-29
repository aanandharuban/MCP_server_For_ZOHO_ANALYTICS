"""
Zoho Analytics MCP Server — tools.py
=====================================
Registers all six tools:

  1. create_chart_report  (main renders chart via custom HTML App)
  2. edit_chart_report    (main renders updated chart via custom HTML App)
  3. query_data
  4. get_workspace_list
  5. search_views
  6. get_view_details
"""

from __future__ import annotations

import json
import os
import traceback
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.apps import AppConfig, ResourceCSP
from fastmcp.server.dependencies import get_context

from src.config import Settings
from src.utils.zoho_client import (
    zoho_export_png_data_url,
    zoho_get,
    zoho_post_config,
    zoho_put_config,
)

mcp = FastMCP("Zoho Analytics MCP Server")

_CHART_VIEWER_URI = "ui://zoho-analytics/chart-viewer.html"


@mcp.resource(
    _CHART_VIEWER_URI,
    app=AppConfig(
        csp=ResourceCSP(
            resource_domains=["https://unpkg.com", "https://cdn.jsdelivr.net", "data:"],
            connect_domains=["https://unpkg.com", "https://cdn.jsdelivr.net"],
        )
    ),
)
def _chart_viewer_resource() -> str:
    html_path = os.path.join(os.path.dirname(__file__), "chart_viewer.html")
    with open(html_path, "r", encoding="utf-8") as fh:
        return fh.read()


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build axis columns list
# ─────────────────────────────────────────────────────────────────────────────

# Zoho v2 API accepted chartType values (for validation / normalisation)
_VALID_CHART_TYPES: set[str] = {
    # Area
    "area",
    "area with points",
    "area without points",
    "smooth area",
    "smooth area with points",
    "smooth area without points",
    "stacked area",
    "stacked area with points",
    "stacked smooth area",
    "stacked smooth area with points",
    "stacked smooth area without points",
    # Bar
    "bar",
    "horizontal bar",
    "stacked bar",
    "horizontal stacked bar",
    # Bubble
    "bubble",
    "packed bubble",
    # Combo
    "combo",
    "combo bar with smooth line",
    # Funnel / Pyramid
    "funnel",
    "pyramid",
    # Line
    "line",
    "line with points",
    "line without points",
    "smooth line",
    "smooth line with points",
    "smooth line without points",
    "step",
    "map area",
    "map bubble",
    "map filled",
    "map pie",
    "map pie bubble",
    "map bubble pie",
    "map scatter",
    "geo heat map",
    "pie",
    "ring",
    "semi pie",
    "semi ring",
    "scatter",
    "web",
    "web with fill",
    "web without fill",
    "heat map",
    "butterfly",
    "table chart",
}

_VALID_AXIS_OPERATIONS: set[str] = {
    "actual",
    "measure",
    "dimension",
    "range",
    "sum",
    "average",
    "min",
    "max",
    "count",
    "distinctCount",
    "stdDev",
    "median",
    "mode",
    "variance",
    "year",
    "quarterYear",
    "monthYear",
    "weekYear",
    "fullDate",
    "dateTime",
    "quarter",
    "month",
    "week",
    "weekDay",
    "day",
    "hour",
}


def _normalise_chart_type(raw: str) -> str:
    """Lower-case the input and return as-is (Zoho accepts lowercase values)."""
    return raw.strip().lower()


def _normalise_operation(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    compact = value.replace(" ", "").replace("_", "").lower()
    aliases = {
        "avg": "average",
        "average": "average",
        "distinctcount": "distinctCount",
        "countdistinct": "distinctCount",
        "stddev": "stdDev",
        "standarddeviation": "stdDev",
        "quarteryear": "quarterYear",
        "monthyear": "monthYear",
        "weekyear": "weekYear",
        "fulldate": "fullDate",
        "datetime": "dateTime",
        "weekday": "weekDay",
    }
    op = aliases.get(compact, value)
    return op if op in _VALID_AXIS_OPERATIONS else None


def _build_axis_columns(chart_details: dict) -> list[dict]:
    x_axis = chart_details.get("x_axis") or chart_details.get("xAxis")
    y_axis = chart_details.get("y_axis") or chart_details.get("yAxis")
    if not x_axis or not y_axis:
        raise ValueError("chart_details must include x_axis/y_axis or xAxis/yAxis.")

    x_col: dict[str, Any] = {
        "type": "xAxis",
        "columnName": x_axis["columnName"],
        "operation": _normalise_operation(x_axis.get("operation")) or "actual",
    }
    if "tableName" in x_axis:
        x_col["tableName"] = x_axis["tableName"]

    y_col: dict[str, Any] = {
        "type": "yAxis",
        "columnName": y_axis["columnName"],
        "operation": _normalise_operation(y_axis.get("operation")) or "sum",
    }
    if "tableName" in y_axis:
        y_col["tableName"] = y_axis["tableName"]

    return [x_col, y_col]


def _apply_optional_report_config(config: dict[str, Any], chart_details: dict) -> None:
    for key in ("description", "userFilters", "isAxisMerge", "mergeAxisInfo"):
        if key in chart_details and chart_details[key] is not None:
            config[key] = chart_details[key]


async def _chart_payload(
    workspace_id: str,
    view_id: str,
    report_name: str,
    chart_details: dict,
    *,
    existing: bool = False,
) -> dict:
    image_data_url = await zoho_export_png_data_url(workspace_id, view_id)
    payload = {
        "report_id": view_id,
        "report_name": report_name,
        "chart_type": _normalise_chart_type(chart_details.get("chartType", "bar")),
        "chart_details": chart_details,
        "image_data_url": image_data_url,
        "image_mime": "image/png",
    }
    if existing:
        payload["existing"] = True
        payload["message"] = f"Using existing report named '{report_name}'."
    return payload


def _zoho_error_code(exc: httpx.HTTPStatusError) -> int | None:
    try:
        return exc.response.json().get("data", {}).get("errorCode")
    except Exception:
        return None


async def _find_view_by_name(workspace_id: str, view_name: str) -> dict | None:
    cfg = {"searchTerm": view_name}
    path = f"/restapi/v2/workspaces/{workspace_id}/views"
    data = await zoho_get(path, params={"CONFIG": json.dumps(cfg)})
    views = data.get("data", {}).get("views", []) or data.get("data", [])
    for view in views:
        if view.get("viewName") == view_name:
            return view
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. CREATE CHART REPORT
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(app=AppConfig(resource_uri=_CHART_VIEWER_URI))
async def create_chart_report(
    workspace_id: str,
    table_name: str,
    chart_name: str,
    chart_details: dict,
    filters: list[dict] | None = None,
    org_id: str | None = None,
) -> str:
    """
    <use_case>
    Create a new chart report in Zoho Analytics and render it interactively.
    Supported chart types: bar, line, pie, scatter, bubble (and all Zoho subtypes).
    </use_case>

    <arguments>
    - workspace_id (str): Workspace to create the chart in.
    - table_name   (str): Base table name for the chart.
    - chart_name   (str): Name for the new chart report.
    - chart_details (dict):
        - chartType (str): e.g. "bar", "line", "pie", "scatter", "bubble"
        - x_axis (dict):  columnName, operation[, tableName]
        - y_axis (dict):  columnName, operation[, tableName]
    - filters (list[dict] | None): Optional list of filter dicts.
        Each filter: tableName, columnName, operation, filterType, values, exclude
    - org_id (str | None): Defaults to ANALYTICS_ORG_ID env var.
    </arguments>
    """
    ctx = get_context()
    if not org_id:
        org_id = Settings.ORG_ID

    try:
        chart_type = _normalise_chart_type(chart_details.get("chartType", "bar"))
        if chart_type not in _VALID_CHART_TYPES:
            return json.dumps(
                {
                    "error": f"Invalid chartType '{chart_type}'. Valid values: {sorted(_VALID_CHART_TYPES)}"
                }
            )

        # Warn early if x/y column names look wrong (common LLM mistake: lowercase)
        x_axis = chart_details.get("x_axis") or chart_details.get("xAxis") or {}
        y_axis = chart_details.get("y_axis") or chart_details.get("yAxis") or {}
        x_col = x_axis["columnName"]
        y_col = y_axis["columnName"]
        if x_col != x_col.strip() or y_col != y_col.strip():
            return json.dumps(
                {
                    "error": f"Column names must match Zoho exactly (case-sensitive). Got x='{x_col}', y='{y_col}'. Use get_view_details or search_views to find exact column names."
                }
            )

        config: dict[str, Any] = {
            "baseTableName": table_name,  # correct Zoho v2 API key
            "title": chart_name,
            "reportType": "chart",
            "chartType": chart_type,
            "axisColumns": _build_axis_columns(chart_details),
        }
        _apply_optional_report_config(config, chart_details)
        if filters:
            config["filters"] = filters

        path = f"/restapi/v2/workspaces/{workspace_id}/reports"
        try:
            data = await zoho_post_config(path, config)
        except httpx.HTTPStatusError as exc:
            if _zoho_error_code(exc) != 7111:
                raise

            existing_view = await _find_view_by_name(workspace_id, chart_name)
            if not existing_view:
                return json.dumps(
                    {
                        "error": (
                            f"A report named '{chart_name}' already exists, but "
                            "the existing view could not be found. Use a different "
                            "chart_name or call search_views."
                        )
                    }
                )

            report_id = str(existing_view.get("viewId", ""))
            return json.dumps(
                await _chart_payload(
                    workspace_id,
                    report_id,
                    chart_name,
                    chart_details,
                    existing=True,
                )
            )

        # SDK returns int viewId directly; REST API wraps it
        report_id: str = str(
            data.get("data", {}).get("viewId", "")
            or data.get("data", {}).get("views", [{}])[0].get("viewId", "")
        )

        if not report_id or report_id == "0":
            return json.dumps({"error": f"Created but no viewId in response: {data}"})

        return json.dumps(
            await _chart_payload(workspace_id, report_id, chart_name, chart_details)
        )

    except Exception as exc:
        await ctx.error(traceback.format_exc())
        msg = exc.message if hasattr(exc, "message") else str(exc)
        return json.dumps({"error": msg})


# ─────────────────────────────────────────────────────────────────────────────
# 2. EDIT CHART REPORT
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(app=AppConfig(resource_uri=_CHART_VIEWER_URI))
async def edit_chart_report(
    workspace_id: str,
    view_id: str,
    chart_name: str,
    chart_details: dict,
    filters: list[dict] | None = None,
    org_id: str | None = None,
) -> str:
    """
    <use_case>
    Update an existing chart report in Zoho Analytics and re-render it.
    Uses PUT /restapi/v2/workspaces/{workspace_id}/reports/{view_id}
    After updating, fetches fresh HTML export and renders it in the chart viewer.
    </use_case>

    <arguments>
    - workspace_id (str): Workspace that owns the chart.
    - view_id      (str): The ID of the chart report to update.
    - chart_name   (str): New title for the chart.
    - chart_details (dict):
        - chartType (str): e.g. "bar", "line", "pie", "scatter", "bubble"
        - x_axis (dict):  columnName, operation[, tableName]
        - y_axis (dict):  columnName, operation[, tableName]
    - filters (list[dict] | None): Optional list of filter dicts.
    - org_id (str | None): Defaults to ANALYTICS_ORG_ID env var.
    </arguments>
    """
    ctx = get_context()
    if not org_id:
        org_id = Settings.ORG_ID

    try:
        chart_type = _normalise_chart_type(chart_details.get("chartType", "bar"))
        if chart_type not in _VALID_CHART_TYPES:
            return json.dumps(
                {
                    "error": f"Invalid chartType '{chart_type}'. Valid values: {sorted(_VALID_CHART_TYPES)}"
                }
            )

        x_axis = chart_details.get("x_axis") or chart_details.get("xAxis") or {}
        y_axis = chart_details.get("y_axis") or chart_details.get("yAxis") or {}
        x_col = x_axis["columnName"]
        y_col = y_axis["columnName"]
        if x_col != x_col.strip() or y_col != y_col.strip():
            return json.dumps(
                {
                    "error": f"Column names must match Zoho exactly (case-sensitive). Got x='{x_col}', y='{y_col}'. Use get_view_details to find exact column names."
                }
            )

        config: dict[str, Any] = {
            "title": chart_name,
            "reportType": "chart",
            "chartType": chart_type,
            "axisColumns": _build_axis_columns(chart_details),
        }
        _apply_optional_report_config(config, chart_details)
        if filters:
            config["filters"] = filters

        path = f"/restapi/v2/workspaces/{workspace_id}/reports/{view_id}"
        try:
            await zoho_put_config(path, config)
        except httpx.HTTPStatusError as exc:
            code = _zoho_error_code(exc)
            if code not in {8050, 8542}:
                raise
            payload = await _chart_payload(
                workspace_id, view_id, chart_name, chart_details
            )
            payload["warning"] = (
                "Zoho rejected the edit config, so the existing chart data was rendered. "
                f"Zoho error: {exc}"
            )
            return json.dumps(payload)

        return json.dumps(
            await _chart_payload(workspace_id, view_id, chart_name, chart_details)
        )

    except Exception as exc:
        await ctx.error(traceback.format_exc())
        msg = exc.message if hasattr(exc, "message") else str(exc)
        return json.dumps({"error": msg})


# ─────────────────────────────────────────────────────────────────────────────
# 3. QUERY DATA
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def query_data(
    workspace_id: str,
    sql_query: str,
    org_id: str | None = None,
) -> str:
    """
    <use_case>
    Execute a SQL SELECT query against a Zoho Analytics workspace.
    Returns results as a JSON string.
    Column and table names must be enclosed in double quotes.
    </use_case>

    <arguments>
    - workspace_id (str): Target workspace ID.
    - sql_query    (str): MySQL-compatible SELECT query.
    - org_id (str | None): Defaults to ANALYTICS_ORG_ID env var.
    </arguments>
    """
    ctx = get_context()
    try:
        import urllib.parse as _up

        cfg_str = _up.quote_plus(json.dumps({"sqlQuery": sql_query}))
        path = f"/restapi/v2/workspaces/{workspace_id}/data"
        data = await zoho_get(
            path, params={"CONFIG": json.dumps({"sqlQuery": sql_query})}
        )
        return json.dumps(data, indent=2)
    except Exception as exc:
        await ctx.error(traceback.format_exc())
        msg = exc.message if hasattr(exc, "message") else str(exc)
        return f"Error executing query: {msg}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. GET WORKSPACE LIST
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def get_workspace_list(org_id: str | None = None) -> str:
    """
    <use_case>
    Return all workspaces in the organisation with their IDs and names.
    </use_case>

    <arguments>
    - org_id (str | None): Defaults to ANALYTICS_ORG_ID env var.
    </arguments>
    """
    ctx = get_context()
    if not org_id:
        org_id = Settings.ORG_ID
    try:
        path = f"/restapi/v2/orgs/{org_id}/workspaces"
        data = await zoho_get(path)
        workspaces = data.get("data", {}).get("ownedWorkspaces", []) or data.get(
            "data", []
        )
        result = [
            {
                "workspaceId": ws.get("workspaceId", ws.get("id", "")),
                "workspaceName": ws.get("workspaceName", ws.get("name", "")),
            }
            for ws in workspaces
        ]
        return json.dumps(result, indent=2)
    except Exception as exc:
        await ctx.error(traceback.format_exc())
        msg = exc.message if hasattr(exc, "message") else str(exc)
        return f"Error fetching workspaces: {msg}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. SEARCH VIEWS
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def search_views(
    workspace_id: str,
    search_term: str = "",
    view_type_ids: list[int] | None = None,
    org_id: str | None = None,
) -> str:
    """
    <use_case>
    Search for views (tables, charts, dashboards) within a workspace.
    view_type_ids: 0=table, 2=chart, 6=query table.
    </use_case>

    <arguments>
    - workspace_id  (str):        Workspace to search.
    - search_term   (str):        Optional keyword filter on name.
    - view_type_ids (list[int]):  Optional view type filter.
    - org_id        (str | None): Defaults to ANALYTICS_ORG_ID env var.
    </arguments>
    """
    ctx = get_context()
    try:
        cfg: dict[str, Any] = {}
        if search_term:
            cfg["searchTerm"] = search_term
        if view_type_ids:
            cfg["allowedViewTypesIds"] = view_type_ids

        path = f"/restapi/v2/workspaces/{workspace_id}/views"
        params = {"CONFIG": json.dumps(cfg)} if cfg else None
        data = await zoho_get(path, params=params)
        views = data.get("data", {}).get("views", []) or data.get("data", [])
        result = [
            {
                "viewId": v.get("viewId", ""),
                "viewName": v.get("viewName", ""),
                "viewType": v.get("viewType", ""),
            }
            for v in views
        ]
        return json.dumps(result, indent=2)
    except Exception as exc:
        await ctx.error(traceback.format_exc())
        msg = exc.message if hasattr(exc, "message") else str(exc)
        return f"Error searching views: {msg}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. GET VIEW DETAILS
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def get_view_details(
    workspace_id: str,
    view_id: str,
    org_id: str | None = None,
) -> str:
    """
    <use_case>
    Return full metadata for a view: column names, data types, report type,
    chart type, axis config, etc.
    Call this before edit_chart_report to inspect current configuration.
    </use_case>

    <arguments>
    - workspace_id (str): Workspace that owns the view.
    - view_id      (str): The view to inspect.
    - org_id (str | None): Defaults to ANALYTICS_ORG_ID env var.
    </arguments>
    """
    ctx = get_context()
    try:
        path = f"/restapi/v2/workspaces/{workspace_id}/views/{view_id}"
        data = await zoho_get(path)
        return json.dumps(data, indent=2)
    except Exception as exc:
        await ctx.error(traceback.format_exc())
        msg = exc.message if hasattr(exc, "message") else str(exc)
        return f"Error fetching view details: {msg}"
