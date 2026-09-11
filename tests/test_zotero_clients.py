"""Offline Zotero adapter tests using httpx.MockTransport."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from raven_mcp.config import Settings
from raven_mcp.errors import ErrorCode, RavenError
from raven_mcp.zotero.client import ZoteroClient
from raven_mcp.zotero.local import (
    LocalZoteroClient,
    normalize_payload,
    resolve_get,
    resolve_search,
)
from raven_mcp.zotero.web import WebZoteroClient

KEY_A = "ABCD2345"
KEY_B = "WXYZ6789"


def _item(key: str, *, title: str = "A study") -> dict[str, object]:
    return {
        "key": key,
        "version": "7",
        "library": {"type": "user", "id": 42},
        "data": {"key": key, "title": title, "itemType": "journalArticle"},
        "csljson": {
            "id": key,
            "type": "article-journal",
            "title": title,
        },
    }


@pytest.mark.anyio
async def test_local_status_uses_version_headers(
    raven_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:23119/api/"
        assert request.headers["Zotero-API-Version"] == "3"
        assert "Zotero-API-Key" not in request.headers
        return httpx.Response(
            200,
            json={},
            headers={
                "Zotero-API-Version": "3",
                "Zotero-Schema-Version": "32",
                "Zotero-Server-ID": "local-test",
            },
        )

    async with LocalZoteroClient(
        raven_settings,
        transport=httpx.MockTransport(handler),
    ) as client:
        status = await client.status()

    assert status == {
        "available": True,
        "source": "local",
        "api_version": "3",
        "schema_version": "32",
        "server_id": "local-test",
    }


@pytest.mark.anyio
async def test_local_search_builds_collection_query_and_normalizes_items(
    raven_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/users/0/collections/{KEY_A}/items"
        assert request.url.params["q"] == "café"
        assert request.url.params["qmode"] == "everything"
        assert request.url.params["itemType"] == "journalArticle"
        assert request.url.params["tag"] == "reviewed"
        assert request.url.params["include"] == "data,csljson"
        return httpx.Response(200, json={"items": [_item(KEY_A)]})

    async with LocalZoteroClient(
        raven_settings,
        transport=httpx.MockTransport(handler),
    ) as client:
        items = await client.search(
            "café",
            collection_key=KEY_A,
            item_type="journalArticle",
            tag="reviewed",
            qmode="everything",
            limit=5,
        )

    assert len(items) == 1
    assert items[0].key == KEY_A
    assert items[0].version == 7
    assert items[0].source == "local"
    assert items[0].csl_json["title"] == "A study"
    assert items[0].uri == f"http://zotero.org/users/42/items/{KEY_A}"


@pytest.mark.anyio
async def test_local_get_deduplicates_keys_and_restores_requested_order(
    raven_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/users/0/items"
        assert request.url.params["itemKey"] == f"{KEY_B},{KEY_A}"
        return httpx.Response(200, json=[_item(KEY_A), _item(KEY_B)])

    async with LocalZoteroClient(
        raven_settings,
        transport=httpx.MockTransport(handler),
    ) as client:
        items = await client.get_items([KEY_B, KEY_A, KEY_B])

    assert [item.key for item in items] == [KEY_B, KEY_A]


@pytest.mark.anyio
async def test_web_client_keeps_secret_in_header_and_uses_user_and_group_paths(
    raven_settings: Settings,
) -> None:
    settings = replace(
        raven_settings,
        zotero_api_key="top-secret",
        zotero_user_id="42",
    )
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["Zotero-API-Key"] == "top-secret"
        assert "top-secret" not in str(request.url)
        item = _item(KEY_A)
        if request.url.path.startswith("/groups/"):
            item["library"] = {"type": "group", "id": 99}
        return httpx.Response(200, json=[item])

    async with WebZoteroClient(
        settings,
        transport=httpx.MockTransport(handler),
    ) as client:
        user_items = await client.search("query")
        group_items = await client.get_items(
            [KEY_A],
            library_type="group",
            library_id="99",
        )

    assert paths == ["/users/42/items", "/groups/99/items"]
    assert user_items[0].source == "web"
    assert group_items[0].uri == f"http://zotero.org/groups/99/items/{KEY_A}"


@pytest.mark.anyio
async def test_unified_client_is_local_first_without_speculative_web_requests(
    raven_settings: Settings,
) -> None:
    settings = replace(
        raven_settings,
        zotero_api_key="secret",
        zotero_user_id="42",
    )
    calls = {"local": 0, "web": 0}

    def local_handler(request: httpx.Request) -> httpx.Response:
        calls["local"] += 1
        return httpx.Response(200, json=[_item(KEY_A)], request=request)

    def web_handler(request: httpx.Request) -> httpx.Response:
        calls["web"] += 1
        return httpx.Response(200, json=[_item(KEY_B)], request=request)

    async with ZoteroClient(
        settings,
        local_transport=httpx.MockTransport(local_handler),
        web_transport=httpx.MockTransport(web_handler),
    ) as client:
        items = await client.search("local")

    assert [item.key for item in items] == [KEY_A]
    assert calls == {"local": 1, "web": 0}


@pytest.mark.anyio
@pytest.mark.parametrize("local_status", [403, 404, 405, 501])
async def test_unified_client_falls_back_for_supported_local_statuses(
    local_status: int,
    raven_settings: Settings,
) -> None:
    settings = replace(
        raven_settings,
        zotero_api_key="secret",
        zotero_user_id="42",
    )

    def local_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(local_status, request=request)

    def web_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_item(KEY_B)], request=request)

    async with ZoteroClient(
        settings,
        local_transport=httpx.MockTransport(local_handler),
        web_transport=httpx.MockTransport(web_handler),
    ) as client:
        items = await client.get_items([KEY_B])

    assert [item.source for item in items] == ["web"]


@pytest.mark.anyio
async def test_unified_client_falls_back_after_local_transport_error(
    raven_settings: Settings,
) -> None:
    settings = replace(
        raven_settings,
        zotero_api_key="secret",
        zotero_user_id="42",
    )

    def local_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Zotero is offline", request=request)

    def web_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={},
            headers={"Zotero-API-Version": "3"},
            request=request,
        )

    async with ZoteroClient(
        settings,
        local_transport=httpx.MockTransport(local_handler),
        web_transport=httpx.MockTransport(web_handler),
    ) as client:
        status = await client.status()

    assert status["source"] == "web"
    assert status["api_version"] == "3"


@pytest.mark.anyio
async def test_unified_client_does_not_fallback_for_bad_request(
    raven_settings: Settings,
) -> None:
    settings = replace(
        raven_settings,
        zotero_api_key="secret",
        zotero_user_id="42",
    )
    web_calls = 0

    def local_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, request=request)

    def web_handler(request: httpx.Request) -> httpx.Response:
        nonlocal web_calls
        web_calls += 1
        return httpx.Response(200, json=[], request=request)

    async with ZoteroClient(
        settings,
        local_transport=httpx.MockTransport(local_handler),
        web_transport=httpx.MockTransport(web_handler),
    ) as client:
        with pytest.raises(RavenError) as error:
            await client.search("bad")

    assert error.value.code == ErrorCode.INVALID_REQUEST
    assert error.value.locator == {
        "source": "local",
        "operation": "search",
        "status": 400,
    }
    assert web_calls == 0


@pytest.mark.anyio
async def test_web_failures_are_mapped_without_leaking_credentials(
    raven_settings: Settings,
) -> None:
    settings = replace(
        raven_settings,
        zotero_api_key="never-report-this",
        zotero_user_id="42",
    )

    def local_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    def web_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request)

    async with ZoteroClient(
        settings,
        local_transport=httpx.MockTransport(local_handler),
        web_transport=httpx.MockTransport(web_handler),
    ) as client:
        with pytest.raises(RavenError) as error:
            await client.search("rate limited")

    assert error.value.code == ErrorCode.ZOTERO_UNAVAILABLE
    assert error.value.retryable is True
    assert error.value.locator == {
        "source": "web",
        "operation": "search",
        "status": 429,
    }
    assert "never-report-this" not in str(error.value.as_dict())


@pytest.mark.anyio
async def test_web_client_requires_complete_numeric_credentials(
    raven_settings: Settings,
) -> None:
    async with WebZoteroClient(
        raven_settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    ) as disabled:
        assert disabled.enabled is False
        with pytest.raises(RavenError) as missing:
            await disabled.status()
    assert missing.value.code == ErrorCode.ZOTERO_UNAVAILABLE

    invalid = replace(
        raven_settings,
        zotero_api_key="secret",
        zotero_user_id="not-numeric",
    )
    async with WebZoteroClient(
        invalid,
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    ) as client:
        with pytest.raises(RavenError) as user_id:
            await client.status()
    assert user_id.value.code == ErrorCode.INVALID_REQUEST


@pytest.mark.anyio
async def test_invalid_json_and_unknown_payloads_are_incompatible(
    raven_settings: Settings,
) -> None:
    def invalid_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", request=request)

    async with LocalZoteroClient(
        raven_settings,
        transport=httpx.MockTransport(invalid_json),
    ) as client:
        with pytest.raises(RavenError) as error:
            await client.search()
    assert error.value.code == ErrorCode.ZOTERO_INCOMPATIBLE

    with pytest.raises(RavenError) as payload_error:
        normalize_payload(
            {"unexpected": "scalar"},
            source="local",
            library_type="user",
            library_id="0",
        )
    assert payload_error.value.code == ErrorCode.ZOTERO_INCOMPATIBLE


def test_request_resolution_validates_paths_keys_and_pagination() -> None:
    with pytest.raises(RavenError, match="group"):
        resolve_search(
            None,
            query="",
            library_type="group",
            library_id=None,
            collection=None,
            collection_key=None,
            item_type=None,
            tag=None,
            qmode=None,
            search_mode=None,
            sort="dateModified",
            direction="desc",
            start=0,
            limit=20,
        )

    with pytest.raises(RavenError, match="collection key"):
        resolve_search(
            None,
            query="",
            library_type="user",
            library_id=None,
            collection=None,
            collection_key="bad",
            item_type=None,
            tag=None,
            qmode=None,
            search_mode=None,
            sort="dateModified",
            direction="desc",
            start=0,
            limit=20,
        )

    with pytest.raises(RavenError, match="Invalid Zotero item key"):
        resolve_get(
            ["not-key"],
            item_keys=None,
            library_type="user",
            library_id=None,
        )


def test_normalization_accepts_keyed_and_string_csl_payloads() -> None:
    items = normalize_payload(
        {
            KEY_A: {
                "version": 3,
                "title": "Keyed record",
                "csljson": f'[{{"id":"{KEY_A}","title":"Keyed CSL"}}]',
            }
        },
        source="local",
        library_type="group",
        library_id="99",
    )

    assert items[0].key == KEY_A
    assert items[0].data["title"] == "Keyed record"
    assert items[0].csl_json["title"] == "Keyed CSL"
    assert items[0].uri == f"http://zotero.org/groups/99/items/{KEY_A}"
