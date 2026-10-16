"""Human approval for confirmed changes.

``confirm=true`` is set by the model, so it only proves the model meant it. When the MCP client
supports elicitation, the user is additionally asked directly (yes/no) before anything changes.

MHPHOTOS_CONFIRMATION:
  ask      (default) ask the user when the client can; otherwise rely on confirm=true
  require  ask the user; refuse when the client cannot ask
  flag     never ask, confirm=true is enough
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import Context
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, Field

MODE_ENV = "MHPHOTOS_CONFIRMATION"


class Approval(BaseModel):
    approve: bool = Field(default=False, description="Ja, jetzt ausführen")


def mode() -> str:
    value = os.environ.get(MODE_ENV, "ask").strip().lower()
    return value if value in {"ask", "require", "flag"} else "ask"


async def approve(ctx: Context | None, message: str) -> tuple[bool, str]:
    """Return (approved, how). how: user | flag | declined | unsupported | failed."""
    chosen = mode()
    if chosen == "flag" or ctx is None:  # ctx is None for direct calls (tests, scripts)
        return True, "flag"
    try:
        result = await ctx.elicit(message=message, schema=Approval)
    except McpError as exc:
        if "not supported" in str(exc).lower():  # the client cannot ask
            return (False, "unsupported") if chosen == "require" else (True, "flag (client cannot ask)")
        return False, "failed"
    except Exception:  # noqa: BLE001 (any other failure must fail closed)
        return False, "failed"
    data = getattr(result, "data", None)
    if getattr(result, "action", "") == "accept" and getattr(data, "approve", False):
        return True, "user"
    return False, "declined"


def refusal(how: str) -> dict[str, Any]:
    if how == "declined":
        return {"declined": True, "message": "Du hast abgelehnt. Es wurde nichts geändert."}
    if how == "unsupported":
        return {
            "error": "Dieser Client kann dich nicht um Bestätigung bitten, und "
            f"{MODE_ENV}=require ist gesetzt. Nichts wurde geändert.",
            "code": "approval_unavailable",
        }
    return {"error": "Die Bestätigung ist fehlgeschlagen. Nichts wurde geändert.", "code": "approval_failed"}
