"""Session log path helpers."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from common.session_logs import (
    LAUNCHER_BASE,
    STREAM_TT_BASE,
    latest_session_log,
    legacy_log_path,
    new_session_log_path,
    relocate_legacy_log,
    session_stamp,
)


class TestSessionLogs(unittest.TestCase):
    def test_session_stamp_format(self):
        stamp = session_stamp(datetime(2026, 6, 26, 14, 30, 41))
        self.assertEqual(stamp, "2026-06-26_143041")

    def test_new_session_log_path_creates_logs_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            when = datetime(2026, 6, 26, 9, 15, 0)
            path = new_session_log_path(tmp, LAUNCHER_BASE, when=when)
            self.assertEqual(
                path,
                Path(tmp) / "logs" / "launcher_2026-06-26_091500.log",
            )
            self.assertTrue(path.parent.is_dir())

    def test_latest_session_log_prefers_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            logs.mkdir()
            older = logs / "stream_pub_tt_2026-06-26_083000.log"
            newer = logs / "stream_pub_tt_2026-06-26_143000.log"
            older.write_text("old", encoding="utf-8")
            newer.write_text("new", encoding="utf-8")
            time.sleep(0.01)
            os.utime(newer, None)
            resolved = latest_session_log(tmp, STREAM_TT_BASE)
            self.assertEqual(resolved, str(newer))

    def test_relocate_legacy_log_moves_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = legacy_log_path(tmp, STREAM_TT_BASE)
            assert legacy is not None
            legacy.write_text(
                "2026-06-26 14:30:44,054 [TT-STREAM] starting\n",
                encoding="utf-8",
            )
            dest = relocate_legacy_log(tmp, STREAM_TT_BASE)
            self.assertIsNotNone(dest)
            assert dest is not None
            self.assertFalse(legacy.exists())
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.name, "stream_pub_tt_2026-06-26_143044.log")
            resolved = latest_session_log(tmp, STREAM_TT_BASE)
            self.assertEqual(resolved, str(dest))


if __name__ == "__main__":
    unittest.main()
