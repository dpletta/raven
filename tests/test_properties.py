"""Hypothesis invariants for Unicode chunking and citation JSON."""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from raven_mcp.citations.zotero import (
    build_citation_instruction,
    build_citation_payload,
    chunk_utf16,
    parse_citation_instruction,
)

SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=500,
)
JSON_SCALAR: st.SearchStrategy[Any] = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    SAFE_TEXT,
)


def _extend_json(
    children: st.SearchStrategy[Any],
) -> st.SearchStrategy[Any]:
    return st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(SAFE_TEXT, children, max_size=5),
    )


JSON_VALUE: st.SearchStrategy[Any] = st.recursive(
    JSON_SCALAR,
    _extend_json,
    max_leaves=20,
)


@settings(max_examples=150, derandomize=True)
@given(value=SAFE_TEXT, limit=st.integers(min_value=2, max_value=64))
def test_chunk_utf16_is_lossless_and_respects_code_unit_limit(
    value: str,
    limit: int,
) -> None:
    chunks = chunk_utf16(value, limit)

    assert "".join(chunks) == value
    assert chunks
    assert all(len(chunk.encode("utf-16-le")) // 2 <= limit for chunk in chunks)


@settings(max_examples=100, derandomize=True)
@given(item_data=st.dictionaries(SAFE_TEXT, JSON_VALUE, max_size=8), extension=JSON_VALUE)
def test_safe_citation_payload_json_round_trips(
    item_data: dict[str, Any],
    extension: Any,
) -> None:
    payload = build_citation_payload(
        [
            {
                "item_key": "ABCD2345",
                "uri": "http://zotero.org/users/1/items/ABCD2345",
                "csl_json": item_data,
            }
        ],
        citation_id="property-test",
        formatted="(Unicode 😀)",
        existing={"extension": extension},
    )

    decoded = parse_citation_instruction(build_citation_instruction(payload))

    assert decoded == payload
    assert decoded is not None
    assert decoded["extension"] == extension
    assert decoded["citationItems"][0]["itemData"] == item_data
