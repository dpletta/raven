"""Generate deterministic, privacy-safe DOCX fixtures for Open XML SDK validation."""

from __future__ import annotations

import argparse
from pathlib import Path

from raven_mcp.citations.zotero import CitationManager
from raven_mcp.config import Settings
from raven_mcp.docx.opc import OpcPackage
from raven_mcp.docx.wordml import all_paragraphs, make_locator
from raven_mcp.schemas import CitationItemInput
from tests.conftest import DocxFactory, DocxSpec


def generate(output_directory: Path) -> list[Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    factory = DocxFactory(output_directory)

    paths = [
        factory("minimal.docx"),
        factory(
            "rich-structure.docx",
            DocxSpec(
                table=True,
                notes=True,
                comments_revisions=True,
                unicode_long_prefs=True,
                drawing=True,
            ),
        ),
        factory(
            "author-date-zotero.docx",
            DocxSpec(split_zotero=True, unicode_long_prefs=True),
        ),
        factory(
            "legacy-bibliography.docx",
            DocxSpec(legacy_bibliography=True),
        ),
    ]

    settings = Settings(allowed_roots=(output_directory.resolve(),))
    numeric_source = factory("numeric-source.docx")
    package = OpcPackage.open(numeric_source, settings)
    locator = make_locator(all_paragraphs(package)[-1])
    CitationManager(package).insert(
        locator,
        [
            CitationItemInput(
                item_key="ABCD2345",
                uri="http://zotero.org/users/1/items/ABCD2345",
                csl_json={
                    "id": "ABCD2345",
                    "type": "article-journal",
                    "title": "Synthetic first source",
                },
                locator="12",
                label="page",
            ),
            CitationItemInput(
                item_key="EFGH6789",
                uri="http://zotero.org/users/1/items/EFGH6789",
                csl_json={
                    "id": "EFGH6789",
                    "type": "book",
                    "title": "Synthetic second source",
                },
                prefix="see ",
            ),
        ],
        formatted="[1, 2]",
        style="http://www.zotero.org/styles/ieee",
        locale="en-US",
    )
    bibliography_locator = make_locator(all_paragraphs(package)[-1])
    CitationManager(package).sync_bibliography(
        bibliography_locator,
        heading="References",
        style="http://www.zotero.org/styles/ieee",
        locale="en-US",
    )
    numeric_output = output_directory / "numeric-raven-output.docx"
    package.write(numeric_output)
    numeric_source.unlink()
    paths.append(numeric_output)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_directory", type=Path)
    arguments = parser.parse_args()
    for path in generate(arguments.output_directory.resolve()):
        print(path)


if __name__ == "__main__":
    main()
