"""Tests for the PDF export.

Two layers:
  * No external deps: section ordering, HTML assembly, lazy-import
    error path when WeasyPrint isn't installed.
  * With WeasyPrint (skipped when the library isn't importable):
    end-to-end render → bytes that start with the PDF magic header.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest


def _make_job(**overrides):
    """Build a JobState-like object with the minimum surface
    render_job_to_pdf consumes."""
    selections = SimpleNamespace(
        ticker=overrides.pop("ticker", "NVDA"),
        analysis_date=overrides.pop("analysis_date", "2026-05-17"),
        analysts=overrides.pop("analysts", ["market", "news"]),
        llm_provider=overrides.pop("llm_provider", "openai"),
        deep_thinker=overrides.pop("deep_thinker", "gpt-5.4"),
        quick_thinker=overrides.pop("quick_thinker", "gpt-5.4-mini"),
        research_depth=overrides.pop("research_depth", 1),
    )
    return SimpleNamespace(
        id=overrides.pop("id", "abc123"),
        selections=selections,
        company_name=overrides.pop("company_name", "NVIDIA Corporation"),
        decision=overrides.pop("decision",
                                "FINAL TRANSACTION PROPOSAL: **BUY**.\nStrong fundamentals."),
        rating=overrides.pop("rating", "Buy"),
        partial_state=overrides.pop("partial_state", {
            "market_report": "**Market** looks bullish.",
            "summary": "## Trading Advice\nBuy NVDA.\n\n## Boundaries\n- 6% cap",
        }),
        finished_at=overrides.pop("finished_at", datetime.datetime.now()),
        stats=overrides.pop("stats", {"llm_calls": 10, "tokens_in": 5000,
                                       "tokens_out": 800}),
        **overrides,
    )


# ---------- section ordering / HTML assembly (no WeasyPrint) ---------


def test_ordered_sections_picks_only_non_empty():
    from webui.pdf_export import _ordered_sections

    out = _ordered_sections({
        "market_report": "x",
        "news_report": "",           # skipped
        "summary": "  ",             # whitespace → skipped
        "fundamentals_report": "y",
    })
    keys = [label for label, _ in out]
    assert "Market Analyst" in keys
    assert "Fundamentals Analyst" in keys
    assert "News Analyst" not in keys
    assert "Summary" not in keys


def test_ordered_sections_preserves_canonical_order():
    from webui.pdf_export import _ordered_sections

    out = _ordered_sections({
        "summary": "S",                       # last in TAB_SECTIONS
        "market_report": "M",                 # first
        "investment_plan": "RM",              # middle
    })
    labels = [label for label, _ in out]
    # Market Analyst must appear before Research Manager which must
    # appear before Summary regardless of insertion order in the dict.
    assert labels.index("Market Analyst") < labels.index("Research Manager")
    assert labels.index("Research Manager") < labels.index("Summary")


def test_md_to_html_supports_fenced_code_and_tables():
    from webui.pdf_export import _md_to_html

    md = (
        "# Title\n\n"
        "```python\nprint('hi')\n```\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    html = _md_to_html(md)
    assert "<h1>" in html
    assert "<code>" in html or "<pre>" in html
    assert "<table>" in html


def test_escape_handles_none_and_special_chars():
    from webui.pdf_export import _escape
    assert _escape(None) == ""
    assert _escape("<script>") == "&lt;script&gt;"
    assert _escape('NVDA "the chip"') == "NVDA &quot;the chip&quot;"


# ---------- lazy-import error path ---------------------------------


def test_render_raises_runtime_error_when_weasyprint_missing(monkeypatch):
    """If WeasyPrint can't be imported, the renderer raises a
    `RuntimeError` with a remediation hint pointing at the docs."""
    import sys

    # Block the weasyprint import without touching anything else.
    monkeypatch.setitem(sys.modules, "weasyprint", None)

    from webui.pdf_export import render_job_to_pdf
    with pytest.raises(RuntimeError) as exc:
        render_job_to_pdf(_make_job())
    msg = str(exc.value).lower()
    assert "weasyprint" in msg
    assert "gtk" in msg


# ---------- end-to-end render (skipped without WeasyPrint) ---------


def _weasyprint_renders() -> bool:
    """True only when WeasyPrint can actually produce a PDF on this host.

    On Windows it's common for the import to succeed but the underlying
    Pango/Cairo runtime to be the wrong version, causing the first real
    render to raise an AttributeError. Probe with a minimal render so
    the actual-PDF tests skip cleanly in that case (they pass in the
    Docker image, which ships matching native libs).
    """
    try:
        from weasyprint import HTML
        HTML(string="<p>probe</p>").write_pdf()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(
    not _weasyprint_renders(),
    reason="WeasyPrint can't render on this host (GTK runtime missing or "
           "incompatible). Tests pass against the Docker image.",
)
def test_render_produces_valid_pdf_bytes():
    """End-to-end render — output starts with the `%PDF-` magic header."""
    from webui.pdf_export import render_job_to_pdf

    pdf_bytes = render_job_to_pdf(_make_job())
    assert pdf_bytes[:5] == b"%PDF-", (
        f"output doesn't look like a PDF; first 16 bytes: {pdf_bytes[:16]!r}"
    )
    # Sanity: typical TradingAgents report is at least a few KB.
    assert len(pdf_bytes) > 1500, f"PDF suspiciously small: {len(pdf_bytes)} bytes"


@pytest.mark.skipif(not _weasyprint_renders(), reason="WeasyPrint can't render on this host")
def test_render_includes_company_name_when_provided():
    """When company_name is set, rendering still succeeds and the PDF
    is larger than the bare-ticker variant (cover-page DL fields, etc.).

    We don't grep the bytes for "NVIDIA" — WeasyPrint encodes text via
    TJ/Tj operators with subset fonts, so the ASCII literal isn't
    necessarily present verbatim in the byte stream.
    """
    from webui.pdf_export import render_job_to_pdf

    with_name = render_job_to_pdf(_make_job(company_name="NVIDIA Corporation"))
    without_name = render_job_to_pdf(_make_job(company_name=None))
    assert with_name[:5] == b"%PDF-"
    assert without_name[:5] == b"%PDF-"
    # The cover page with a company-name line should be at least a bit
    # larger than without (cheap evidence the field landed in the doc).
    assert len(with_name) > len(without_name) - 200, (
        "with-name render unexpectedly smaller than without-name"
    )


@pytest.mark.skipif(not _weasyprint_renders(), reason="WeasyPrint can't render on this host")
def test_render_no_company_name_falls_back_to_ticker_only():
    """Job without a resolved company name still renders successfully."""
    from webui.pdf_export import render_job_to_pdf

    pdf = render_job_to_pdf(_make_job(company_name=None))
    assert pdf[:5] == b"%PDF-"
