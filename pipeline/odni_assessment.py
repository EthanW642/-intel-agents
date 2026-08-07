"""Stage 3e — ODNI Annual Threat Assessment excerpt (standing IC context —
NOT part of the Tier 1-4 news-sourcing scale), added 2026-08-06 as the
other half of "actual published intelligence reports."

Unlike CRS reports, the Annual Threat Assessment is published once a year
(most recently March 2026) as a PDF, not something with a live feed or
API — so this is a MANUALLY CURATED, version-controlled excerpt file
(config/odni_ata_excerpt.md), not a network fetch.

IMPORTANT — this deliberately does NOT ship with fabricated content. The
excerpt file ships with a placeholder and is omitted from the analysis
prompt until a human fills it in with real text copied from the actual
published PDF. This pipeline must never invent ODNI assessment language
and attribute it to the agency — that would corrupt the analysis with
fabricated "intelligence" presented as genuine government assessment.

To populate config/odni_ata_excerpt.md:
1. Download the current Annual Threat Assessment PDF from
   https://www.dni.gov/index.php/newsroom/reports-publications (search
   "Annual Threat Assessment").
2. Copy the Iran / Middle East-relevant section(s) verbatim into the file,
   replacing the placeholder comment block entirely.
3. Update the "Excerpt from: <year>" line at the top once populated.
4. Repeat roughly once a year, when the next ATA is published (typically
   March) — the file's own header is the reminder.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ODNI_EXCERPT_PATH = Path(__file__).parent.parent / "config" / "odni_ata_excerpt.md"
PLACEHOLDER_MARKER = "NOT YET POPULATED"


def load_odni_excerpt() -> str | None:
    """Returns the manually-curated excerpt text, or None if the file is
    missing or still carries the shipped placeholder (nobody has filled it
    in yet) — same degrade-gracefully pattern as the live-fetched context
    sources (oil price, satellite hotspots, CRS reports): a missing
    optional input just omits that piece of context."""
    if not ODNI_EXCERPT_PATH.exists():
        return None
    text = ODNI_EXCERPT_PATH.read_text().strip()
    if not text or PLACEHOLDER_MARKER in text:
        logger.info(
            "ODNI ATA excerpt not populated yet (%s still has the placeholder) — "
            "omitting from prompt. See that file's header comment for instructions.",
            ODNI_EXCERPT_PATH,
        )
        return None
    return text
