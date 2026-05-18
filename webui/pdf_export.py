"""PDF export for finished analyses.

Uses WeasyPrint (HTML/CSS → PDF) because:
  * The output is a typeset document with markdown rendering, code blocks,
    tables, and proper page breaks — WeasyPrint handles all of that
    natively.
  * The library is pure Python at the import layer; only its Pango +
    Cairo runtime libs need to be present (already in the Dockerfile).
  * The same markdown extensions the web UI uses (`fenced_code`,
    `tables`, `nl2br`) flow straight through, so the PDF reads like a
    print version of the job-detail tabs.

Lazy-imports WeasyPrint so importing this module on a host without the
GTK runtime doesn't blow up — the actual `ImportError` surfaces only
when an operator hits the PDF endpoint.
"""

from __future__ import annotations

import logging
import re
from io import BytesIO
from pathlib import Path
from typing import Any, List, Tuple

import markdown as md_lib


# Characters that DejaVu Sans / Sans Mono (the WeasyPrint fallback in the
# Docker image) does not have glyphs for — they render as tofu boxes or
# vanish. LLM analyst reports frequently include emoji, dingbats, and
# pictographs (📈 ✓ ★ ⚡ ⚠ 🎯 …), so we strip the whole pictograph + symbol
# range before handing the markdown to WeasyPrint. Punctuation, arrows,
# CJK, and accented Latin are deliberately NOT in here — those render
# fine and are sometimes load-bearing in non-English output.
_GLYPH_STRIP_RE = re.compile(
    "["
    "☀-➿"           # Misc Symbols + Dingbats (sun, lightning,
                              # check, cross, star, hand pointers)
    "︀-️"           # Variation selectors (emoji-presentation marks)
    "‍"                  # ZWJ — joins multi-codepoint emoji
    "\U0001F000-\U0001FFFF"   # All SMP pictograph planes: emoticons,
                              # transport, supplemental symbols,
                              # extended-A, legacy computing symbols
    "]+",
)
_INTERNAL_WS_RE = re.compile(r"[ \t]{2,}")


def _strip_unsupported_glyphs(text: str) -> str:
    """Remove emoji/pictograph characters that render as tofu in WeasyPrint.

    Also collapses the runs of spaces left behind so an emoji in the
    middle of a sentence doesn't leave a visible double-space. Preserves
    leading indent and line structure so markdown bullets / code blocks
    still parse correctly.
    """
    if not text:
        return text
    cleaned = _GLYPH_STRIP_RE.sub("", text)
    lines = []
    for raw in cleaned.splitlines():
        line = raw.rstrip()
        body = line.lstrip(" \t")
        indent = line[: len(line) - len(body)]
        lines.append(indent + _INTERNAL_WS_RE.sub(" ", body))
    return "\n".join(lines)


log = logging.getLogger("tradingagents.webui.pdf_export")


