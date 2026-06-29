"""
Thin async HTTP helper for Zoho Analytics REST calls.

Encoding strategy for CONFIG:
  POST / PUT  →  CONFIG sent in the request body as application/x-www-form-urlencoded
                 (identical to curl's --data-urlencode). httpx handles percent-encoding,
                 so table names with spaces or + characters are sent correctly.
  GET         →  CONFIG passed via httpx `params=` which also percent-encodes correctly.

Auto-refresh behaviour
──────────────────────
Every request goes through _request().  If Zoho responds with error-code 8535
(token expired) or HTTP 401 the helper calls token_manager.refresh() once and
retries.
"""

from __future__ import annotations

import json
import base64

import httpx

_EXPIRED_ERROR_CODE = 8535


def _get_cfg():
    from src import config  # noqa: PLC0415
    return config


def _is_expired(resp: httpx.Response) -> bool:
    if resp.status_code == 401:
        return True
    try:
        return resp.json().get("data", {}).get("errorCode") == _EXPIRED_ERROR_CODE
    except Exception:
        return False


async def _request(
    method: str,
    url: str,
    *,
    config_dict: dict | None = None,    # CONFIG payload — sent as form body for POST/PUT, query param for GET
    params: dict | None = None,         # plain key-value query params (GET filters)
    timeout: int = 30,
) -> httpx.Response:
    """
    Fire an HTTP request, auto-refreshing the token on expiry (one retry).

    CONFIG encoding strategy (mirrors `curl --data-urlencode`):
      - GET  → CONFIG goes in the query string via httpx `params=`
      - POST/PUT → CONFIG goes in the request BODY as application/x-www-form-urlencoded
                   using httpx `data=` so httpx handles percent-encoding correctly.
                   This is identical to curl's --data-urlencode and avoids the
                   quote_plus '+'-for-space issue that breaks table names with spaces.
    """
    cfg = _get_cfg()
    await cfg.token_manager.ensure()

    headers = cfg.common_headers()
    data: dict | None = None
    get_params = params  # for plain GET params

    if config_dict is not None:
        config_json = json.dumps(config_dict, separators=(",", ":"))
        if method.upper() == "GET":
            # Merge CONFIG into query params dict so httpx encodes it cleanly
            get_params = dict(params or {})
            get_params["CONFIG"] = config_json
        else:
            # POST / PUT: send as form body — httpx uses proper percent-encoding
            data = {"CONFIG": config_json}
            headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(
            method, url, headers=headers, params=get_params, data=data
        )

    if _is_expired(resp):
        await cfg.token_manager.refresh()
        headers = cfg.common_headers()
        if data is not None:
            headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method, url, headers=headers, params=get_params, data=data
            )

    if not resp.is_success:
        # Log Zoho's actual error body before raising so we can diagnose 400s
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text
        raise httpx.HTTPStatusError(
            f"HTTP {resp.status_code} from Zoho — body: {err_body}",
            request=resp.request,
            response=resp,
        )
    return resp


# ── Public helpers ────────────────────────────────────────────────────────────

async def zoho_get(path: str, params: dict | None = None) -> dict:
    """GET {BASE_URL}{path} and return parsed JSON."""
    cfg = _get_cfg()
    url = f"{cfg.Settings.BASE_URL}{path}"
    resp = await _request("GET", url, params=params)
    return resp.json()


async def zoho_get_with_config(path: str, config: dict) -> httpx.Response:
    """GET with a CONFIG dict (e.g. export calls). Returns raw response."""
    cfg = _get_cfg()
    url = f"{cfg.Settings.BASE_URL}{path}"
    return await _request("GET", url, config_dict=config)


async def zoho_post_config(path: str, config: dict) -> dict:
    """
    POST {BASE_URL}{path} with CONFIG in the request body
    (application/x-www-form-urlencoded, identical to curl --data-urlencode).
    Returns parsed JSON.
    """
    cfg = _get_cfg()
    url = f"{cfg.Settings.BASE_URL}{path}"
    resp = await _request("POST", url, config_dict=config)
    return resp.json()


async def zoho_put_config(path: str, config: dict) -> dict | None:
    """
    PUT {BASE_URL}{path} with CONFIG in the request body.
    Zoho Update Report API returns 204 No Content on success.
    """
    cfg = _get_cfg()
    url = f"{cfg.Settings.BASE_URL}{path}"
    resp = await _request("PUT", url, config_dict=config, timeout=30)
    if resp.status_code == 204:
        return {"status": "success"}
    return resp.json()


async def zoho_export_html(workspace_id: str, view_id: str) -> str:
    """
    Export a chart view as HTML from Zoho Analytics.
    Returns the raw HTML string.
    """
    cfg = _get_cfg()
    export_cfg = {
        "responseFormat": "html",
    }
    path = f"/restapi/v2/workspaces/{workspace_id}/views/{view_id}/data"
    resp = await zoho_get_with_config(path, export_cfg)
    return resp.text


async def zoho_export_json(workspace_id: str, view_id: str) -> dict:
    """Export a view's data as JSON for local chart rendering."""
    export_cfg = {
        "responseFormat": "json",
    }
    path = f"/restapi/v2/workspaces/{workspace_id}/views/{view_id}/data"
    resp = await zoho_get_with_config(path, export_cfg)
    return resp.json()


async def zoho_export_png_data_url(workspace_id: str, view_id: str) -> str:
    """Export Zoho's own rendered view as PNG and return a browser data URL."""
    path = f"/restapi/v2/workspaces/{workspace_id}/views/{view_id}/data"
    last_error: Exception | None = None
    resp: httpx.Response | None = None
    for response_format in ("png", "image"):
        try:
            resp = await zoho_get_with_config(path, {"responseFormat": response_format})
            if resp.content.startswith(b"\x89PNG"):
                break
            preview = resp.text[:300] if resp.text else resp.headers.get("content-type", "")
            last_error = ValueError(
                f"Zoho {response_format} export did not return PNG bytes. Response preview: {preview}"
            )
        except Exception as exc:
            last_error = exc
    else:
        raise ValueError(f"Zoho PNG export failed. Last error: {last_error}") from last_error

    assert resp is not None
    encoded = base64.b64encode(resp.content).decode("ascii")
    return f"data:image/png;base64,{encoded}"
