"""An external review (25.10): 64 record() calls from 8 threads kept 3 samples, and 35 calls died on
FileNotFoundError (read-modify-write with no lock, one shared .tmp name). Red on 0.4.0."""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import limit_forecast as lf  # noqa: E402


class ConcurrentRecords(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIMIT_FORECAST_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("LIMIT_FORECAST_DIR", None)
        self.tmp.cleanup()

    def test_sixty_four_records_from_eight_threads_keep_every_sample(self):
        now, errors, start = time.time(), [], threading.Barrier(8)

        def worker(k):
            start.wait()
            for i in range(8):
                try:
                    lf.record(now + k * 8 + i, {"five_hour": {"used_percentage": 10 + i, "resets_at": 0}})
                except Exception as e:  # noqa: BLE001 — a thread's error must fail the test
                    errors.append(repr(e))

        ts = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(len(lf.load()), 64)


class TornWarnedFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIMIT_FORECAST_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("LIMIT_FORECAST_DIR", None)
        self.tmp.cleanup()

    def test_a_torn_warned_file_does_not_silence_the_warning(self):
        import io
        from contextlib import redirect_stdout
        now = time.time()
        for i in range(12):                                                   # a steep climb to 96%
            lf.record(now - 600 + i * 50, {"five_hour": {"used_percentage": 40 + i * 5, "resets_at": int(now + 9000)}})
        with open(os.path.join(self.tmp.name, "warned.json"), "w", encoding="utf-8") as f:
            f.write('{"five_hour": {"r": 1')                                   # cut mid-write
        out = io.StringIO()
        with redirect_stdout(out):
            lf.hook("{}", now=now)
        self.assertIn("5-hour", out.getvalue())


if __name__ == "__main__":
    unittest.main()
