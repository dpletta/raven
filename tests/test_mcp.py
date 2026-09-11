"""End-to-end in-memory MCP surface tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from mcp import Client
from mcp.types import TextContent, TextResourceContents

from raven_mcp.config import Settings
from raven_mcp.server import create_server

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "document_inspect",
    "document_prepare_changes",
    "document_commit",
    "document_abort",
    "document_validate",
    "zotero_status",
    "zotero_search",
    "zotero_get_items",
    "citation_list",
    "citation_prepare_insert",
    "citation_prepare_update",
    "citation_prepare_remove",
    "bibliography_prepare_sync",
    "citation_scan_plain",
}


async def test_server_lists_tools_resources_and_prompts(
    raven_settings: Settings,
) -> None:
    server = create_server(raven_settings)

    async with Client(server, raise_exceptions=True) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        prompts = await client.list_prompts()

    assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
    inspect_tool = next(
        tool for tool in tools.tools if tool.name == "document_inspect"
    )
    assert inspect_tool.input_schema["required"] == ["document_path"]
    assert {str(resource.uri) for resource in resources.resources} == {
        "raven://capabilities"
    }
    assert {prompt.name for prompt in prompts.prompts} == {
        "review_section",
        "citation_audit",
    }


async def test_capabilities_resource_and_prompts_are_structured(
    raven_settings: Settings,
) -> None:
    server = create_server(raven_settings)

    async with Client(server, raise_exceptions=True) as client:
        resource = await client.read_resource("raven://capabilities")
        review = await client.get_prompt(
            "review_section",
            {
                "document_path": "/tmp/paper.docx",
                "heading": "Methods",
                "objective": "clarity",
            },
        )

    content = resource.contents[0]
    assert isinstance(content, TextResourceContents)
    capabilities = json.loads(content.text)
    assert capabilities["documents"]["supported"] == [".docx transitional OOXML"]
    assert capabilities["safety"]["two_phase_transactions"] is True
    prompt_content = review.messages[0].content
    assert isinstance(prompt_content, TextContent)
    assert "Methods" in prompt_content.text
    assert "document_prepare_changes" in prompt_content.text


async def test_inspect_and_validate_tools_return_structured_content(
    minimal_docx: Path,
    raven_settings: Settings,
) -> None:
    server = create_server(raven_settings)

    async with Client(server, raise_exceptions=True) as client:
        inspected = await client.call_tool(
            "document_inspect",
            {
                "document_path": str(minimal_docx),
                "limit": 1,
                "include_metadata": False,
            },
        )
        validated = await client.call_tool(
            "document_validate",
            {
                "document_path": str(minimal_docx),
                "profile": "full",
            },
        )

    inspect_data = cast(dict[str, Any], inspected.structured_content)
    validate_data = cast(dict[str, Any], validated.structured_content)
    assert inspect_data["summary"]["title"] == "Raven fixture"
    assert inspect_data["pagination"] == {
        "offset": 0,
        "limit": 1,
        "returned": 1,
        "total": 2,
        "next_offset": 1,
    }
    assert inspect_data["metadata"] is None
    assert validate_data["profile"] == "full"
    assert validate_data["valid"] is True
    assert validate_data["package"]["valid"] is True
