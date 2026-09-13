"""Offline tests: config, grid, history and report logic. No network calls.

    python -m unittest discover tests
"""
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import flight_watch as fw  # noqa: E402

BASE = """
[destination]
code = "lhr"
city = "London"

[[origins]]
code = "JFK"
city = "New York"
alert = 450

[dates]
outbound_from = "2027-03-01"
outbound_to   = "2027-03-03"
return_from   = "2027-03-15"
return_to     = "2027-03-16"
"""


class Tmp(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def cfg(self, text=BASE):
        p = self.dir / "config.toml"
        p.write_text(textwrap.dedent(text))
        return fw.load_config(p)


class ConfigTests(Tmp):
    def test_round_trip_grid(self):
        c = self.cfg()
        self.assertEqual(c.dest_code, "LHR")
        self.assertFalse(c.one_way)
        self.assertEqual(len(c.combos), 3 * 2)
        self.assertEqual(c.combos[0], ("2027-03-01", "2027-03-15"))

    def test_one_way_when_no_return(self):
        c = self.cfg(BASE.replace('return_from   = "2027-03-15"\n', "")
                         .replace('return_to     = "2027-03-16"\n', ""))
        self.assertTrue(c.one_way)
        self.assertEqual(c.combos, [("2027-03-01", None), ("2027-03-02", None),
                                    ("2027-03-03", None)])

    def test_log_path_relative_to_config(self):
        self.assertEqual(self.cfg().log_path, (self.dir / "flight_watch_log.csv").resolve())

    def test_errors_are_readable(self):
        bad = {
            "missing destination": BASE.replace('code = "lhr"', ""),
            "half a return window": BASE.replace('return_to     = "2027-03-16"', ""),
            "backwards dates": BASE.replace('outbound_to   = "2027-03-03"',
                                            'outbound_to   = "2027-02-01"'),
            "smtp without host": BASE + '\n[notify]\nmethod = "smtp"\nto = "a@b.c"\n',
            "unknown method": BASE + '\n[notify]\nmethod = "pigeon"\n',
        }
        for name, text in bad.items():
            with self.subTest(name), self.assertRaises(fw.ConfigError):
                self.cfg(text)

    def test_missing_file(self):
        with self.assertRaises(fw.ConfigError):
            fw.load_config(self.dir / "nope.toml")


def grid(cfg, prices):
    """Fake scan result: prices keyed by combo index, None for a failed combo."""
    return {cfg.origins[0].code: {c: (prices[i], None) for i, c in enumerate(cfg.combos)}}


class HistoryAndReportTests(Tmp):
    def test_log_round_trip_and_lows(self):
        c = self.cfg()
        fw.append_rows(c, "2027-01-01T09:00:00+00:00", grid(c, [500, 480, None, 490, 510, 520]))
        fw.append_rows(c, "2027-01-02T09:00:00+00:00", grid(c, [470, 490, 495, 500, 505, 515]))
        rows = fw.read_rows(c)
        self.assertEqual(len(rows), 12)
        self.assertEqual(fw.all_time_lows(rows)["JFK"][0], 470)
        runs = fw.run_bests(rows)
        self.assertEqual([runs[t]["JFK"][0] for t in sorted(runs)], [480, 470])

    def test_rows_for_other_routes_are_ignored(self):
        c = self.cfg()
        fw.append_rows(c, "2027-01-01T09:00:00+00:00", {"SFO": {c.combos[0]: (100, None)}})
        self.assertEqual(fw.read_rows(c), [])

    def report(self, c, prev_prices, today_prices):
        if prev_prices:
            fw.append_rows(c, "2027-01-01T09:00:00+00:00", grid(c, prev_prices))
        today, unhealthy = fw.summarize(c, grid(c, today_prices))
        return fw.build_report(c, today, fw.read_rows(c), unhealthy)

    def test_first_run(self):
        subj, body = self.report(self.cfg(), None, [500] * 6)
        self.assertEqual(subj, "✈️ New York → London $500 (Mar 1 → Mar 15)")
        self.assertIn("First run", body)

    def test_new_low_and_target_hit(self):
        subj, body = self.report(self.cfg(), [500] * 6, [500, 500, 440, 500, 500, 500])
        self.assertTrue(subj.endswith("▼ NEW LOW"))
        self.assertIn("(Mar 2 → Mar 15)", subj)
        self.assertIn("TARGET HIT", body)

    def test_flat_and_rise(self):
        c = self.cfg()
        self.assertTrue(self.report(c, [500] * 6, [500] * 6)[0].endswith("= flat"))
        self.assertTrue(self.report(c, None, [530] * 6)[0].endswith("▲ +$30"))

    def test_no_prices_and_thin_scrape(self):
        subj, body = self.report(self.cfg(), None, [None] * 6)
        self.assertIn("no prices", subj)
        self.assertIn("SCRAPE HEALTH", body)

    def test_non_usd_currency(self):
        c = self.cfg(BASE + '\n[search]\ncurrency = "DKK"\n')
        self.assertIn("3200 DKK", self.report(c, None, [3200] * 6)[0])


class DeliveryTests(Tmp):
    SMTP = BASE + """
[notify]
method = "smtp"
to = "me@example.com"
[notify.smtp]
host = "smtp.example.com"
user = "bot@example.com"
"""

    def test_smtp_needs_password(self):
        c = self.cfg(self.SMTP)
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("smtplib.SMTP") as smtp:
            self.assertFalse(fw.deliver(c, "s", "b"))
            smtp.assert_not_called()

    def test_smtp_sends_with_starttls(self):
        c = self.cfg(self.SMTP)
        with mock.patch.dict("os.environ", {fw.SMTP_PASSWORD_ENV: "pw"}), \
                mock.patch("smtplib.SMTP") as smtp:
            self.assertTrue(fw.deliver(c, "✈️ subject", "body"))
        server = smtp.return_value
        smtp.assert_called_once_with("smtp.example.com", 587, timeout=60)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("bot@example.com", "pw")
        msg = server.send_message.call_args[0][0]
        self.assertEqual((msg["To"], msg["Subject"]), ("me@example.com", "✈️ subject"))


if __name__ == "__main__":
    unittest.main()
