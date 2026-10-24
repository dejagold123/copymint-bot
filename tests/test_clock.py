import calendar
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot  # noqa: E402
import helpers  # noqa: E402
from test_snipe import CONTRACT, make_bot, node  # noqa: E402

NOW = calendar.timegm((2026, 10, 24, 15, 53, 0))  # 15:53 UTC


class ClockParsing(unittest.TestCase):
    def secs(self, *words):
        found = helpers.find_clock(list(words), NOW)
        return found[0] if found else None

    def test_common_forms(self):
        self.assertEqual(self.secs("5pm", "UTC"), 67 * 60)
        self.assertEqual(self.secs("5", "pm", "utc"), 67 * 60)
        self.assertEqual(self.secs("17:00", "UTC"), 67 * 60)
        self.assertEqual(self.secs("at", "17:00"), 67 * 60)  # no zone means UTC
        self.assertEqual(self.secs("17:30", "WAT"), 37 * 60)  # WAT is UTC+1
        self.assertEqual(self.secs("5pm", "UTC+1"), 7 * 60)
        self.assertEqual(self.secs("5pm", "utc-5"), 67 * 60 + 5 * 3600)
        self.assertEqual(self.secs("17:00:30", "UTC"), 67 * 60 + 30)

    def test_a_time_already_past_means_tomorrow(self):
        self.assertEqual(self.secs("3pm", "UTC"), 24 * 3600 - 53 * 60)
        self.assertEqual(self.secs("9am", "EST"), 22 * 3600 + 7 * 60)

    def test_tomorrow_is_explicit(self):
        self.assertEqual(self.secs("5pm", "UTC", "tomorrow"), 67 * 60 + 86400)
        self.assertEqual(self.secs("tomorrow", "at", "5pm", "UTC"), 67 * 60 + 86400)

    def test_numbers_that_are_not_times(self):
        for words in (["3", "0.00"], ["3"], ["10m"], ["0.5"], ["25:00"], ["13pm"], ["0pm"], ["5:70"], []):
            self.assertIsNone(helpers.find_clock(words, NOW), words)

    def test_consumed_words(self):
        found = helpers.find_clock(["3", "0.00", "5pm", "UTC", "wallet=2"], NOW)
        self.assertEqual((found[2], found[3]), (2, 4))
        self.assertEqual(found[1], "17:00 UTC")

    def test_duration_text(self):
        self.assertEqual(helpers.fmt_duration(3725), "1h2m5s")
        self.assertEqual(helpers.fmt_duration(120), "2m")
        self.assertEqual(helpers.fmt_duration(0), "0s")


class SnipeWithClock(unittest.TestCase):
    def test_the_users_command(self):
        b = node(make_bot())
        with mock.patch.object(bot.time, "time", return_value=NOW):
            text = b.snipe_command([CONTRACT, "3", "0.00", "5pm", "UTC", "wallet=2"])
        self.assertIn("Snipe plan", text)
        self.assertIn("Starts in: 1h7m (at 17:00 UTC)", text)
        plan = b.snipe_plan
        self.assertEqual(plan["qty"], 3)
        self.assertEqual(plan["value"], 0)
        self.assertEqual(plan["wallets"], [b.minters[1].address.lower()])
        self.assertAlmostEqual(plan["fire_at"], NOW + 67 * 60, delta=1)

    def test_a_countdown_still_works(self):
        b = node(make_bot())
        text = b.snipe_command([CONTRACT, "2", "10m"])
        self.assertIn("Starts in: 9m", text)

    def test_too_soon_is_refused(self):
        b = node(make_bot())
        with mock.patch.object(bot.time, "time", return_value=NOW):
            text = b.snipe_command([CONTRACT, "3:53pm", "UTC"][0:1] + ["15:53:05", "UTC"])
        self.assertIn("at least 15 seconds", text)


if __name__ == "__main__":
    unittest.main()
