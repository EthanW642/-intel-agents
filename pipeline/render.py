"""Stage 5 (part 2) — render the day's briefing to a local Markdown file."""
from __future__ import annotations

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
