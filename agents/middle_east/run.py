"""Full 5-stage pipeline run for the Middle East agent (Phase 1 scope).

INGEST -> DEDUP/TRIAGE/PREDICTION-RESOLUTION -> MEMORY QUERY -> DEEP ANALYSIS -> WRITE-BACK/RENDER/EMAIL
"""
from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

from agents.middle_east.ingest import run_ingest
from pipeline.analyze import run_analysis
from pipeline.dedup import dedup_items
from pipeline.deliver import send_briefing_email, send_ollama_outage_alert
from pipeline.memory import init_store, query_memory, write_back
from pipeline.ollama_client import OllamaUnavailableError
from pipeline.predictions import resolve_predictions
from pipeline.render import render_briefing
from pipeline.run_log import write_run_log_row
from pipeline.triage import triage_items
from store import sqlite_client as db

logger = logging.getLogger(__name__)

DOMAIN = "middle_east"
ROOT = Path(__file__).parent.parent.parent
WATCHLIST_PATH = ROOT / "config" / "watchlists.yaml"
SQLITE_PATH = ROOT / "data" / f"{DOMAIN}.sqlite3"
CHROMA_DIR = ROOT / "data" / "chroma"


def run() -> Path | None:
    load_dotenv()
    watchlist_cfg = yaml.safe_load(WATCHLIST_PATH.read_text())
    pipeline_cfg = watchlist_cfg["pipeline"]
    run_date = date.today()

    log_row: dict = {"run_date": run_date.isoformat(), "domain": DOMAIN}

    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn, collection = init_store(DOMAIN, str(SQLITE_PATH), str(CHROMA_DIR), watchlist_cfg)

    try:
        logger.info("Stage 1: ingest")
        raw_items = run_ingest()
        log_row["items_ingested"] = len(raw_items)
        if not raw_items:
            logger.warning("No raw items ingested this run (check network/source config)")

        logger.info("Stage 2a: dedup")
        deduped = dedup_items(raw_items, similarity_threshold=pipeline_cfg["dedup_similarity_threshold"])
        log_row["items_deduped"] = len(deduped)

        logger.info("Stage 2b: triage")
        entities = [dict(row) for row in db.get_entities(conn, DOMAIN)]
        theses = [dict(row) for row in db.get_active_theses(conn, DOMAIN)]
        triaged = triage_items(
            deduped,
            entities,
            theses,
            ollama_host=pipeline_cfg["ollama_host"],
            model=pipeline_cfg["triage_model"],
            score_threshold=pipeline_cfg["triage_score_threshold"],
        )
        log_row["items_triaged_survived"] = len(triaged)

        logger.info("Stage 2c: prediction resolution check (local, no API cost)")
        pending = [dict(row) for row in db.get_pending_predictions(conn, DOMAIN)]
        # Deliberately `triaged`, not `deduped` (spec 3.C intent, but also a
        # real fix: confirmed live 2026-07-25 that dumping all ~393 deduped
        # items into one prediction-resolution prompt blew Ollama's context
        # window, silently truncating input and producing confident-looking
        # verdicts about news the model never actually saw. `triaged` is a
        # ~6x smaller, already relevance-filtered set — comfortably fits in
        # context, and a prediction can only be legitimately confirmed/
        # contradicted by something that already cleared the relevance bar.
        resolutions = resolve_predictions(
            pending, triaged, ollama_host=pipeline_cfg["ollama_host"], model=pipeline_cfg["triage_model"]
        )
        for r in resolutions:
            db.resolve_prediction(conn, r["id"], r["verdict"], r["reason"])
        log_row["predictions_resolved"] = len(resolutions)

        logger.info("Stage 3: memory query")
        memory_context = query_memory(
            conn,
            collection,
            DOMAIN,
            triaged,
            pipeline_cfg["memory_retrieval_count"],
            dormancy_window_days=pipeline_cfg["dormancy_window_days"],
            predictions_min_for_track_record=pipeline_cfg["predictions_min_for_track_record"],
            run_date=run_date,
        )

        logger.info("Stage 4: deep analysis (Sonnet — the only API call)")
        result = run_analysis(
            triaged,
            memory_context,
            domain=DOMAIN,
            model=pipeline_cfg["analysis_model"],
            max_tokens=pipeline_cfg["analysis_max_tokens"],
            pipeline_cfg=pipeline_cfg,
        )
        log_row.update(
            {
                "effort": result["effort"],
                "input_tokens": result["usage"]["input_tokens"],
                "output_tokens": result["usage"]["output_tokens"],
                "cost_usd": f"{result['cost_usd']:.6f}",
            }
        )

        logger.info("Stage 5: write-back + render")
        write_summary = write_back(conn, collection, DOMAIN, result["write_back"], run_date.isoformat())
        log_row.update(write_summary)
        briefing_path = render_briefing(DOMAIN, result["markdown"], run_date)

        logger.info("Stage 5: email delivery")
        gmail_address = os.environ.get("GMAIL_ADDRESS")
        gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
        if not gmail_address or not gmail_app_password:
            logger.warning(
                "GMAIL_ADDRESS/GMAIL_APP_PASSWORD not set — skipping email delivery. "
                "Briefing is still saved at %s.",
                briefing_path,
            )
            log_row.update({"email_sent": False, "email_retried": False, "email_error": "credentials not configured"})
        else:
            email_result = send_briefing_email(
                result["markdown"], briefing_path, gmail_address, gmail_app_password, run_date
            )
            log_row.update(
                {
                    "email_sent": email_result["sent"],
                    "email_retried": email_result["retried"],
                    "email_error": email_result["error"] or "",
                }
            )

        logger.info(
            "Run complete. Briefing at %s. Cost: $%.4f (effort=%s)",
            briefing_path,
            result["cost_usd"],
            result["effort"],
        )
        return briefing_path
    except OllamaUnavailableError as exc:
        # Every triage/prediction-resolution batch failed to reach Ollama —
        # a local infrastructure outage, not a quiet news day. Abort before
        # the Sonnet call (nothing real to analyze) and send a plain-text
        # alert instead of the briefing, so the outage is never silently
        # mistaken for "nothing happened today."
        logger.error("Aborting before Sonnet call: %s", exc)
        log_row["ollama_outage"] = True
        log_row["pipeline_error"] = str(exc)
        gmail_address = os.environ.get("GMAIL_ADDRESS")
        gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
        if gmail_address and gmail_app_password:
            alert_result = send_ollama_outage_alert(str(exc), gmail_address, gmail_app_password, run_date)
            log_row.update(
                {
                    "email_sent": alert_result["sent"],
                    "email_retried": alert_result["retried"],
                    "email_error": alert_result["error"] or "",
                }
            )
        else:
            logger.warning("GMAIL_ADDRESS/GMAIL_APP_PASSWORD not set — cannot send outage alert email")
            log_row.update({"email_sent": False, "email_retried": False, "email_error": "credentials not configured"})
        return None
    except Exception as exc:
        logger.exception("Pipeline run failed")
        log_row["pipeline_error"] = str(exc)
        raise
    finally:
        write_run_log_row(log_row)
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