# CSS lives in this module rather than a separate file so the PDF renders
# the same regardless of which static-asset mount the operator has
# configured. Print-oriented: A4, sensible margins, page numbers, soft
# colors, syntax-highlighted code blocks.
_CSS = """
@page {
    size: A4;
    margin: 18mm 16mm 22mm 16mm;
    @bottom-right {
        content: "Page " counter(page) " of " counter(pages);
        font-family: "DejaVu Sans", sans-serif;
        font-size: 8pt;
        color: #888;
    }
    @bottom-left {
        content: string(report-meta);
        font-family: "DejaVu Sans", sans-serif;
        font-size: 8pt;
        color: #888;
    }
}
@page :first {
    @bottom-left { content: ""; }
    @bottom-right { content: ""; }
}

html { font-family: "DejaVu Sans", "Helvetica", "Arial", sans-serif;
       font-size: 10pt; color: #1a1d22; line-height: 1.5; }
h1 { font-size: 22pt; margin: 0 0 0.4em; color: #1f3a5f; }
h2 { font-size: 14pt; margin: 1.6em 0 0.3em; color: #1f3a5f;
     border-bottom: 1px solid #ccd; padding-bottom: 4pt; }
h3 { font-size: 11pt; margin: 1.1em 0 0.2em; color: #2a4a78; }
h4 { font-size: 10pt; margin: 0.9em 0 0.2em; color: #2a4a78; }

a { color: #1f5fbf; text-decoration: none; }
p, ul, ol, blockquote { margin: 0 0 0.6em; }
ul, ol { padding-left: 1.4em; }
li { margin-bottom: 0.15em; }

code { font-family: "DejaVu Sans Mono", monospace; font-size: 9pt;
       background: #f1f1f4; padding: 0 3pt; border-radius: 2pt; }
pre { font-family: "DejaVu Sans Mono", monospace; font-size: 8.5pt;
      background: #f5f5f8; border-left: 3pt solid #c7c7d3; padding: 6pt 9pt;
      white-space: pre-wrap; word-wrap: break-word; }
pre code { background: transparent; padding: 0; }

table { border-collapse: collapse; margin: 0.6em 0; width: 100%;
        font-size: 9pt; }
th, td { border: 1px solid #c7c7d3; padding: 4pt 7pt; text-align: left;
         vertical-align: top; }
th { background: #eef0f6; font-weight: 600; }

.cover { string-set: report-meta attr(data-meta); page-break-after: always; }
.cover h1 { font-size: 28pt; margin-bottom: 0.1em; }
.cover .ticker-line { font-size: 14pt; color: #555; margin: 0 0 1em; }
.cover .meta { margin-top: 2em; font-size: 10pt; color: #444;
               border-top: 1px solid #ddd; padding-top: 1em; }
.cover .meta dt { font-weight: 600; float: left; clear: left; width: 30%;
                  padding-bottom: 4pt; }
.cover .meta dd { margin: 0 0 4pt 30%; }
.cover .decision { margin-top: 2.5em; padding: 14pt 18pt;
                   background: #f0f6ff; border-left: 4pt solid #1f5fbf;
                   border-radius: 2pt; font-size: 11pt; }
.cover .decision .label { font-weight: 600; color: #1f3a5f; margin-bottom: 4pt;
                          text-transform: uppercase; font-size: 9pt;
                          letter-spacing: 0.05em; }

.section { page-break-before: always; }
.dim { color: #777; font-size: 9pt; }

footer.signoff { margin-top: 4em; padding-top: 1em; border-top: 1px solid #ddd;
                 font-size: 8.5pt; color: #888; }
"""


# Friendly section titles. Mirrors webui.runner.TAB_SECTIONS but adds
# the Summary entry and orders the debate transcripts together.
def _ordered_sections(partial_state: dict) -> List[Tuple[str, str]]:
    """Pick (label, markdown) tuples from partial_state in display order,
    skipping empties.
    """
    # Defer the import to avoid a circular reference at module load.
    from webui.runner import ALL_TAB_SECTIONS

    ordered = []
    for _key, label, src in ALL_TAB_SECTIONS:
        content = partial_state.get(src)
        if isinstance(content, str) and content.strip():
            ordered.append((label, content))
    return ordered


def _md_to_html(md: str) -> str:
    """Render markdown with the same extensions the web UI uses.

    Glyph-strip runs on the source markdown (not the rendered HTML) so
    we don't accidentally chew through tag characters.
    """
    return md_lib.markdown(
        _strip_unsupported_glyphs(md or ""),
        extensions=["fenced_code", "tables", "nl2br"],
    )


def _format_decision(text: str | None) -> str:
    if not text:
        return "<em>No decision recorded.</em>"
    return _md_to_html(text)


