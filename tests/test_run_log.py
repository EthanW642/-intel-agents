import csv

from pipeline import run_log


def test_write_run_log_row_creates_header_and_row(tmp_path, monkeypatch):
    log_path = tmp_path / "run_log.csv"
    monkeypatch.setattr(run_log, "RUN_LOG_PATH", log_path)

    run_log.write_run_log_row(
        {
            "run_date": "2026-07-23",
            "domain": "middle_east",
            "effort": "medium",
            "input_tokens": 1000,
            "output_tokens": 500,
            "cost_usd": "0.01",
            "email_sent": True,
        }
    )

    with open(log_path) as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["domain"] == "middle_east"
    assert rows[0]["effort"] == "medium"
    assert rows[0]["email_sent"] == "True"
    assert rows[0]["pipeline_error"] == ""  # unset fields default to empty, not KeyError


def test_write_run_log_row_appends_without_duplicate_header(tmp_path, monkeypatch):
    log_path = tmp_path / "run_log.csv"
    monkeypatch.setattr(run_log, "RUN_LOG_PATH", log_path)

    run_log.write_run_log_row({"run_date": "2026-07-23", "domain": "middle_east"})
    run_log.write_run_log_row({"run_date": "2026-07-24", "domain": "middle_east"})

    with open(log_path) as f:
        lines = f.readlines()

    assert lines[0].startswith("timestamp,")
    assert len(lines) == 3  # header + 2 rows, no repeated header
