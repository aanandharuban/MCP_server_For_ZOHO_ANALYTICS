"""
Zoho Analytics MCP Server — tools.py
Registers all six tools:
create_chart_report  (main renders chart via custom HTML App)
edit_chart_report    (main renders updated chart via custom HTML App)
query_data
get_workspace_list
search_views
get_view_details
"""

from __future__ import annotations
import json
import os
import re
import traceback
from typing import Any
import httpx
from fastmcp import FastMCP
from fastmcp.apps import AppConfig, ResourceCSP
from fastmcp.server.dependencies import get_context
from src.config import Settings
from src.utils.zoho_client import (
    zoho_bulk_sql_query,
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
# Helper: Parse JSON safely (handles LLM passing strings instead of objects)
# ─────────────────────────────────────────────────────────────────────────────


def _safe_parse_json(val: Any) -> Any:
    """Safely parse JSON from string, handling common LLM formatting issues like backticks."""
    if isinstance(val, str):
        cleaned = val.replace("`", '"')
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            match = re.search(r"\[.*\]", cleaned, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            raise ValueError(f"Could not parse JSON string: {val}")
    return val


# ─────────────────────────────────────────────────────────────────────────────
# Constants & Normalization
# ─────────────────────────────────────────────────────────────────────────────

_VALID_CHART_TYPES: set[str] = {
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
    "bar",
    "horizontal bar",
    "stacked bar",
    "horizontal stacked bar",
    "bubble",
    "packed bubble",
    "combo",
    "combo bar with smooth line",
    "funnel",
    "pyramid",
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

# Valid reportType values per the Modeling API spec.
_VALID_REPORT_TYPES: set[str] = {"chart", "pivot", "summary"}

# Valid axis "type" values, keyed by reportType.
_VALID_AXIS_TYPES_BY_REPORT_TYPE: dict[str, set[str]] = {
    "chart": {"xAxis", "yAxis", "textAxis", "colorAxis", "toolTip"},
    "pivot": {"row", "column", "data"},
    "summary": {"groupBy", "summarize"},
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
    "seasonal",
    "relative",
}

# Full filterType set per the Modeling API spec (numeric + all date variants).
_VALID_FILTER_TYPES: set[str] = {
    "individualValues",
    "range",
    "ranking",
    "rankingPct",
    "dateRange",
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
    "count",
    "distinctCount",
}

_FILTER_TYPE_ALIASES: dict[str, str] = {
    "limit": "ranking",
    "top": "ranking",
    "bottom": "ranking",
    "topn": "ranking",
    "bottomn": "ranking",
    "percentage": "rankingPct",
    "percent": "rankingPct",
    "between": "range",
    "in": "individualValues",
    "equals": "individualValues",
    "eq": "individualValues",
    "date": "dateRange",
}

_FILTER_TYPE_CANONICAL: dict[str, str] = {
    **{ft.lower(): ft for ft in _VALID_FILTER_TYPES},
    **_FILTER_TYPE_ALIASES,
}


def _normalise_chart_type(raw: Any) -> str:
    value = str(raw).strip().lower().replace("_", " ")
    aliases = {
        "donut": "ring",
        "doughnut": "ring",
        "semi donut": "semi ring",
        "semi doughnut": "semi ring",
        "column": "bar",
        "columns": "bar",
        "vertical bar": "bar",
    }
    return aliases.get(value, value)


def _get_chart_type(
    details: dict[str, Any], explicit_chart_type: Any = None
) -> str | None:
    for value in (
        explicit_chart_type,
        details.get("chartType"),
        details.get("chart_type"),
        details.get("chart"),
    ):
        if value is not None and str(value).strip():
            return _normalise_chart_type(value)
    return None


def normalise_operation(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    compact = value.replace(" ", "").lower()
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
        "none": "actual",
        "null": "actual",
    }
    op = aliases.get(compact, value)
    return op if op in _VALID_AXIS_OPERATIONS else None


def _normalise_filter_type(raw: Any) -> str | None:
    if raw is None:
        return None
    compact = str(raw).strip().replace(" ", "").lower()
    return _FILTER_TYPE_CANONICAL.get(compact)


def _normalise_bool_str(raw: Any) -> str:
    """Zoho expects the literal strings 'true'/'false' for isAxisMerge, not JSON booleans."""
    if isinstance(raw, bool):
        return "true" if raw else "false"
    s = str(raw).strip().lower()
    return "true" if s in ("true", "1", "yes") else "false"


def _ensure_list(raw: Any, *, field_name: str) -> list[Any]:
    parsed = _safe_parse_json(raw)
    if parsed is None:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, tuple):
        return list(parsed)
    if isinstance(parsed, dict):
        return [parsed]
    raise ValueError(f"{field_name} must be a JSON array or object.")


def _name_key(raw: Any) -> str:
    """Match client-friendly names such as item sale to Zoho names like item_sale."""
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").lower())


def _best_name_match(raw: Any, candidates: list[str]) -> str:
    value = str(raw or "").strip()
    if not value:
        return value
    for candidate in candidates:
        if value == candidate:
            return candidate
    key = _name_key(value)
    if not key:
        return value
    for candidate in candidates:
        if _name_key(candidate) == key:
            return candidate
    return value


def _column_name_from_metadata(col: dict[str, Any]) -> str:
    for key in ("columnName", "name", "displayName", "columnDisplayName"):
        value = col.get(key)
        if value:
            return str(value)
    return ""


def _resolve_names_in_payload(
    value: Any, table_names: list[str], column_names: list[str]
) -> Any:
    if isinstance(value, list):
        return [
            _resolve_names_in_payload(item, table_names, column_names) for item in value
        ]
    if not isinstance(value, dict):
        return value

    resolved: dict[str, Any] = {}
    for key, item in value.items():
        if key in ("tableName", "baseTableName") and isinstance(item, str):
            resolved[key] = _best_name_match(item, table_names)
        elif key == "columnName" and isinstance(item, str):
            resolved[key] = _best_name_match(item, column_names)
        else:
            resolved[key] = _resolve_names_in_payload(item, table_names, column_names)
    return resolved


async def _workspace_views(workspace_id: str) -> list[dict[str, Any]]:
    data = await zoho_get(f"/restapi/v2/workspaces/{workspace_id}/views")
    return data.get("data", {}).get("views", []) or data.get("data", [])


async def _resolve_table_and_columns(
    workspace_id: str,
    table_name: str | None,
    chart_details: dict[str, Any],
    filters: Any = None,
) -> tuple[str | None, dict[str, Any], Any, list[str]]:
    """
    Resolve LLM-friendly table/column spellings to Zoho's exact names.
    Zoho is strict, but MCP clients often turn underscores into spaces.
    """
    warnings: list[str] = []
    if not table_name:
        return table_name, chart_details, filters, warnings

    try:
        views = await _workspace_views(workspace_id)
    except Exception as exc:
        warnings.append(f"Could not inspect workspace views for exact names: {exc}")
        return table_name, chart_details, filters, warnings

    view_names = [str(v.get("viewName", "")) for v in views if v.get("viewName")]
    resolved_table = _best_name_match(table_name, view_names)
    if resolved_table != table_name:
        warnings.append(
            f"Resolved table_name '{table_name}' to exact Zoho view '{resolved_table}'."
        )

    table_view = next((v for v in views if v.get("viewName") == resolved_table), None)
    column_names: list[str] = []
    if table_view and table_view.get("viewId"):
        try:
            metadata = await zoho_get(
                f"/restapi/v2/workspaces/{workspace_id}/views/{table_view['viewId']}/metadata"
            )
            columns = metadata.get("data", {}).get("columns", [])
            column_names = [
                name for col in columns if (name := _column_name_from_metadata(col))
            ]
        except Exception as exc:
            warnings.append(f"Could not inspect columns for '{resolved_table}': {exc}")

    resolved_details = _resolve_names_in_payload(
        chart_details, [resolved_table], column_names
    )
    resolved_filters = _resolve_names_in_payload(
        filters, [resolved_table], column_names
    )
    if column_names and resolved_details != chart_details:
        warnings.append(
            "Resolved chart_details column/table names to exact Zoho metadata names."
        )
    if column_names and resolved_filters != filters:
        warnings.append(
            "Resolved filter column/table names to exact Zoho metadata names."
        )
    return resolved_table, resolved_details, resolved_filters, warnings


async def _resolve_columns_for_view(
    workspace_id: str,
    view_id: str,
    chart_details: dict[str, Any],
    filters: Any = None,
) -> tuple[dict[str, Any], Any, list[str]]:
    warnings: list[str] = []
    try:
        metadata = await zoho_get(
            f"/restapi/v2/workspaces/{workspace_id}/views/{view_id}/metadata"
        )
        columns = metadata.get("data", {}).get("columns", [])
        column_names = [
            name for col in columns if (name := _column_name_from_metadata(col))
        ]
    except Exception as exc:
        warnings.append(f"Could not inspect view columns for exact names: {exc}")
        return chart_details, filters, warnings

    resolved_details = _resolve_names_in_payload(chart_details, [], column_names)
    resolved_filters = _resolve_names_in_payload(filters, [], column_names)
    if resolved_details != chart_details:
        warnings.append(
            "Resolved chart_details column names to exact Zoho metadata names."
        )
    if resolved_filters != filters:
        warnings.append("Resolved filter column names to exact Zoho metadata names.")
    return resolved_details, resolved_filters, warnings


def _axis_dict(details: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        value = details.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
    return None


def _coerce_rank_size(raw: Any) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw) if raw > 0 else None
    match = re.search(r"\d+", str(raw))
    return int(match.group(0)) if match else None


def _extract_rank_spec(details: dict[str, Any]) -> tuple[str, int] | None:
    for key in ("top", "topN", "top_n", "limit", "rankingLimit", "rankLimit"):
        if key in details:
            size = _coerce_rank_size(details[key])
            if size:
                return "top", size
    for key in ("bottom", "bottomN", "bottom_n"):
        if key in details:
            size = _coerce_rank_size(details[key])
            if size:
                return "bottom", size

    ranking = details.get("ranking") or details.get("rank")
    if isinstance(ranking, dict):
        direction = str(
            ranking.get("direction") or ranking.get("type") or "top"
        ).lower()
        size = _coerce_rank_size(
            ranking.get("limit")
            or ranking.get("count")
            or ranking.get("n")
            or ranking.get("value")
        )
        if size:
            return ("bottom" if "bottom" in direction else "top"), size
    return None


def _ranking_filters_from_details(
    details: dict[str, Any],
    table_name: str | None = None,
) -> list[dict[str, Any]]:
    rank = _extract_rank_spec(details)
    if not rank:
        return []

    y_axis = _axis_dict(details, "y_axis", "yAxis", "yAxisColumn")
    y_axis_raw = (
        details.get("y_axis") or details.get("yAxis") or details.get("yAxisColumn")
    )
    if y_axis:
        measure_col = y_axis.get("columnName")
        measure_op = normalise_operation(y_axis.get("operation")) or "sum"
        axis_table = y_axis.get("tableName")
    elif isinstance(y_axis_raw, str):
        measure_col = y_axis_raw.strip()
        measure_op = "sum"
        axis_table = None
    else:
        return []

    if not measure_col:
        return []

    direction, size = rank
    entry: dict[str, Any] = {
        "columnName": measure_col,
        "operation": measure_op,
        "filterType": "ranking",
        "values": [f"{'Bottom' if direction == 'bottom' else 'Top'} {size}"],
        "exclude": "false",
    }
    if axis_table or table_name:
        entry["tableName"] = axis_table or table_name
    return [entry]


def _normalise_sql_query(sql_query: str) -> str:
    query = str(sql_query or "").strip()
    if not re.match(r"(?is)^select\b", query):
        raise ValueError(
            "sql_query must be a complete SELECT statement. Example: "
            'SELECT "item_sale", SUM("Sales") FROM "SalesTable" GROUP BY "item_sale" LIMIT 10'
        )

    match = re.match(r"(?is)^select\s+top\s+(\d+)\s+(.*)$", query)
    if match and not re.search(r"(?is)\blimit\s+\d+\b", query):
        query = f"SELECT {match.group(2).rstrip(';')} LIMIT {match.group(1)}"
    return query


# ─────────────────────────────────────────────────────────────────────────────
# Axis & Filter Builders
# ─────────────────────────────────────────────────────────────────────────────


def _parse_axis_input(axis_data: Any, default_type: str, default_op: str) -> list[dict]:
    """Converts various LLM inputs (str, dict, list) into a list of axis column dicts."""
    if axis_data is None:
        return []
    if isinstance(axis_data, str):
        return [
            {
                "type": default_type,
                "columnName": axis_data.strip(),
                "operation": default_op,
            }
        ]
    if isinstance(axis_data, dict):
        axis_data = [axis_data]
    if not isinstance(axis_data, list):
        return []

    cols = []
    for ad in axis_data:
        if isinstance(ad, str):
            ad = {"columnName": ad}
        if not isinstance(ad, dict):
            continue
        c = {
            "type": ad.get("type", default_type),
            "columnName": ad.get("columnName", "").strip(),
            "operation": normalise_operation(ad.get("operation")) or default_op,
        }
        if "tableName" in ad:
            c["tableName"] = ad["tableName"]
        cols.append(c)
    return cols


def _build_axis_columns(chart_details: dict, report_type: str = "chart") -> list[dict]:
    if "axisColumns" in chart_details and isinstance(
        chart_details["axisColumns"], list
    ):
        default_type = {"chart": "xAxis", "pivot": "row", "summary": "groupBy"}.get(
            report_type, "xAxis"
        )
        return _parse_axis_input(chart_details["axisColumns"], default_type, "actual")

    x_axis = (
        chart_details.get("x_axis")
        or chart_details.get("xAxis")
        or chart_details.get("xAxisColumn")
    )
    y_axis = (
        chart_details.get("y_axis")
        or chart_details.get("yAxis")
        or chart_details.get("yAxisColumn")
    )

    if not x_axis or not y_axis:
        raise ValueError(
            "chart_details must include x_axis/y_axis (or xAxis/yAxis). They can be strings (columnName) or dicts."
        )

    cols = []
    cols.extend(_parse_axis_input(x_axis, "xAxis", "actual"))
    cols.extend(_parse_axis_input(y_axis, "yAxis", "sum"))

    for axis_type in (
        "textAxis",
        "colorAxis",
        "toolTip",
        "row",
        "column",
        "data",
        "groupBy",
        "summarize",
    ):
        axis_data = chart_details.get(axis_type)
        if axis_data:
            cols.extend(_parse_axis_input(axis_data, axis_type, "actual"))

    return cols


def _validate_axis_columns(axis_columns: list[dict], report_type: str) -> None:
    """Ensure each axis column's `type` is legal for the given reportType."""
    allowed = _VALID_AXIS_TYPES_BY_REPORT_TYPE.get(report_type)
    if not allowed:
        return
    for col in axis_columns:
        t = col.get("type")
        if t not in allowed:
            raise ValueError(
                f"Invalid axis type '{t}' for reportType '{report_type}'. "
                f"Valid values for '{report_type}': {sorted(allowed)}"
            )


def _validate_filters(filters: list[dict]) -> None:
    filter_type_aliases = {
        "limit": "ranking",
        "top": "ranking",
        "bottom": "ranking",
        "topn": "ranking",
        "bottomn": "ranking",
        "percentage": "rankingPct",
        "percent": "rankingPct",
        "between": "range",
        "in": "individualValues",
        "equals": "individualValues",
        "eq": "individualValues",
        "date": "dateRange",
    }
    for f in filters:
        ft = f.get("filterType")
        ft_lower = str(ft).strip().lower() if ft else ""
        if ft_lower in filter_type_aliases:
            f["filterType"] = filter_type_aliases[ft_lower]
            ft = f["filterType"]
            ft_lower = ft.lower()

        if ft and ft not in _VALID_FILTER_TYPES:
            raise ValueError(
                f"Invalid filterType '{ft}'. Valid values: {sorted(_VALID_FILTER_TYPES)}"
            )

        op = str(f.get("operation", "")).lower()
        is_bottom = "bottom" in op

        # Zoho expects 'actual' or 'count' on the dimension column being ranked
        if op in ("ranking", "top", "bottom", "limit", "none", "null", ""):
            f["operation"] = "actual"

        # Auto-format values for ranking filters (e.g., "10" -> "Top 10")
        if ft in ("ranking", "rankingPct"):
            values = f.get("values", [])
            if not isinstance(values, list):
                values = [values]
            formatted = []
            for v in values:
                v_str = str(v).strip().strip('"').strip("'")
                if v_str.isdigit() or v_str.replace(".", "", 1).isdigit():
                    prefix = "Bottom " if is_bottom or "bottom" in ft_lower else "Top "
                    formatted.append(
                        f"{prefix}{v_str}%"
                        if ft == "rankingPct"
                        else f"{prefix}{v_str}"
                    )
                else:
                    formatted.append(v_str)
            f["values"] = formatted

        # `exclude` is a required field per the spec — default to "false" (string,
        # matching Zoho's expected literal) if the caller didn't supply it.
        if "exclude" in f and f["exclude"] is not None:
            f["exclude"] = _normalise_bool_str(f["exclude"])
        else:
            f["exclude"] = "false"


def _build_filters(raw_filters: Any) -> list[dict]:
    result: list[dict[str, Any]] = []
    for raw in _ensure_list(raw_filters, field_name="filters"):
        if not isinstance(raw, dict):
            raise ValueError("Each filter must be a JSON object.")

        missing = [
            key
            for key in ("columnName", "operation", "filterType", "values")
            if raw.get(key) in (None, "")
        ]
        if missing:
            raise ValueError(
                f"Filter for column '{raw.get('columnName', '')}' is missing required field(s): {missing}"
            )

        filter_type = _normalise_filter_type(raw.get("filterType"))
        if not filter_type:
            raise ValueError(
                f"Invalid filterType '{raw.get('filterType')}'. Valid values: {sorted(_VALID_FILTER_TYPES)}"
            )

        op_text = str(raw.get("operation", "")).strip().lower()
        operation = normalise_operation(raw.get("operation"))
        if not operation and op_text in (
            "ranking",
            "top",
            "bottom",
            "limit",
            "none",
            "null",
            "",
        ):
            operation = "actual"
        if not operation:
            raise ValueError(
                f"Invalid filter operation '{raw.get('operation')}' for column '{raw.get('columnName')}'."
            )

        values = raw.get("values")
        if not isinstance(values, list):
            values = [values]

        is_bottom = (
            "bottom" in op_text
            or str(raw.get("direction", "")).strip().lower() == "bottom"
        )
        if filter_type in ("ranking", "rankingPct"):
            formatted = []
            for value in values:
                value_text = str(value).strip().strip('"').strip("'")
                numeric = (
                    value_text.isdigit() or value_text.replace(".", "", 1).isdigit()
                )
                if numeric:
                    prefix = "Bottom " if is_bottom else "Top "
                    formatted.append(
                        f"{prefix}{value_text}%"
                        if filter_type == "rankingPct"
                        else f"{prefix}{value_text}"
                    )
                else:
                    formatted.append(value_text)
            values = formatted

        entry: dict[str, Any] = {
            "columnName": str(raw["columnName"]).strip(),
            "operation": operation,
            "filterType": filter_type,
            "values": values,
            "exclude": _normalise_bool_str(raw.get("exclude", "false")),
        }
        if "tableName" in raw and raw["tableName"]:
            entry["tableName"] = raw["tableName"]
        result.append(entry)
    return result


def _normalize_ranking_filters(
    raw_filters: list[dict],
    chart_details: dict[str, Any],
    table_name: str,
) -> list[dict]:
    """
    Zoho rejects ranking filters on categorical columns with operation 'actual'.
    For Top/Bottom N, remap to the y-axis measure column when possible.
    """
    y_axis = chart_details.get("y_axis") or chart_details.get("yAxis")
    if isinstance(y_axis, str):
        measure_col = y_axis.strip()
        measure_op = "sum"
    elif isinstance(y_axis, dict):
        measure_col = y_axis.get("columnName")
        measure_op = normalise_operation(y_axis.get("operation")) or y_axis.get(
            "operation"
        )
    else:
        return raw_filters
    if not measure_col or not measure_op:
        return raw_filters

    normalized: list[dict] = []
    for raw in raw_filters:
        if not isinstance(raw, dict):
            normalized.append(raw)
            continue
        ft = _normalise_filter_type(raw.get("filterType"))
        op = str(raw.get("operation", "")).strip().lower()
        if ft == "ranking" and op in (
            "actual",
            "ranking",
            "top",
            "bottom",
            "limit",
            "",
        ):
            remapped = {
                **raw,
                "columnName": measure_col,
                "operation": measure_op,
                "tableName": raw.get("tableName") or table_name,
            }
            normalized.append(remapped)
        else:
            normalized.append(raw)
    return normalized


def _build_user_filters(raw_user_filters: list[Any]) -> list[dict]:
    """
    Build userFilters strictly per the User Filter Structure spec:
    only tableName, columnName, operation — no filterType/values/exclude.
    Anything carrying those extra keys belongs in `filters`, not `userFilters`,
    so callers should route those through _apply_optional_report_config instead.
    """
    result = []
    for uf in raw_user_filters:
        if not isinstance(uf, dict):
            continue
        entry: dict[str, Any] = {}
        if "tableName" in uf and uf["tableName"]:
            entry["tableName"] = uf["tableName"]
        if "columnName" in uf and uf["columnName"]:
            entry["columnName"] = str(uf["columnName"]).strip()
        op = normalise_operation(uf.get("operation")) if uf.get("operation") else None
        if not op:
            raise ValueError(
                f"Invalid or missing user filter operation for column '{uf.get('columnName', '')}'."
            )
        entry["operation"] = op
        if "columnName" in entry:
            result.append(entry)
    return result


def _apply_optional_report_config(config: dict[str, Any], chart_details: dict) -> None:
    if "filters" in chart_details and chart_details["filters"] is not None:
        config["filters"] = _build_filters(chart_details["filters"])

    if "userFilters" in chart_details and chart_details["userFilters"] is not None:
        raw_uf = _ensure_list(chart_details["userFilters"], field_name="userFilters")

        # Anything that carries filterType/values/exclude is actually a regular
        # filter that was misplaced under userFilters by the caller — route it
        # to `filters`. Everything else is a true user filter
        # (tableName/columnName/operation only, per spec).
        actual_filters = config.get("filters", [])
        true_user_filters_raw = []
        for uf in raw_uf:
            if isinstance(uf, dict) and any(
                k in uf for k in ("filterType", "values", "exclude")
            ):
                actual_filters.append(uf)
            else:
                true_user_filters_raw.append(uf)

        if actual_filters:
            config["filters"] = _build_filters(actual_filters)

        built_user_filters = _build_user_filters(true_user_filters_raw)
        if built_user_filters:
            config["userFilters"] = built_user_filters

    for key in ("description", "mergeAxisInfo"):
        if key in chart_details and chart_details[key] is not None:
            config[key] = chart_details[key]

    # isAxisMerge must be the literal string "true"/"false" per spec, and
    # mergeAxisInfo is only meaningful when isAxisMerge is true.
    if "isAxisMerge" in chart_details and chart_details["isAxisMerge"] is not None:
        config["isAxisMerge"] = _normalise_bool_str(chart_details["isAxisMerge"])

    if config.get("isAxisMerge") == "true" and "mergeAxisInfo" in config:
        mai = config["mergeAxisInfo"]
        if isinstance(mai, str):
            try:
                mai = json.loads(mai)
            except json.JSONDecodeError:
                raise ValueError(
                    f"mergeAxisInfo must be valid JSON like {{'axisIndex':[2,3],'labelName':'Merged'}}. Got: {mai}"
                )
        if (
            not isinstance(mai, dict)
            or "axisIndex" not in mai
            or "labelName" not in mai
        ):
            raise ValueError(
                "mergeAxisInfo must be a dict with 'axisIndex' (list of ints) and 'labelName' (str), e.g. {'axisIndex':[2,3],'labelName':'Merged'}"
            )
        if not isinstance(mai["axisIndex"], list) or not all(
            isinstance(i, int) for i in mai["axisIndex"]
        ):
            raise ValueError("mergeAxisInfo.axisIndex must be a list of integers.")
        config["mergeAxisInfo"] = (
            json.dumps(mai)
            if not isinstance(chart_details["mergeAxisInfo"], str)
            else chart_details["mergeAxisInfo"]
        )
    elif config.get("isAxisMerge") != "true":
        # mergeAxisInfo is only valid alongside isAxisMerge == "true"; drop it otherwise
        config.pop("mergeAxisInfo", None)


def _build_report_config(
    *,
    report_name: str | None,
    report_type: str,
    details: dict[str, Any],
    base_table_name: str | None = None,
    chart_type: Any = None,
    include_chart_type_default: bool = False,
    require_axis_columns: bool = True,
) -> dict[str, Any]:
    report_type = (report_type or details.get("reportType") or "chart").strip().lower()
    if report_type not in _VALID_REPORT_TYPES:
        raise ValueError(
            f"Invalid reportType '{report_type}'. Valid values: {sorted(_VALID_REPORT_TYPES)}"
        )

    if "CONFIG" in details:
        raw_config = _safe_parse_json(details["CONFIG"])
        if not isinstance(raw_config, dict):
            raise ValueError("CONFIG must be a JSON object.")
        details = {**raw_config, **{k: v for k, v in details.items() if k != "CONFIG"}}

    config: dict[str, Any] = {"reportType": report_type}
    if base_table_name is not None:
        config["baseTableName"] = base_table_name

    title = report_name or details.get("title") or details.get("reportName")
    if title:
        config["title"] = str(title).strip()
    if "title" in config and not config["title"]:
        raise ValueError("title cannot be empty.")

    if report_type == "chart":
        chart_type_value = _get_chart_type(details, chart_type)
        if chart_type_value is not None or include_chart_type_default:
            chart_type_value = chart_type_value or "bar"
            if chart_type_value not in _VALID_CHART_TYPES:
                raise ValueError(
                    f"Invalid chartType '{chart_type_value}'. Valid values: {sorted(_VALID_CHART_TYPES)}"
                )
            config["chartType"] = chart_type_value

    if "axisColumns" in details or any(
        k in details
        for k in (
            "x_axis",
            "xAxis",
            "xAxisColumn",
            "y_axis",
            "yAxis",
            "yAxisColumn",
            "row",
            "column",
            "data",
            "groupBy",
            "summarize",
        )
    ):
        axis_columns = _build_axis_columns(details, report_type)
        _validate_axis_columns(axis_columns, report_type)
        config["axisColumns"] = axis_columns
    elif require_axis_columns:
        raise ValueError(
            "axisColumns are required. Provide axisColumns or x_axis/y_axis style fields."
        )

    _apply_optional_report_config(config, details)
    return config


# ─────────────────────────────────────────────────────────────────────────────
# API Helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _chart_payload(
    workspace_id: str,
    view_id: str,
    report_name: str,
    chart_details: dict,
    *,
    existing: bool = False,
    warnings: list[str] | None = None,
) -> dict:
    image_data_url = await zoho_export_png_data_url(workspace_id, view_id)
    payload = {
        "report_id": view_id,
        "report_name": report_name,
        "chart_type": _get_chart_type(chart_details) or "bar",
        "chart_details": chart_details,
        "image_data_url": image_data_url,
        "image_mime": "image/png",
    }
    if existing:
        payload["existing"] = True
        payload["message"] = f"Using existing report named '{report_name}'."
    if warnings:
        payload["warnings"] = warnings
    return payload


def _zoho_error_code(exc: httpx.HTTPStatusError) -> int | None:
    try:
        return exc.response.json().get("data", {}).get("errorCode")
    except Exception:
        return None


async def _find_view_by_name(workspace_id: str, view_name: str) -> dict | None:
    views = await _workspace_views(workspace_id)
    for view in views:
        if view.get("viewName") == view_name:
            return view
    wanted = _name_key(view_name)
    for view in views:
        if _name_key(view.get("viewName", "")) == wanted:
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
    chart_details: str | dict,
    org_id: str | None = None,
) -> str:
    """
    <use_case>
    Create a new chart report in Zoho Analytics and render it interactively via the custom HTML App.
    Use this for all new chart creation requests, including specific data requests like "top 10" or "bottom 10" items.
    This tool resolves common MCP client name drift (e.g., 'item sale' -> 'item_sale') by validating against exact Zoho metadata when possible.
    </use_case>
    <arguments>
    - workspace_id (str):
      The ID of the workspace where the chart will be created.
      If unknown, use the `get_workspace_list` tool first to retrieve available workspace IDs.
      Example 1: "12345000000068001"
      Example 2: "12345000000092003"

    - table_name (str):
      The exact, case-sensitive name of the base table to query.
      Minor space/underscore drift is tolerated, but exact matches are highly recommended.
      Use `search_views` to find the precise table name if unsure.
      Example 1: "Sales_Data"
      Example 2: "Inventory_Items"

    - chart_name (str):
      The display title for the new chart report. Must be unique within the workspace.
      Example 1: "Top 10 Best Selling Items"
      Example 2: "Monthly Revenue Trend 2024"

    - chart_details (dict | str):
      A JSON object (or stringified JSON) defining the chart's axes, visual type, and data restrictions.
      Required fields:
        - x_axis (str | list): Column name(s) for the X-axis/categories.
        - y_axis (str | list): Column name(s) for the Y-axis/values.
      Optional fields:
        - chart_type (str): Visual style (e.g., 'bar', 'line', 'pie', 'ring', 'area', 'scatter'). Defaults to 'bar'.
        - filters (list[dict]): List of filter dictionaries to restrict data, apply Top/Bottom N rankings, or filter by specific values/dates.
          Required keys per filter object: columnName, operation, filterType, values.
      Example 1 (Simple with Top 10): {"x_axis": "Category", "y_axis": "Sales", "chart_type": "bar", "filters": [{"columnName": "Sales", "operation": "sum", "filterType": "ranking", "values": ["10"]}]}
      Example 2 (Multiple Y-axes with specific values): {"x_axis": "Month", "y_axis": ["Revenue", "Profit"], "chart_type": "line", "filters": [{"columnName": "Region", "operation": "actual", "filterType": "individualValues", "values": ["North", "South"]}]}

    - org_id (str | None):
      The organization ID. Defaults to the ANALYTICS_ORG_ID environment variable.
      Example 1: "60000000001"
      Example 2: "60000000002"
    </arguments>
    """
    ctx = get_context()
    if not org_id:
        org_id = Settings.ORG_ID

    try:
        chart_details = _safe_parse_json(chart_details)

        if table_name != table_name.strip():
            return json.dumps(
                {
                    "error": f"Table names are case-sensitive and cannot have trailing spaces. Got: '{table_name}'. Use search_views to find the exact table name."
                }
            )

        if not chart_name or not chart_name.strip():
            return json.dumps(
                {"error": "chart_name (title) is required and cannot be empty."}
            )

        config = _build_report_config(
            report_name=chart_name,
            report_type="chart",
            details=chart_details,
            base_table_name=table_name,
            chart_type=None,
            include_chart_type_default=True,
            require_axis_columns=True,
        )

        # FIX: Deduplicate filters to prevent Zoho from ignoring ranking limits
        if "filters" in config and isinstance(config["filters"], list):
            seen_filters = set()
            unique_filters = []
            for f in config["filters"]:
                filter_key = (f.get("columnName"), f.get("filterType"))
                if filter_key not in seen_filters:
                    seen_filters.add(filter_key)
                    unique_filters.append(f)
            config["filters"] = unique_filters

        if "chartType" in config:
            chart_details["chartType"] = config["chartType"]

        path = f"/restapi/v2/workspaces/{workspace_id}/reports"
        try:
            data = await zoho_post_config(path, config)
        except httpx.HTTPStatusError as exc:
            code = _zoho_error_code(exc)
            if code == 7111:
                existing_view = await _find_view_by_name(workspace_id, chart_name)
                if not existing_view:
                    return json.dumps(
                        {
                            "error": f"A report named '{chart_name}' already exists, but the existing view could not be found."
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

            if code == 8050:
                return json.dumps(
                    {
                        "error": f"Zoho rejected the chart configuration (Error 8050). This usually means a column name is invalid, or the operation doesn't match the column's data type. Zoho response: {exc.response.text}"
                    }
                )
            raise

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
        msg = getattr(exc, "message", str(exc))
        return json.dumps({"error": msg})


# ─────────────────────────────────────────────────────────────────────────────
# 2. EDIT CHART REPORT
# _____________________________________________________________________________


@mcp.tool(app=AppConfig(resource_uri=_CHART_VIEWER_URI))
async def edit_chart_report(
    workspace_id: str,
    view_id: str,
    chart_name: str,
    chart_details: str | dict,
    org_id: str | None = None,
) -> str:
    """
    <use_case>
    Update an existing chart report in Zoho Analytics and re-render it interactively via the custom HTML App.
    Whenever the user asks to change, modify, or update an existing chart, ALWAYS prefer using this tool over creating a new one.
    This tool resolves common MCP client column-name drift (e.g., 'item sale' -> 'item_sale') by validating against exact Zoho view metadata when possible.
    </use_case>
    <arguments>
    - workspace_id (str):
      The ID of the workspace that owns the existing chart.
      Example 1: "12345000000068001"
      Example 2: "12345000000092003"

    - view_id (str):
      The unique ID of the specific chart report to update. This is strictly required to identify the existing chart.
      Use `search_views` or `get_view_details` to find this ID if it is not already known.
      Example 1: "12345000000123005"
      Example 2: "12345000000456009"

    - chart_name (str):
      The new display title for the chart report. This will overwrite the existing title.
      Example 1: "Top 10 Best Selling Items (Updated)"
      Example 2: "Monthly Revenue Trend Q3"

    - chart_details (dict | str):
      A JSON object (or stringified JSON) defining the chart's updated axes, visual type, and data restrictions.
      Required fields (if changing axes):
        - x_axis (str | list): Column name(s) for the X-axis/categories.
        - y_axis (str | list): Column name(s) for the Y-axis/values.
      Optional fields:
        - chart_type (str): Visual style (e.g., 'bar', 'line', 'pie', 'ring', 'area').
        - filters (list[dict]): List of filter dictionaries to restrict data, apply Top/Bottom N rankings, or filter by specific values/dates.
          Required keys per filter object: columnName, operation, filterType, values.
      Example 1 (Change to Pie with Top 10): {"x_axis": "Category", "y_axis": "Sales", "chart_type": "pie", "filters": [{"columnName": "Sales", "operation": "sum", "filterType": "ranking", "values": ["10"]}]}
      Example 2 (Update Y-axis with specific values): {"x_axis": "Month", "y_axis": ["Revenue", "Profit"], "chart_type": "line", "filters": [{"columnName": "Region", "operation": "actual", "filterType": "individualValues", "values": ["North", "South"]}]}

    - org_id (str | None):
      The organization ID. Defaults to the ANALYTICS_ORG_ID environment variable.
      Example 1: "60000000001"
      Example 2: "60000000002"
    </arguments>
    """
    ctx = get_context()
    if not org_id:
        org_id = Settings.ORG_ID

    try:
        chart_details = _safe_parse_json(chart_details)
        if not isinstance(chart_details, dict):
            return json.dumps({"error": "chart_details must be a JSON object."})

        if not chart_name or not chart_name.strip():
            return json.dumps(
                {"error": "chart_name (title) is required and cannot be empty."}
            )

        chart_details, resolved_filters, warnings = await _resolve_columns_for_view(
        workspace_id, view_id, chart_details, chart_details.get("filters")
        )
        if resolved_filters is not None:
                chart_details["filters"] = resolved_filters

        config = _build_report_config(
            report_name=chart_name,
            report_type=chart_details.get("reportType", "chart"),
            details=chart_details,
            chart_type=chart_details.get("chart_type"),
            include_chart_type_default=False,
            require_axis_columns=False,
        )

        auto_rank_filters = _ranking_filters_from_details(chart_details)
        if auto_rank_filters:
            existing = config.get("filters", [])
            config["filters"] = existing + _build_filters(auto_rank_filters)
            warnings.append(
                f"Applied {auto_rank_filters[0]['values'][0]} ranking filter from chart_details."
            )

        # FIX: Deduplicate filters to prevent Zoho from ignoring ranking limits
        if "filters" in config and isinstance(config["filters"], list):
            seen_filters = set()
            unique_filters = []
            for f in config["filters"]:
                # Create a unique key based on column and filter type
                filter_key = (f.get("columnName"), f.get("filterType"))
                if filter_key not in seen_filters:
                    seen_filters.add(filter_key)
                    unique_filters.append(f)
            config["filters"] = unique_filters

        if "chartType" in config:
            chart_details["chartType"] = config["chartType"]

        path = f"/restapi/v2/workspaces/{workspace_id}/reports/{view_id}"
        try:
            await zoho_put_config(path, config)
        except httpx.HTTPStatusError as exc:
            code = _zoho_error_code(exc)
            if code == 8050:
                return json.dumps(
                    {
                        "error": f"Zoho rejected the edit configuration (Error 8050). A column name is likely misspelled or the operation is invalid. Zoho response: {exc.response.text}"
                    }
                )
            if code == 8542:
                return json.dumps(
                    {
                        "error": f"Malformed configuration JSON sent to Zoho (Error 8542). Zoho response: {exc.response.text}"
                    }
                )
            raise

        return json.dumps(
            await _chart_payload(
                workspace_id, view_id, chart_name, chart_details, warnings=warnings
            )
        )

    except Exception as exc:
        await ctx.error(traceback.format_exc())
        msg = getattr(exc, "message", str(exc))
        return json.dumps({"error": msg})


#
# 3. QUERY DATA
#
'''
@mcp.tool()
async def update_report(
    workspace_id: str,
    report_id: str,
    config: str | dict,
    filters: str | list | None = None,
    chart_type: str | None = None,
    org_id: str | None = None,
) -> str:
    """
    <use_case>
    Update an existing Zoho Analytics report using the v2 Modeling API CONFIG
    shape directly. Supports chart, pivot, and summary reports, including
    axisColumns, filters, userFilters, isAxisMerge, and mergeAxisInfo.

    When users ask for changes in existing charts, prefer using this over creating a fresh start, wherever possible.
    </use_case>
    <arguments>
    - workspace_id (str): Workspace that owns the report.
    - report_id    (str): Report/view ID to update.
    - config       (dict | str): CONFIG JSON for the Update Report API.
    - filters      (list[dict] | str | None): Optional extra regular filters.
    - chart_type   (str | None): Explicit chart type override, e.g. 'pie'.
    - org_id       (str | None): Defaults to ANALYTICS_ORG_ID env var.
    </arguments>
    """
    ctx = get_context()
    if not org_id:
        org_id = Settings.ORG_ID

    try:
        details = _safe_parse_json(config)
        if not isinstance(details, dict):
            return json.dumps({"error": "config must be a JSON object."})
        if filters is not None:
            filters = _safe_parse_json(filters)

        payload = _build_report_config(
            report_name=details.get("title") or details.get("reportName"),
            report_type=str(details.get("reportType") or "chart"),
            details=details,
            chart_type=chart_type,
            include_chart_type_default=False,
            require_axis_columns=False,
        )
        if filters:
            payload["filters"] = payload.get("filters", []) + _build_filters(filters)

        path = f"/restapi/v2/workspaces/{workspace_id}/reports/{report_id}"
        try:
            data = await zoho_put_config(path, payload)
        except httpx.HTTPStatusError as exc:
            code = _zoho_error_code(exc)
            if code == 8050:
                return json.dumps({"error": f"Zoho rejected the report configuration (Error 8050). A column name is likely misspelled or the operation is invalid. Zoho response: {exc.response.text}"})
            if code == 8542:
                return json.dumps({"error": f"Malformed configuration JSON sent to Zoho (Error 8542). Zoho response: {exc.response.text}"})
            raise

        return json.dumps({"status": "success", "zoho_response": data, "config_sent": payload}, indent=2)

    except Exception as exc:
        await ctx.error(traceback.format_exc())
        msg = getattr(exc, "message", str(exc))
        return json.dumps({"error": msg})
'''


@mcp.tool()
async def query_data(
    workspace_id: str, sql_query: str, org_id: str | None = None
) -> str:
    """
    <use_case>
    Execute a raw SQL SELECT query against a Zoho Analytics workspace to retrieve data rows or aggregates.
    Use this tool strictly for reading, exploring, or validating data (e.g., checking column names, fetching exact totals, or previewing top N rows).
    DO NOT use this tool to create, edit, or visualize charts; use `create_chart_report` or `edit_chart_report` for those tasks.
    The query must be a valid MySQL-compatible SELECT statement.
    </use_case>
    <arguments>
    - workspace_id (str):
      The ID of the workspace where the target tables reside.
      If unknown, use the `get_workspace_list` tool first to retrieve available workspace IDs.
      Example 1: "12345000000068001"
      Example 2: "12345000000092003"

    - sql_query (str):
      A valid, MySQL-compatible SELECT query. Do not include INSERT, UPDATE, or DELETE statements.
      You can use standard SQL clauses like WHERE, GROUP BY, ORDER BY, and LIMIT.
      For Top N requests, append `LIMIT N` to the end of your query.
      Example 1 (Basic Retrieval): "SELECT * FROM Sales_Data LIMIT 10"
      Example 2 (Aggregation): "SELECT Region, SUM(Revenue) AS Total_Revenue FROM Sales_Data GROUP BY Region ORDER BY Total_Revenue DESC LIMIT 5"

    - org_id (str | None):
      The organization ID. Defaults to the ANALYTICS_ORG_ID environment variable if not provided.
      Example 1: "60000000001"
      Example 2: "60000000002"
    </arguments>
    """
    ctx = get_context()
    try:
        sql_query = _normalise_sql_query(sql_query)
        data = await zoho_bulk_sql_query(workspace_id, sql_query)
        return json.dumps(data, indent=2)
    except Exception as exc:
        await ctx.error(traceback.format_exc())
        msg = getattr(exc, "message", str(exc))
        return f"Error executing query: {msg}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. GET WORKSPACE LIST
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
async def get_workspace_list(org_id: str | None = None) -> str:
    """
    <use_case>
    Retrieve a list of all accessible workspaces (both owned and shared) within an organization, including their unique IDs and names.
    Use this tool as a foundational step whenever the `workspace_id` is unknown, missing, or needs to be verified before calling tools like `create_chart_report`, `edit_chart_report`, or `query_data`.
    </use_case>
    <arguments>
    - org_id (str | None):
      The unique identifier for the Zoho Analytics organization.
      If provided, it returns workspaces specifically for that organization.
      If omitted (and the ANALYTICS_ORG_ID environment variable is not set), the tool automatically falls back to returning ALL accessible workspaces (owned and shared) across all organizations the authenticated user has access to.
      Example 1: "60000000001"
      Example 2: "60000000002"
    </arguments>
    """
    ctx = get_context()
    if not org_id:
        org_id = Settings.ORG_ID

    try:
        # /orgs/{id}/workspaces returns 8525 on India DC; use global list instead.
        path = "/restapi/v2/workspaces"
        data = await zoho_get(path)
        data_obj = data.get("data", {})
        owned = data_obj.get("ownedWorkspaces", [])
        shared = data_obj.get("sharedWorkspaces", [])
        workspaces = owned + shared
        if org_id:
            workspaces = [
                ws
                for ws in workspaces
                if not ws.get("orgId") or str(ws.get("orgId", "")) == str(org_id)
            ]

        result = [
            {
                "workspaceId": ws.get("workspaceId", ws.get("id", "")),
                "workspaceName": ws.get("workspaceName", ws.get("name", "")),
                "orgId": ws.get("orgId", ""),
            }
            for ws in workspaces
        ]
        return json.dumps(result, indent=2)
    except Exception as exc:
        await ctx.error(traceback.format_exc())
        msg = getattr(exc, "message", str(exc))
        return f"Error fetching workspaces: {msg}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. SEARCH VIEWS
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
async def search_views(
    workspace_id: str,
    search_term: str = "",
    view_type_ids: str | list | None = None,
    org_id: str | None = None,
) -> str:
    """
    <use_case>
    Search for views (tables, charts, dashboards, and reports) within a specific Zoho Analytics workspace.
    Use this tool to discover exact table names (to prevent MCP client name drift) or to find the `view_id` of an existing chart required for the `edit_chart_report` tool.
    </use_case>
    <arguments>
    - workspace_id (str):
      The ID of the workspace to search within.
      If unknown, use the `get_workspace_list` tool first to retrieve available workspace IDs.
      Example 1: "12345000000068001"
      Example 2: "12345000000092003"

    - search_term (str):
      An optional, case-insensitive keyword used to filter the returned views by their name.
      If omitted or left empty, the tool returns all views in the workspace.
      Example 1: "Sales" (Returns views like "Sales_Data", "Q3_Sales_Chart")
      Example 2: "Inventory" (Returns views like "Inventory_Items", "Stock_Levels")

    - view_type_ids (list[int] | str | None):
      An optional list of integer IDs to filter results by specific view types (e.g., restricting the search to only tables or only charts).
      If omitted, returns all view types.
      Example 1: [1] (Filters to return only Tables)
      Example 2: [2, 3] (Filters to return only Charts and Dashboards)

    - org_id (str | None):
      The organization ID. Defaults to the ANALYTICS_ORG_ID environment variable.
      Example 1: "60000000001"
      Example 2: "60000000002"
    </arguments>
    """
    ctx = get_context()
    try:
        if view_type_ids is not None:
            view_type_ids = _safe_parse_json(view_type_ids)

        cfg: dict[str, Any] = {}
        if view_type_ids:
            cfg["allowedViewTypesIds"] = view_type_ids

        path = f"/restapi/v2/workspaces/{workspace_id}/views"
        params = {"CONFIG": json.dumps(cfg)} if cfg else None
        data = await zoho_get(path, params=params)
        views = data.get("data", {}).get("views", []) or data.get("data", [])

        if search_term:
            search_text = search_term.lower()
            search_key = _name_key(search_term)
            views = [
                v
                for v in views
                if search_text in v.get("viewName", "").lower()
                or (search_key and search_key in _name_key(v.get("viewName", "")))
            ]

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
        msg = getattr(exc, "message", str(exc))
        return f"Error searching views: {msg}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. GET VIEW DETAILS
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
async def get_view_details(
    workspace_id: str, view_id: str, org_id: str | None = None
) -> str:
    """
    <use_case>
    Retrieve the complete metadata and configuration details for a specific view (table, chart, pivot, or dashboard) within a Zoho Analytics workspace.
    Use this tool to inspect the exact column names, data types, and current configuration of an existing chart before using the `edit_chart_report` tool. This ensures you do not introduce column-name drift or accidentally break existing filters and axes.
    </use_case>
    <arguments>
    - workspace_id (str):
      The ID of the workspace that owns the target view.
      If unknown, use the `get_workspace_list` tool first to retrieve available workspace IDs.
      Example 1: "12345000000068001"
      Example 2: "12345000000092003"

    - view_id (str):
      The unique ID of the specific view to inspect.
      Use the `search_views` tool to find this ID if it is not already known.
      Example 1: "12345000000123005"
      Example 2: "12345000000456009"

    - org_id (str | None):
      The organization ID. Defaults to the ANALYTICS_ORG_ID environment variable.
      Example 1: "60000000001"
      Example 2: "60000000002"
    </arguments>
    """
    ctx = get_context()
    try:
        view_data = await zoho_get(f"/restapi/v2/views/{view_id}")
        metadata = await zoho_get(
            f"/restapi/v2/workspaces/{workspace_id}/views/{view_id}/metadata"
        )
        result = {
            "status": "success",
            "view": view_data.get("data", {}).get("views", view_data.get("data", {})),
            "columns": metadata.get("data", {}).get("columns", []),
        }
        return json.dumps(result, indent=2)
    except Exception as exc:
        await ctx.error(traceback.format_exc())
        msg = getattr(exc, "message", str(exc))
        return f"Error fetching view details: {msg}"
