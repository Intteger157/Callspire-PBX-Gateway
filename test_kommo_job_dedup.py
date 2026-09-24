"""Web + mobile on one extension report the same inbound call with different session ids."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import kommo_jobs_db
from phone_normalize import phones_match_for_dedup

EXT = "52678419"
CALL_TIME = datetime(2026, 9, 24, 13, 32, 20, tzinfo=timezone.utc).isoformat()


class PhonesMatchTest(unittest.TestCase):
    def test_plus_prefix(self) -> None:
        self.assertTrue(phones_match_for_dedup("+447418359435", "447418359435"))

    def test_different_numbers(self) -> None:
        self.assertFalse(phones_match_for_dedup("+79161234567", "+447418359435"))


class WeakerDuplicateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        kommo_jobs_db.init_db(Path(self._tmpdir.name) / "jobs.sqlite")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _job(self, dedup: str, phone: str, answered: bool, session: str) -> dict:
        return kommo_jobs_db.create_job(
            EXT,
            dedup,
            {
                "phone": phone,
                "call_time": CALL_TIME,
                "was_answered": answered,
                "is_incoming": True,
                "session_id": session,
            },
        )

    def test_answered_report_drops_unanswered_from_other_client(self) -> None:
        web = self._job("web", "447418359435", False, "web-uuid")
        mobile = self._job("mob", "+447418359435", True, "mob-1790256741967-447418359435")

        dropped = kommo_jobs_db.fail_weaker_duplicate_jobs(
            EXT, "+447418359435", CALL_TIME, mobile["id"], incoming_was_answered=True
        )

        self.assertEqual(dropped, 1)
        self.assertEqual(kommo_jobs_db.get_job(web["id"])["status"], "failed")
        self.assertEqual(kommo_jobs_db.get_job(mobile["id"])["status"], "queued")

    def test_unanswered_report_keeps_other_jobs(self) -> None:
        self._job("web", "447418359435", False, "web-uuid")
        other = self._job("mob", "447418359435", False, "mob-x")

        dropped = kommo_jobs_db.fail_weaker_duplicate_jobs(
            EXT, "447418359435", CALL_TIME, other["id"], incoming_was_answered=False
        )

        self.assertEqual(dropped, 0)


if __name__ == "__main__":
    unittest.main()
