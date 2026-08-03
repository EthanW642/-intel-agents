"""Stage 5 (part 2) — render the day's briefing to a local Markdown file,
plus a minimal Markdown -> HTML conversion used by the email delivery
stage (pipeline/deliver.py). The Markdown file is always written
regardless of email outcome (spec section 6) — this module has no
dependency on deliver.py, only the reverse.
"""
from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path

BRIEFINGS_DIR = Path(__file__).parent.parent / "briefings"


def render_briefing(domain: str, analysis_markdown: str, run_date: date | None = None) -> Path:
    run_date = run_date or date.today()
    BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = BRIEFINGS_DIR / f"{domain}_{run_date.isoformat()}.md"

    header = f"# {domain.replace('_', ' ').title()} Briefing — {run_date.isoformat()}\n\n"
    out_path.write_text(header + analysis_markdown)
    return out_path


_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline(text: str) -> str:
    text = html.escape(text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    text = _INLINE_CODE_RE.sub(r"<code>\1</code>", text)
    return text


def markdown_to_html(md_text: str) -> str:
    """Small, dependency-free Markdown -> HTML conversion — enough for the
    briefing template's structure (headers, bullet/numbered lists, bold/
    italic/inline code, fenced code blocks, links, paragraphs). Not a
    general-purpose Markdown renderer; not meant to be one."""
    lines = md_text.splitlines()
    html_parts: list[str] = []
    in_code_block = False
    in_list: str | None = None  # "ul" | "ol" | None
    paragraph_buf: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph_buf
        if paragraph_buf:
            html_parts.append(f"<p>{_inline(' '.join(paragraph_buf))}</p>")
            paragraph_buf = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            html_parts.append(f"</{in_list}>")
            in_list = None

    for line in lines:
        if line.strip().startswith("```"):
            flush_paragraph()
            close_list()
            if in_code_block:
                html_parts.append("</code></pre>")
            else:
                html_parts.append("<pre><code>")
            in_code_block = not in_code_block
            continue
        if in_code_block:
            html_parts.append(html.escape(line))
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading_match:
            flush_paragraph()
            close_list()
            level = len(heading_match.group(1))
            html_parts.append(f"<h{level}>{_inline(heading_match.group(2))}</h{level}>")
            continue

        bullet_match = re.match(r"^\s*[-*]\s+(.*)$", line)
        numbered_match = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if bullet_match:
            flush_paragraph()
            if in_list != "ul":
                close_list()
                html_parts.append("<ul>")
                in_list = "ul"
            html_parts.append(f"<li>{_inline(bullet_match.group(1))}</li>")
            continue
        if numbered_match:
            flush_paragraph()
            if in_list != "ol":
                close_list()
                html_parts.append("<ol>")
                in_list = "ol"
            html_parts.append(f"<li>{_inline(numbered_match.group(1))}</li>")
            continue

        if not line.strip():
            flush_paragraph()
            close_list()
            continue

        paragraph_buf.append(line.strip())

    flush_paragraph()
    close_list()
    if in_code_block:
        html_parts.append("</code></pre>")

    body = "\n".join(html_parts)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<style>body{font-family:-apple-system,Helvetica,Arial,sans-serif;"
        "max-width:720px;margin:2rem auto;line-height:1.5;color:#1a1a1a}"
        "h1,h2,h3{border-bottom:1px solid #ddd;padding-bottom:0.2rem}"
        "pre{background:#f4f4f4;padding:0.75rem;overflow-x:auto;border-radius:4px}"
        "code{font-family:ui-monospace,Menlo,Consolas,monospace}</style>"
        f"</head><body>{body}</body></html>"
    )
