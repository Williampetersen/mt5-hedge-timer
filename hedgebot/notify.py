"""Optional Telegram notifications.

Messages are queued and sent by one background thread with a short HTTP
timeout. send() never blocks and never raises, and quiet() holds the queue
during the trigger window, so notifications can never delay or break a trade.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

import requests

log = logging.getLogger("hedgebot.notify")
API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    def __init__(
        self,
        token: str = "",
        chat_id: str = "",
        prefix: str = "",
        *,
        timeout_s: float = 5.0,
        post: Optional[Callable] = None,
    ):
        self._token = (token or "").strip()
        self._chat_id = str(chat_id or "").strip()
        self.prefix = prefix
        self.timeout_s = timeout_s
        self.enabled = bool(self._token and self._chat_id)
        self._post = post or requests.post
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._open = threading.Event()
        self._open.set()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.sent = 0
        self.failed = 0

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        msg = f"[{self.prefix}] {text}" if self.prefix else text
        self._queue.put(msg[:4000])
        self._ensure_thread()

    @contextmanager
    def quiet(self) -> Iterator[None]:
        """Hold queued messages until the block ends (used around the trigger)."""
        self._open.clear()
        try:
            yield
        finally:
            self._open.set()

    def flush(self, timeout_s: float = 10.0) -> bool:
        """Wait (bounded) for queued messages to go out before the process exits."""
        if not self.enabled or self._thread is None:
            return True
        self._open.set()
        deadline = time.monotonic() + timeout_s
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        return self._queue.unfinished_tasks == 0

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            msg = self._queue.get()
            try:
                self._open.wait()
                self._deliver(msg)
            finally:
                self._queue.task_done()

    def _deliver(self, msg: str) -> None:
        try:
            resp = self._post(
                API_URL.format(token=self._token),
                json={"chat_id": self._chat_id, "text": msg, "disable_web_page_preview": True},
                timeout=self.timeout_s,
            )
            status = getattr(resp, "status_code", 200)
            if status != 200:
                self.failed += 1
                body = self._scrub(str(getattr(resp, "text", ""))[:200])
                log.warning("Telegram returned HTTP %s: %s", status, body)
            else:
                self.sent += 1
        except Exception as exc:  # network down, DNS, timeout...: never propagate
            self.failed += 1
            log.warning("Telegram message not sent: %s", self._scrub(str(exc)))

    def _scrub(self, text: str) -> str:
        """Error texts can contain the request URL, which contains the bot token."""
        return text.replace(self._token, "***") if self._token else text
