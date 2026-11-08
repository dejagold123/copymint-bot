import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot  # noqa: E402
from test_buy import ETH, make_bot  # noqa: E402

DEST = "0x" + "dd" * 20


def collector(balances, **env):
    b = make_bot(**env)
    b.settings.clear()
    b.balance_now = balances
    b.balance = lambda chain, token, who: b.balance_now["v"]
    b.fees = lambda chain: (10**9, 10**9)
    b.estimate_gas = lambda *a, **k: 21_000
    b.sent = []

    def send(chain, acct, to, data, value=0):
        b.sent.append((to, value))
        b.balance_now["v"] -= value + 21_000 * 10**9
        return "0xtx", True

    b.send_native_tx = send
    return b


def run(b, **settings):
    os.environ["COLLECT_TO"] = DEST
    os.environ["COLLECT_WALLET"] = "1"
    b.settings["collect"] = True
    b.settings.update(settings)


class Collect(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("COLLECT_TO", "COLLECT_WALLET")])

    def test_forwards_40_percent_of_a_deposit_once_it_holds(self):
        b = collector({"v": 1 * ETH})
        run(b)
        self.assertEqual(b.collect_cycle(), [])  # first look sets the starting point
        b.balance_now["v"] += ETH  # a deposit of 1
        self.assertEqual(b.collect_cycle(), [])  # still arriving: wait for it to hold
        lines = b.collect_cycle()
        self.assertEqual(len(lines), 1)
        self.assertEqual(b.sent, [(DEST, int(0.4 * ETH))])
        self.assertEqual(b.collect_cycle(), [])  # the 60% that stayed is not forwarded again
        self.assertEqual(b.sent, [(DEST, int(0.4 * ETH))])

    def test_existing_balance_is_never_forwarded(self):
        b = collector({"v": 5 * ETH})
        run(b)
        for _ in range(4):
            b.collect_cycle()
        self.assertEqual(b.sent, [])

    def test_spending_lowers_the_starting_point(self):
        b = collector({"v": 2 * ETH})
        run(b)
        b.collect_cycle()
        b.balance_now["v"] = ETH  # a mint spent 1
        b.collect_cycle()
        b.balance_now["v"] = ETH + ETH // 2  # now 0.5 arrives
        b.collect_cycle()
        b.collect_cycle()
        self.assertEqual(b.sent, [(DEST, int(0.2 * ETH))])

    def test_small_deposits_and_the_minimum(self):
        b = collector({"v": ETH})
        run(b, collect_minimum="0.1")
        b.collect_cycle()
        b.balance_now["v"] += ETH // 20
        b.collect_cycle()
        b.collect_cycle()
        self.assertEqual(b.sent, [])

    def test_gas_is_kept_back_when_the_wallet_is_nearly_empty(self):
        b = collector({"v": 0}, )
        run(b, collect_percent=100.0)
        b.collect_cycle()
        b.balance_now["v"] = 50_000 * 10**9  # barely more than gas
        b.collect_cycle()
        b.collect_cycle()
        to, value = b.sent[0]
        self.assertLessEqual(value + int(21_000 * 10**9 * 1.5), 50_000 * 10**9)

    def test_off_and_bad_setup_do_nothing(self):
        b = collector({"v": ETH})
        self.assertEqual(b.collect_cycle(), [])
        self.assertIn("COLLECT_TO", b.collect_command(["on"]))
        self.assertFalse(b.setting("collect"))

    def test_turning_on_sets_the_starting_point(self):
        b = collector({"v": 3 * ETH})
        os.environ["COLLECT_TO"] = DEST
        os.environ["COLLECT_WALLET"] = "1"
        self.assertIn("is on", b.collect_command(["on"]))
        self.assertEqual(b.collect_base["4663"], 3 * ETH)
        self.assertIn(DEST, b.collect_command([]))


if __name__ == "__main__":
    unittest.main()
