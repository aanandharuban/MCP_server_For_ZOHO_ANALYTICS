"""
Centralised configuration for the Zoho Analytics MCP server.

Reads credentials and settings from environment variables (or a .env file
sitting next to this file's parent package root).

Auth strategy: refresh-token based.
On every request we call common_headers() which lazily fetches (and caches)
an access token.  If Zoho returns error-code 8535 (token expired) the caller
catches it and calls token_manager.refresh() before retrying.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from src.auth import TokenManager

# ── locate .env relative to this file so it works regardless of CWD ──────────
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)


class Settings:
    # OAuth credentials (long-lived; stored in .env)
    CLIENT_ID: str     = os.environ.get("ANALYTICS_CLIENT_ID", "")
    CLIENT_SECRET: str = os.environ.get("ANALYTICS_CLIENT_SECRET", "")
    REFRESH_TOKEN: str = os.environ.get("ANALYTICS_REFRESH_TOKEN", "")

    # Accounts server (where token refresh calls go)
    ACCOUNTS_URL: str  = os.environ.get(
        "ANALYTICS_ACCOUNTS_URL", "https://accounts.zoho.in"
    )

    # Analytics API base URL  (India DC default)
    BASE_URL: str = os.environ.get(
        "ANALYTICS_BASE_URL", "https://analyticsapi.zoho.in"
    )

    # Organisation ID
    ORG_ID: str = os.environ.get("ANALYTICS_ORG_ID", "")


# ── Singleton token manager ───────────────────────────────────────────────────
token_manager = TokenManager(
    client_id=Settings.CLIENT_ID,
    client_secret=Settings.CLIENT_SECRET,
    refresh_token=Settings.REFRESH_TOKEN,
    accounts_url=Settings.ACCOUNTS_URL,
)


def common_headers() -> dict[str, str]:
    """
    Return the two headers required by every Zoho Analytics API call.
    The access token is fetched (and cached) lazily; it is refreshed
    automatically when it expires.
    """
    return {
        "Authorization": f"Zoho-oauthtoken {token_manager.get_access_token()}",
        "ZANALYTICS-ORGID": Settings.ORG_ID,
    }
