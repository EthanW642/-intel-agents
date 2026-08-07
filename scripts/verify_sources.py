"""Spec-mandated pre-flight check: confirm every source in
config/sources.yaml actually resolves and returns current items before the
pipeline is built around it (or, since it already is, before you trust it
in production). Run this on a machine with real network access — it
CANNOT be run from a sandboxed CI/build environment with outbound
restrictions.

Usage:
    python scripts/verify_sources.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import httpx
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.middle_east.sources import ASAM_API_BASE, RSS_REQUEST_HEADERS  # noqa: E402 — see sys.path insert above

ROOT = Path(__file__).parent.parent
SOURCES_PATH = ROOT / "config" / "sources.yaml"
STALE_AFTER_DAYS = 14  # a feed that resolves but hasn't posted in 2 weeks is suspicious, not dead


def _check_rss(name: str, url: str, timeout: float = 15.0) -> tuple[bool, str]:
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers=RSS_REQUEST_HEADERS)
        redirect_note = ""
        if resp.history:
            chain = " -> ".join(str(r.url) for r in resp.history) + f" -> {resp.url}"
            redirect_note = f" [redirected: {chain}]"
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        chain = ""
        if exc.response.history:
            chain = " -> ".join(str(r.url) for r in exc.response.history) + f" -> {exc.response.url}"
            chain = f" — redirected via: {chain}"
        return False, f"HTTP fetch failed: {exc}{chain}"
    except Exception as exc:
        return False, f"HTTP fetch failed: {exc}"

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        return False, f"feed did not parse as RSS/Atom: {parsed.bozo_exception}"
    if not parsed.entries:
        return False, "feed parsed but has zero entries"

    newest = None
    for entry in parsed.entries[:5]:
        for field in ("published_parsed", "updated_parsed"):
            struct = entry.get(field)
            if struct:
                candidate = datetime(*struct[:6], tzinfo=timezone.utc)
                if newest is None or candidate > newest:
                    newest = candidate

    if newest is None:
        return True, f"{len(parsed.entries)} entries, but none had a parseable date — spot-check manually{redirect_note}"

    age_days = (datetime.now(timezone.utc) - newest).days
    if age_days > STALE_AFTER_DAYS:
        return True, f"{len(parsed.entries)} entries, but newest is {age_days} days old — feed may be stale/dead{redirect_note}"
    return True, f"{len(parsed.entries)} entries, newest {age_days} day(s) old — looks live{redirect_note}"


def _check_gdelt(timeout: float = 15.0) -> tuple[bool, str]:
    url = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        return False, f"HTTP fetch failed: {exc}"
    if "gdeltv2" not in resp.text:
        return False, "response didn't look like the expected GDELT file listing"
    return True, "data.gdeltproject.org reachable and returned an update listing"


def _check_asam(timeout: float = 20.0) -> tuple[bool, str]:
    """NGA/MSI ASAM is UNVERIFIED from the build environment (see
    agents/middle_east/sources.py's module comment) — this is the first
    real network confirmation of the endpoint path, so a FAIL here is
    expected to be informative, not surprising."""
    try:
        resp = httpx.get(ASAM_API_BASE, params={"output": "json"}, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        parsed = resp.json()
    except Exception as exc:
        return False, f"HTTP fetch failed: {exc} — endpoint/params in sources.py may need updating"
    count = len(parsed) if isinstance(parsed, list) else "unknown shape"
    return True, f"msi.nga.mil/api/publications/asam reachable, returned {count} record(s) (verify field names manually)"


def main() -> int:
    cfg = yaml.safe_load(SOURCES_PATH.read_text())
    # status per result: "OK" | "FAIL" — no more "SKIPPED" case since
    # LiveUAMap (the one disabled-but-configured source that needed it) was
    # retired 2026-07-31 in favor of a Google News RSS query in rss_feeds.
    results: list[tuple[str, str, str]] = []

    ok, detail = _check_gdelt()
    results.append(("GDELT", "OK" if ok else "FAIL", detail))

    ok, detail = _check_asam()
    results.append(("NGA ASAM (shipping)", "OK" if ok else "FAIL", detail))

    for feed in cfg.get("rss_feeds", []):
        ok, detail = _check_rss(feed["name"], feed["url"])
        results.append((feed["name"], "OK" if ok else "FAIL", detail))

    print(f"{'SOURCE':<28}{'STATUS':<10}DETAIL")
    all_ok = True
    for name, status, detail in results:
        if status == "FAIL":
            all_ok = False
        print(f"{name:<28}{status:<10}{detail}")

    if not all_ok:
        print("\nOne or more enabled sources failed to resolve. Fix config/sources.yaml before relying on them.")
        return 1
    print("\nAll enabled sources resolved. Spot-check the 'looks live' vs. 'may be stale' notes above too.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