def render_job_to_pdf(job: Any) -> bytes:
    """Render a finished job's reports as a PDF (returns the bytes).

    `job` is a `JobState` (from the in-memory registry) or any object
    exposing `selections.ticker`, `selections.analysis_date`,
    `company_name`, `decision`, `rating`, `partial_state`,
    `finished_at`, `stats`. The `RecentRunRow`-built job returned by
    `_load_job_from_db` works unchanged.

    Raises `RuntimeError` when WeasyPrint isn't installed (host
    installs without GTK). Callers can surface this as a 500 with a
    pointed message.
    """
    try:
        # Lazy import — keeps `webui.pdf_export` importable on hosts
        # without the GTK runtime libs WeasyPrint requires.
        from weasyprint import HTML, CSS
    except ImportError as e:
        raise RuntimeError(
            "PDF export requires WeasyPrint + GTK. The Docker image bundles "
            "it; for host installs see "
            "https://doc.courtbouillon.org/weasyprint/stable/first_steps.html"
        ) from e

    sections = _ordered_sections(getattr(job, "partial_state", {}) or {})
    company_name = _strip_unsupported_glyphs(getattr(job, "company_name", None) or "")
    ticker = job.selections.ticker
    analysis_date = job.selections.analysis_date
    title_line = f"{ticker} — {company_name}" if company_name else ticker
    meta_line = f"{title_line} · {analysis_date}"

    # Build the HTML. Inline assembly is simpler than a Jinja template
    # given the small surface — and avoids loading the template engine
    # for a one-off PDF render.
    finished_at = getattr(job, "finished_at", None)
    stats = getattr(job, "stats", {}) or {}
    selections_obj = job.selections

    html_parts = [
        '<!doctype html><html><head><meta charset="utf-8">',
        f"<title>TradingAgents Report — {ticker} ({analysis_date})</title>",
        "</head><body>",
        # Cover page.
        f'<div class="cover" data-meta="{_escape(meta_line)}">',
        f"<h1>{_escape(ticker)}</h1>",
    ]
    if company_name:
        html_parts.append(f'<div class="ticker-line">{_escape(company_name)}</div>')
    html_parts.extend([
        f'<div class="ticker-line">Analysis date: <strong>{_escape(analysis_date)}</strong></div>',
        '<div class="decision">',
        '<div class="label">Portfolio Manager Decision</div>',
        _format_decision(getattr(job, "decision", None)),
        "</div>",
        "<dl class='meta'>",
        f"<dt>Rating</dt><dd>{_escape(getattr(job, 'rating', None) or '—')}</dd>",
        f"<dt>Provider</dt><dd>{_escape(getattr(selections_obj, 'llm_provider', '—'))} · deep={_escape(getattr(selections_obj, 'deep_thinker', '—'))} · quick={_escape(getattr(selections_obj, 'quick_thinker', '—'))}</dd>",
        f"<dt>Analysts</dt><dd>{_escape(', '.join(getattr(selections_obj, 'analysts', []) or []))}</dd>",
        f"<dt>Research depth</dt><dd>{getattr(selections_obj, 'research_depth', '—')}</dd>",
    ])
    if finished_at is not None:
        html_parts.append(
            f"<dt>Completed</dt><dd>{_escape(finished_at.strftime('%Y-%m-%d'))}</dd>"
        )
    if stats:
        html_parts.append(
            f"<dt>Token usage</dt><dd>"
            f"{stats.get('llm_calls', 0)} LLM calls · "
            f"{stats.get('tokens_in', 0):,} in · {stats.get('tokens_out', 0):,} out"
            f"</dd>"
        )
    html_parts.append("</dl>")
    html_parts.append(
        "<footer class='signoff'>Generated by "
        "<a href='https://github.com/TauricResearch/TradingAgents'>TradingAgents</a>. "
        "This document is for research / educational purposes and does not "
        "constitute financial advice."
        "</footer>"
    )
    html_parts.append("</div>")  # close cover

    # One <section> per report block.
    for label, markdown in sections:
        html_parts.append(
            f'<section class="section"><h2>{_escape(label)}</h2>'
            f"{_md_to_html(markdown)}</section>"
        )

    html_parts.append("</body></html>")
    html_doc = "".join(html_parts)

    buf = BytesIO()
    HTML(string=html_doc).write_pdf(
        buf,
        stylesheets=[CSS(string=_CSS)],
    )
    return buf.getvalue()


def _escape(text: object) -> str:
    """Tiny HTML escape — `markdown` already escapes report bodies, but the
    cover-page interpolations come from raw strings (ticker, company name)
    that we should defend against quote / angle-bracket leakage."""
    import html as _html
    return _html.escape(str(text) if text is not None else "")
