"""Shared MCP tool helpers."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from raven_mcp.errors import RavenError


def as_tool_error(error: RavenError) -> ToolError:
    """Serialize a stable Raven error without exposing internal details."""

    return ToolError(json.dumps(error.as_dict(), ensure_ascii=False))


def guarded[ResultT](call: Callable[[], ResultT]) -> ResultT:
    """Convert expected domain failures to model-visible MCP tool errors."""

    try:
        return call()
    except RavenError as error:
        raise as_tool_error(error) from error


async def guarded_async(call: Callable[[], Any]) -> Any:
    """Async counterpart to :func:`guarded`."""

    try:
        return await call()
    except RavenError as error:
        raise as_tool_error(error) from error
