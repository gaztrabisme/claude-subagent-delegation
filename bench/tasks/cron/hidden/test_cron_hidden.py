import time
import unittest
from datetime import datetime as dt

from cron import matches, next_run, next_runs, parse


class NextRun(unittest.TestCase):
    def test_every_minute_is_strictly_after(self):
        self.assertEqual(next_run("* * * * *", dt(2026, 1, 1, 10, 0, 30)), dt(2026, 1, 1, 10, 1))
        self.assertEqual(next_run("* * * * *", dt(2026, 1, 1, 10, 0)), dt(2026, 1, 1, 10, 1))

    def test_fixed_time(self):
        self.assertEqual(next_run("30 9 * * *", dt(2026, 3, 10, 9, 30)), dt(2026, 3, 11, 9, 30))
        self.assertEqual(next_run("30 9 * * *", dt(2026, 3, 10, 9, 29, 59)), dt(2026, 3, 10, 9, 30))

    def test_steps_and_ranges(self):
        self.assertEqual(next_run("*/15 * * * *", dt(2026, 1, 1, 10, 7)), dt(2026, 1, 1, 10, 15))
        self.assertEqual(next_run("0 9-17/4 * * *", dt(2026, 1, 1, 13, 0)), dt(2026, 1, 1, 17, 0))
        self.assertEqual(next_run("0 9-17/4 * * *", dt(2026, 1, 1, 17, 0)), dt(2026, 1, 2, 9, 0))
        self.assertEqual(next_run("10-30/10 * * * *", dt(2026, 1, 1, 10, 30)), dt(2026, 1, 1, 11, 10))

    def test_value_with_step(self):
        self.assertEqual(next_run("5/20 * * * *", dt(2026, 1, 1, 10, 26)), dt(2026, 1, 1, 10, 45))
        self.assertEqual(next_run("5/20 * * * *", dt(2026, 1, 1, 10, 45)), dt(2026, 1, 1, 11, 5))

    def test_names_lists_and_case(self):
        expr = "0 12 * JAN,jul MON-FRI"
        self.assertEqual(next_run(expr, dt(2026, 1, 1)), dt(2026, 1, 1, 12, 0))
        self.assertEqual(next_run(expr, dt(2026, 1, 30, 12, 0)), dt(2026, 7, 1, 12, 0))
        self.assertEqual(next_run("  0  12  *  jan  mon ", dt(2026, 1, 1)), dt(2026, 1, 5, 12, 0))

    def test_month_range(self):
        self.assertEqual(next_runs("0 0 1 JAN-MAR *", dt(2026, 1, 1), 3),
                         [dt(2026, 2, 1), dt(2026, 3, 1), dt(2027, 1, 1)])

    def test_sunday_is_0_and_7(self):
        sunday = dt(2026, 1, 4)
        self.assertTrue(matches("0 0 * * 0", sunday))
        self.assertTrue(matches("0 0 * * 7", sunday))
        self.assertTrue(matches("0 0 * * SUN", sunday))
        self.assertEqual(next_runs("0 0 * * 5-7", dt(2026, 1, 1), 4),
                         [dt(2026, 1, 2), dt(2026, 1, 3), dt(2026, 1, 4), dt(2026, 1, 9)])

    def test_day_of_month_or_day_of_week(self):
        self.assertEqual(next_runs("0 0 13 * FRI", dt(2026, 1, 1), 4),
                         [dt(2026, 1, 2), dt(2026, 1, 9), dt(2026, 1, 13), dt(2026, 1, 16)])

    def test_only_one_day_field_restricted(self):
        self.assertEqual(next_runs("0 0 13 * *", dt(2026, 1, 1), 2), [dt(2026, 1, 13), dt(2026, 2, 13)])
        self.assertEqual(next_runs("0 0 * * FRI", dt(2026, 1, 1), 2), [dt(2026, 1, 2), dt(2026, 1, 9)])

    def test_star_with_step_counts_as_restricted(self):
        self.assertEqual(next_runs("0 0 15 * */1", dt(2026, 1, 1), 2), [dt(2026, 1, 2), dt(2026, 1, 3)])

    def test_31st_skips_short_months(self):
        self.assertEqual(next_runs("0 0 31 * *", dt(2026, 1, 1), 3),
                         [dt(2026, 1, 31), dt(2026, 3, 31), dt(2026, 5, 31)])

    def test_year_rollover(self):
        self.assertEqual(next_run("59 23 31 12 *", dt(2026, 12, 31, 23, 59)), dt(2027, 12, 31, 23, 59))

    def test_leap_day_is_fast(self):
        started = time.perf_counter()
        self.assertEqual(next_run("0 0 29 2 *", dt(2026, 3, 1)), dt(2028, 2, 29))
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_never_matches_is_none_and_fast(self):
        started = time.perf_counter()
        self.assertIsNone(next_run("0 0 30 2 *", dt(2026, 1, 1)))
        self.assertIsNone(next_run("0 0 31 4,6,9,11 *", dt(2026, 1, 1)))
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_macros(self):
        self.assertEqual(next_run("@hourly", dt(2026, 1, 1, 10, 0)), dt(2026, 1, 1, 11, 0))
        self.assertEqual(next_run("@daily", dt(2026, 1, 1, 10, 0)), dt(2026, 1, 2))
        self.assertEqual(next_run("@midnight", dt(2026, 1, 1, 10, 0)), dt(2026, 1, 2))
        self.assertEqual(next_run("@weekly", dt(2026, 1, 1)), dt(2026, 1, 4))
        self.assertEqual(next_run("@monthly", dt(2026, 1, 1)), dt(2026, 2, 1))
        self.assertEqual(next_run("@yearly", dt(2026, 1, 1)), dt(2027, 1, 1))
        self.assertEqual(next_run("@annually", dt(2026, 1, 1)), dt(2027, 1, 1))


