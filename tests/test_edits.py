"""Chat edit operations — the boundary between a model and a client deliverable."""

from __future__ import annotations

from app.core.edits import EditTarget, apply_operations
from app.llm.prompts.chat import Operation


def target() -> EditTarget:
    return EditTarget(
        site_name="Example",
        site_summary="The platform teams use to ship software faster.",
        pages={
            "https://example.com/docs/quickstart": {
                "title": "Quick Start",
                "description": "Get running in five minutes.",
                "section": "Docs",
                "is_optional": False,
                "included": True,
            },
            "https://example.com/blog/post": {
                "title": "A Post",
                "description": "Some writing about things.",
                "section": "Blog",
                "is_optional": False,
                "included": True,
            },
        },
    )


def test_a_url_the_run_does_not_have_is_refused_not_created():
    """The rule that matters most: a model must not be able to invent a page."""
    edits = target()
    report = apply_operations(
        edits, [Operation(op="set_page_copy", url="https://example.com/invented", title="Nope")]
    )

    assert report.applied == []
    assert len(report.rejected) == 1
    assert "not a page in this run" in report.rejected[0]
    assert "https://example.com/invented" not in edits.pages


def test_a_mangled_url_is_refused_rather_than_fuzzy_matched():
    """Off by a trailing slash is still not a page we have."""
    edits = target()
    report = apply_operations(
        edits,
        [Operation(op="exclude_page", url="https://example.com/docs/quickstart/")],
    )
    assert report.applied == []
    assert edits.pages["https://example.com/docs/quickstart"]["included"] is True


def test_copy_rewrites_land_on_the_right_page():
    edits = target()
    report = apply_operations(
        edits,
        [
            Operation(
                op="set_page_copy",
                url="https://example.com/docs/quickstart",
                title="Quickstart Guide",
                description="Install, authenticate and make a first call.",
            )
        ],
    )

    page = edits.pages["https://example.com/docs/quickstart"]
    assert report.rejected == []
    assert page["title"] == "Quickstart Guide"
    assert page["description"].startswith("Install, authenticate")
    # The other page is untouched.
    assert edits.pages["https://example.com/blog/post"]["title"] == "A Post"


def test_an_empty_description_is_refused():
    """Every link line needs one; the spec validator would flag it anyway."""
    edits = target()
    edits.pages["https://example.com/blog/post"]["description"] = ""
    report = apply_operations(
        edits,
        [Operation(op="set_page_copy", url="https://example.com/blog/post", title="Renamed")],
    )
    assert any("empty description" in r for r in report.rejected)


def test_notes_cannot_smuggle_in_a_heading():
    """The spec allows any markdown except headings between blockquote and first H2."""
    edits = target()
    report = apply_operations(edits, [Operation(op="set_notes", text="## Not allowed\nbody")])
    assert edits.notes == ""
    assert any("cannot contain headings" in r for r in report.rejected)

    ok = apply_operations(edits, [Operation(op="set_notes", text="v3 is current; v2 is archived.")])
    assert ok.rejected == []
    assert edits.notes.startswith("v3 is current")


def test_renaming_a_section_moves_its_pages_with_it():
    edits = target()
    report = apply_operations(
        edits, [Operation(op="rename_section", section="Docs", text="Documentation")]
    )
    assert report.rejected == []
    assert edits.pages["https://example.com/docs/quickstart"]["section"] == "Documentation"
    assert edits.pages["https://example.com/blog/post"]["section"] == "Blog"


def test_renaming_a_section_that_does_not_exist_is_refused():
    edits = target()
    report = apply_operations(
        edits, [Operation(op="rename_section", section="Nope", text="Something")]
    )
    assert any("no section named" in r for r in report.rejected)


def test_reorder_keeps_sections_the_model_forgot():
    """A section left out of the list must not vanish from the file."""
    edits = target()
    report = apply_operations(edits, [Operation(op="reorder_sections", sections=["Blog"])])
    assert report.rejected == []
    assert edits.section_order == ["Blog", "Docs"]


def test_reorder_with_an_unknown_section_is_refused_whole():
    edits = target()
    report = apply_operations(
        edits, [Operation(op="reorder_sections", sections=["Blog", "Imaginary"])]
    )
    assert edits.section_order == []
    assert any("unknown section" in r for r in report.rejected)


def test_exclude_and_include_round_trip():
    edits = target()
    apply_operations(edits, [Operation(op="exclude_page", url="https://example.com/blog/post")])
    assert edits.pages["https://example.com/blog/post"]["included"] is False

    apply_operations(edits, [Operation(op="include_page", url="https://example.com/blog/post")])
    assert edits.pages["https://example.com/blog/post"]["included"] is True


def test_moving_a_page_takes_it_out_of_optional():
    edits = target()
    edits.pages["https://example.com/blog/post"]["is_optional"] = True
    apply_operations(
        edits, [Operation(op="move_page", url="https://example.com/blog/post", section="Docs")]
    )
    page = edits.pages["https://example.com/blog/post"]
    assert page["section"] == "Docs"
    assert page["is_optional"] is False


def test_a_bad_operation_does_not_stop_the_good_ones():
    """Four sensible edits and one impossible one should do the four and say so."""
    edits = target()
    report = apply_operations(
        edits,
        [
            Operation(op="set_site_name", text="Example Inc"),
            Operation(op="set_page_copy", url="https://nope.example/x", title="No"),
            Operation(op="set_optional", url="https://example.com/blog/post", flag=True),
        ],
    )

    assert edits.site_name == "Example Inc"
    assert edits.pages["https://example.com/blog/post"]["is_optional"] is True
    assert len(report.applied) == 2
    assert len(report.rejected) == 1


def test_an_unsupported_operation_is_refused():
    edits = target()
    report = apply_operations(edits, [Operation(op="delete_everything")])
    assert report.applied == []
    assert any("unsupported" in r for r in report.rejected)
