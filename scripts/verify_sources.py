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

ROOT = Path(__file__).parent.parent
SOURCES_PATH = ROOT / "config" / "sources.yaml"
STALE_AFTER_DAYS = 14  # a feed that resolves but hasn't posted in 2 weeks is suspicious, not dead


def _check_rss(name: str, url: str, timeout: float = 15.0) -> tuple[bool, str]:
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
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
        return True, f"{len(parsed.entries)} entries, but none had a parseable date — spot-check manually"

    age_days = (datetime.now(timezone.utc) - newest).days
    if age_days > STALE_AFTER_DAYS:
        return True, f"{len(parsed.entries)} entries, but newest is {age_days} days old — feed may be stale/dead"
    return True, f"{len(parsed.entries)} entries, newest {age_days} day(s) old — looks live"


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


def main() -> int:
    cfg = yaml.safe_load(SOURCES_PATH.read_text())
    results: list[tuple[str, bool, str]] = []

    ok, detail = _check_gdelt()
    results.append(("GDELT", ok, detail))

    liveuamap = cfg.get("liveuamap", {})
    if liveuamap.get("feed_url"):
        ok, detail = _check_rss("LiveUAMap", liveuamap["feed_url"])
        results.append(("LiveUAMap", ok, detail))

    for feed in cfg.get("rss_feeds", []):
        ok, detail = _check_rss(feed["name"], feed["url"])
        results.append((feed["name"], ok, detail))

    print(f"{'SOURCE':<28}{'STATUS':<8}DETAIL")
    all_ok = True
    for name, ok, detail in results:
        status = "OK" if ok else "FAIL"
        all_ok = all_ok and ok
        print(f"{name:<28}{status:<8}{detail}")

    if not all_ok:
        print("\nOne or more sources failed to resolve. Fix config/sources.yaml before relying on them.")
        return 1
    print("\nAll sources resolved. Spot-check the 'looks live' vs. 'may be stale' notes above too.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
