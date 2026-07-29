from datetime import date
from unittest.mock import MagicMock

from scripts import run_if_not_already_today as trigger


def test_already_ran_today_false_when_log_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(trigger, "RUN_LOG_PATH", tmp_path / "run_log.csv")
    assert trigger.already_ran_today(date(2026, 7, 29)) is False


def test_already_ran_today_false_when_log_has_only_other_dates(tmp_path, monkeypatch):
    log_path = tmp_path / "run_log.csv"
    log_path.write_text("run_date,domain\n2026-07-28,middle_east\n")
    monkeypatch.setattr(trigger, "RUN_LOG_PATH", log_path)
    assert trigger.already_ran_today(date(2026, 7, 29)) is False


def test_already_ran_today_true_when_todays_row_present(tmp_path, monkeypatch):
    log_path = tmp_path / "run_log.csv"
    log_path.write_text("run_date,domain\n2026-07-28,middle_east\n2026-07-29,middle_east\n")
    monkeypatch.setattr(trigger, "RUN_LOG_PATH", log_path)
    assert trigger.already_ran_today(date(2026, 7, 29)) is True


def test_main_skips_run_when_already_ran_today(tmp_path, monkeypatch):
    log_path = tmp_path / "run_log.csv"
    log_path.write_text(f"run_date,domain\n{date.today().isoformat()},middle_east\n")
    monkeypatch.setattr(trigger, "RUN_LOG_PATH", log_path)
    run_mock = MagicMock()
    monkeypatch.setattr(trigger, "run", run_mock)

    trigger.main()

    run_mock.assert_not_called()


def test_main_runs_when_not_already_run_today(tmp_path, monkeypatch):
    monkeypatch.setattr(trigger, "RUN_LOG_PATH", tmp_path / "run_log.csv")
    run_mock = MagicMock()
    monkeypatch.setattr(trigger, "run", run_mock)

    trigger.main()

    run_mock.assert_called_once()
