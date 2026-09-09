# -- checks that reported agreement on a file that contradicts itself -------------


def test_a_comment_does_not_hide_a_site_wide_block():
    """`DIRECTIVE` captured to end of line, so `Disallow: /  # block everything`
    yielded the comment as part of the path and never equalled "/".

    CRW-001 then reported that the stated signal and the crawl rules agree, on a
    file where they contradict each other -- which its own docstring calls the
    one check nothing else in this tool can make.
    """
    from app.core.rules import audit_crawl

    robots = (
        "Content-Signal: ai-train=yes, search=yes\n\n"
        "User-agent: GPTBot\n"
        "Disallow: /  # block everything\n"
    )

    finding = audit_crawl(robots, site_url="https://x.example", fetched=True).by_id("CRW-001")

    assert finding.outcome.value == "fail", "a comment still hides the block"


def test_a_lowercase_user_agent_is_the_same_bot():
    """RFC 9309 matches the product token case-insensitively; every lookup used
    an exact-cased constant.

    It failed open on the rule that matters: no group found meant CRW-001
    reported agreement. `user-agent: gptbot` is common in hand-written files.
    """
    from app.core.rules import audit_crawl

    for spelling in ("GPTBot", "gptbot", "GPTBOT"):
        robots = f"Content-Signal: ai-train=yes\n\nUser-agent: {spelling}\nDisallow: /\n"
        finding = audit_crawl(robots, site_url="https://x.example", fetched=True).by_id("CRW-001")
        assert finding.outcome.value == "fail", spelling


def test_a_directive_value_still_parses_without_a_comment():
    """The narrower capture must not break the ordinary case."""
    from app.core.rules.crawl_rules import DIRECTIVE

    assert DIRECTIVE.search("Disallow: /private/").groups() == ("Disallow", "/private/")
    assert DIRECTIVE.search("Allow: /").groups() == ("Allow", "/")
    assert DIRECTIVE.search("Disallow:").groups() == ("Disallow", "")
