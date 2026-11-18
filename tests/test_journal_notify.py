import logging
import threading
import time

from hedgebot.journal import COLUMNS, Journal
from hedgebot.notify import Notifier


def test_journal_writes_header_once_and_rows(tmp_path):
    j = Journal(tmp_path / "data" / "journal.csv")
    j.append({"date": "2026-11-16", "app_name": "buy-account", "login": 1001, "status": "opened"})
    j.append({"date": "2026-11-17", "app_name": "buy-account", "login": 1001, "status": "skipped_late",
              "note": "line1\nline2"})
    text = (tmp_path / "data" / "journal.csv").read_text(encoding="utf-8")
    assert text.count("date,app_name") == 1
    assert text.splitlines()[0].split(",") == list(COLUMNS)
    rows = j.rows()
    assert [r["status"] for r in rows] == ["opened", "skipped_late"]
    assert rows[1]["note"] == "line1 line2"


def test_journal_upsert_fills_in_the_close_later(tmp_path):
    j = Journal(tmp_path / "journal.csv")
    j.upsert({"app_name": "sell-account", "position_ticket": 7, "status": "opened"})
    j.upsert({"app_name": "buy-account", "position_ticket": 7, "status": "opened", "note": "monitoring"})
    j.upsert({"app_name": "buy-account", "status": "skipped_late"})  # no ticket -> appended
    j.upsert({"app_name": "buy-account", "position_ticket": 7, "status": "opened", "close_reason": "TP"})
    rows = j.rows()
    assert len(rows) == 3
    assert rows[1]["app_name"] == "buy-account" and rows[1]["close_reason"] == "TP" and rows[1]["note"] == ""
    assert rows[0]["close_reason"] == ""  # the other app's row with the same ticket number is untouched


def test_journal_upsert_never_overwrites_an_earlier_run(tmp_path):
    """Several runs a day; a ticket number that comes back (another account) is still a new row."""
    j = Journal(tmp_path / "journal.csv")
    first = {"app_name": "buy", "position_ticket": 7, "status": "opened", "target_time": "2026-11-18 10:00:00.000 CET"}
    later = {"app_name": "buy", "position_ticket": 7, "status": "opened", "target_time": "2026-11-18 14:29:45.000 CET"}
    j.upsert(dict(first, note="monitoring"))
    j.upsert(dict(first, close_reason="TP"))  # the same run: its row is filled in
    j.upsert(dict(later, note="monitoring"))  # a later run: a row of its own
    j.upsert(dict(later, close_reason="SL"))
    rows = j.rows()
    assert [(r["target_time"][11:19], r["close_reason"]) for r in rows] == [("10:00:00", "TP"), ("14:29:45", "SL")]


def test_journal_concurrent_appends(tmp_path):
    j = Journal(tmp_path / "journal.csv")

    def writer(name):
        for i in range(20):
            j.append({"app_name": name, "note": str(i)})

    threads = [threading.Thread(target=writer, args=(n,)) for n in ("buy", "sell")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(j.rows()) == 40


def test_notifier_disabled_is_a_noop():
    n = Notifier("", "")
    n.send("hello")
    assert n._thread is None and n.flush()


def test_notifier_sends_in_background_and_holds_while_quiet():
    posted = []
    n = Notifier("123:TOKEN", "42", prefix="buy", post=lambda url, json, timeout: posted.append(json) or
                 type("R", (), {"status_code": 200})())
    with n.quiet():
        n.send("armed")
        time.sleep(0.2)
        assert posted == []  # held during the trigger window
    assert n.flush(5)
    assert posted == [{"chat_id": "42", "text": "[buy] armed", "disable_web_page_preview": True}]


def test_notifier_errors_never_raise_and_hide_the_token(caplog):
    def boom(url, json, timeout):
        raise ConnectionError(f"failed to reach {url}")

    n = Notifier("123:SECRET", "42", post=boom)
    with caplog.at_level(logging.WARNING):
        n.send("x")
        assert n.flush(5)
    assert n.failed == 1
    assert "SECRET" not in caplog.text and "***" in caplog.text
