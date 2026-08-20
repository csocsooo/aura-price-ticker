"""Regression tests for the AURA price ticker.

Two things are locked in here.

1. Run 11463 (2026-08-19): "[EN] permanent error: The read operation timed
   out" exited 1 and emailed the owner over a blip. A timeout while READING a
   Telegram response is a bare TimeoutError, which is an OSError but not a
   urllib.error.URLError, so the old handler never saw it.

2. The watchdog that pays for that quiet: transient misses stay silent, but
   MAX_SILENT_RUNS of them in a row must fail loudly, or a real outage would
   never reach anyone.

    python -m unittest discover -v -p "test_*.py"
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_prices as up


class _FakeResp:
    """Minimal stand-in for the context manager urlopen returns."""

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def _http_error(status, payload):
    return urllib.error.HTTPError(
        "https://api.telegram.org/", status, "Bad Request", {},
        io.BytesIO(json.dumps(payload).encode("utf-8")))


class ExceptionShapeTest(unittest.TestCase):
    """Documents WHY the old handler missed it, not just that it did."""

    def test_read_timeout_is_oserror_but_not_urlerror(self):
        err = TimeoutError("The read operation timed out")
        self.assertIsInstance(err, OSError)
        self.assertNotIsInstance(err, urllib.error.URLError)

    def test_urlerror_is_still_covered_by_oserror(self):
        self.assertTrue(issubclass(urllib.error.URLError, OSError))


class TelegramCallTest(unittest.TestCase):

    def setUp(self):
        self.sleeps = []
        patcher = mock.patch.object(up.time, "sleep", self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self, side_effect, attempts=4):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            raise side_effect()

        with mock.patch.object(up.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.object(sys, "stderr", io.StringIO()):
            try:
                return up.telegram_call("TOK", "editMessageText", {"chat_id": "@x"},
                                        attempts=attempts), calls
            except Exception as e:
                return e, calls

    def test_read_timeout_becomes_transient_and_retries(self):
        res, calls = self._call(lambda: TimeoutError("The read operation timed out"))
        self.assertIsInstance(res, up.TransientError)
        self.assertEqual(len(calls), 4, "should use every attempt")
        self.assertEqual(len(self.sleeps), 3, "no sleep after the final attempt")

    def test_connection_reset_becomes_transient(self):
        res, _ = self._call(lambda: ConnectionResetError("reset by peer"))
        self.assertIsInstance(res, up.TransientError)

    def test_truncated_body_becomes_transient(self):
        class Truncated(_FakeResp):
            def read(self):
                return b'{"ok": tru'

        with mock.patch.object(up.urllib.request, "urlopen",
                               lambda *a, **k: Truncated(None)), \
             mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(up.TransientError):
                up.telegram_call("TOK", "editMessageText", {}, attempts=2)

    def test_programming_error_is_not_swallowed(self):
        """Positive control: a real bug must not be laundered into a retry."""
        res, calls = self._call(lambda: KeyError("boom"))
        self.assertIsInstance(res, KeyError)
        self.assertEqual(len(calls), 1, "must fail fast, not retry a code bug")

    def test_permanent_4xx_envelope_still_returned(self):
        """Positive control: 'chat not found' must still reach the caller."""
        err = _http_error(400, {"ok": False,
                                "description": "Bad Request: chat not found"})
        with mock.patch.object(up.urllib.request, "urlopen",
                               mock.Mock(side_effect=err)):
            res = up.telegram_call("TOK", "editMessageText", {}, attempts=2)
        self.assertFalse(res["ok"])
        self.assertIn("chat not found", res["description"])


class SilenceCounterTest(unittest.TestCase):
    """A broken watchdog must never be the reason a run fails."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "ticker-health")
        patcher = mock.patch.object(up, "HEALTH_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_raw(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_missing_file_reads_as_healthy(self):
        self.assertEqual(up.read_silent_runs(), 0)

    def test_corrupted_file_reads_as_healthy(self):
        self._write_raw("not a number")
        self.assertEqual(up.read_silent_runs(), 0)

    def test_negative_value_reads_as_healthy(self):
        self._write_raw("-7")
        self.assertEqual(up.read_silent_runs(), 0)

    def test_roundtrip(self):
        up.write_silent_runs(3)
        self.assertEqual(up.read_silent_runs(), 3)


class MainExitCodeTest(unittest.TestCase):
    """End of the chain: what exit code does the workflow actually see?"""

    ENV = {
        "BOT_TOKEN": "tok",
        "CHAT_ID": "@en",
        "MESSAGE_ID": "123",
    }

    MARKET = [(sym, {
        "current_price": 1.0,
        "price_change_percentage_24h": 1.0,
        "price_change_percentage_7d_in_currency": 1.0,
        "sparkline_in_7d": {"price": [1.0, 1.1, 1.2]},
    }) for _, sym in up.COINS]

    TIMEOUT = TimeoutError("The read operation timed out")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.health = os.path.join(self.tmp.name, "ticker-health")
        self.gh_out = os.path.join(self.tmp.name, "gh-output")
        patcher = mock.patch.object(up, "HEALTH_FILE", self.health)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, n):
        with open(self.health, "w", encoding="utf-8") as fh:
            fh.write(str(n))

    def counter(self):
        return up.read_silent_runs()

    def cache_signal(self):
        try:
            with open(self.gh_out, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    @staticmethod
    def _no_market_data():
        raise RuntimeError("CoinGecko down")

    def run_main(self, side_effect=None, market_ok=True):
        if side_effect is None:
            urlopen = lambda *a, **k: _FakeResp(
                {"ok": True, "result": {"message_id": 42}})
        else:
            urlopen = mock.Mock(side_effect=side_effect)
        env = dict(self.ENV, GITHUB_OUTPUT=self.gh_out)
        market = (lambda: self.MARKET) if market_ok else self._no_market_data
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(up, "fetch_market_data", market), \
             mock.patch.object(up, "fetch_global", lambda: None), \
             mock.patch.object(up, "fetch_fng", lambda: None), \
             mock.patch.object(up.time, "sleep", lambda *_: None), \
             mock.patch.object(up.urllib.request, "urlopen", urlopen), \
             mock.patch.object(sys, "stderr", io.StringIO()), \
             mock.patch.object(sys, "stdout", io.StringIO()):
            return up.main()

    # ---- the original regression --------------------------------------

    def test_read_timeout_exits_zero_no_email(self):
        self.assertEqual(self.run_main(self.TIMEOUT), 0,
                         "a blip must not fail the workflow")
        self.assertEqual(self.counter(), 1)

    def test_chat_not_found_still_exits_one(self):
        """Positive control: a real config error must still email the owner."""
        err = _http_error(400, {"ok": False,
                                "description": "Bad Request: chat not found"})
        self.assertEqual(self.run_main(err), 1)

    # ---- the watchdog --------------------------------------------------

    def test_third_silent_run_stays_quiet(self):
        self.seed(2)
        self.assertEqual(self.run_main(self.TIMEOUT), 0)
        self.assertEqual(self.counter(), 3)

    def test_fourth_silent_run_escalates(self):
        self.seed(up.MAX_SILENT_RUNS - 1)
        self.assertEqual(self.run_main(self.TIMEOUT), 1,
                         "an hour of silence is no longer a blip")

    def test_escalation_resets_so_it_emails_hourly_not_every_run(self):
        self.seed(up.MAX_SILENT_RUNS - 1)
        self.run_main(self.TIMEOUT)
        self.assertEqual(self.counter(), 0)

    def test_market_data_outage_counts_as_silence(self):
        self.assertEqual(self.run_main(market_ok=False), 0)
        self.assertEqual(self.counter(), 1,
                         "no market data means the ticker went stale too")

    def test_recovery_resets_the_counter(self):
        self.seed(2)
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.counter(), 0)
        self.assertIn("health_changed=true", self.cache_signal())

    def test_permanent_error_resets_the_counter(self):
        """It emails on its own, so the watchdog has nothing left to add."""
        self.seed(2)
        err = _http_error(400, {"ok": False,
                                "description": "Bad Request: chat not found"})
        self.assertEqual(self.run_main(err), 1)
        self.assertEqual(self.counter(), 0)

    def test_healthy_run_does_not_touch_the_cache(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.cache_signal(), "",
                         "a green run must not create a cache entry")


class RunnerGuardTest(unittest.TestCase):
    """run_tests.py must not report success on a run that tested nothing."""

    @staticmethod
    def _run_guard(suite):
        import run_tests
        patches = [
            mock.patch.object(unittest.TestLoader, "discover",
                              lambda self, *a, **kw: suite),
            mock.patch.object(sys, "stderr", io.StringIO()),
            mock.patch.object(sys, "stdout", io.StringIO()),
        ]
        for p in patches:
            p.start()
        try:
            return run_tests.main()
        finally:
            for p in patches:
                p.stop()

    def test_empty_discovery_fails(self):
        self.assertEqual(self._run_guard(unittest.TestSuite()), 1,
                         "zero discovered tests must not pass CI")

    def test_non_empty_discovery_passes(self):
        """Positive control: the guard must not simply always fail."""

        class Trivial(unittest.TestCase):
            def test_ok(self):
                pass

        suite = unittest.TestLoader().loadTestsFromTestCase(Trivial)
        self.assertEqual(self._run_guard(suite), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