class NextRuns(unittest.TestCase):
    def test_chained(self):
        self.assertEqual(next_runs("*/30 * * * *", dt(2026, 1, 1), 3),
                         [dt(2026, 1, 1, 0, 30), dt(2026, 1, 1, 1, 0), dt(2026, 1, 1, 1, 30)])

    def test_stops_when_no_match(self):
        self.assertEqual(next_runs("0 0 30 2 *", dt(2026, 1, 1), 3), [])

    def test_accepts_parsed_expression(self):
        expr = parse("*/5 * * * *")
        self.assertEqual(next_run(expr, dt(2026, 1, 1, 0, 1)), dt(2026, 1, 1, 0, 5))
        self.assertTrue(matches(expr, dt(2026, 1, 1, 0, 10)))


class Matches(unittest.TestCase):
    def test_seconds_ignored(self):
        self.assertTrue(matches("30 9 * * *", dt(2026, 3, 10, 9, 30, 45, 123)))

    def test_ranges(self):
        self.assertTrue(matches("0-10 * * * *", dt(2026, 1, 1, 10, 10)))
        self.assertFalse(matches("0-10 * * * *", dt(2026, 1, 1, 10, 11)))


class Invalid(unittest.TestCase):
    def test_invalid_expressions(self):
        for expr in ["", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * 32 * *",
                     "* * * 13 *", "* * * 0 *", "* * * * 8", "5-1 * * * *", "*/0 * * * *", "*/x * * * *",
                     "1,,2 * * * *", ",1 * * * *", "a * * * *", "@reboot", "* * * FOO *", "* * * * MON-",
                     "1-2-3 * * * *", "* * * * 6-1", "-1 * * * *"]:
            with self.subTest(expr=expr):
                with self.assertRaises(ValueError):
                    parse(expr)

    def test_valid_edge_values(self):
        for expr in ["59 23 31 12 7", "0 0 1 1 0", "* * * * 5-7", "0 0 * DEC SAT", "@annually"]:
            with self.subTest(expr=expr):
                parse(expr)


if __name__ == "__main__":
    unittest.main()
