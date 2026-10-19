import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot  # noqa: E402
import opensea  # noqa: E402
from test_buy import ETH, NFT, SEAPORT, FakeOpenSea, make_bot, transfer_log  # noqa: E402

COLLECTION = "https://opensea.io/collection/robo-cards"


def row(n, price, **extra):
    base = {"hash": f"0xh{n}", "protocol_address": SEAPORT, "price_wei": price, "native": True, "quantity": 1, "token_id": n, "contract": NFT}
    base.update(extra)
    return base


class SweepOpenSea(FakeOpenSea):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def collection_listings(self, collection, limit=50):
        return list(self.rows)

    def fulfilment(self, lst, chain, buyer):
        self.calls.append((buyer, lst["hash"]))
        return SEAPORT, lst["price_wei"], "0xdeadbeef", "fulfillBasicOrder_efficient_6GL6yc"


class SweepCommandTests(unittest.TestCase):
    def test_parse_collection_link(self):
        self.assertEqual(opensea.parse_collection_link("https://opensea.io/collection/Robo-Cards/overview"), "robo-cards")
        self.assertIsNone(opensea.parse_collection_link("nothing"))

    def test_validation(self):
        b = make_bot()
        b.opensea = SweepOpenSea([])
        self.assertIn("Usage", b.sweep_command([]))
        self.assertIn("need max=", b.sweep_command([COLLECTION]))
        self.assertIn("above your limit", b.sweep_command([COLLECTION, "max=5"]))
        self.assertIn("could not read", b.sweep_command([COLLECTION, "max=x"]))
        self.assertIn("below max", b.sweep_command([COLLECTION, "max=0.01", "budget=0.001"]))
        self.assertIn("collection link", b.sweep_command(["hello", "max=0.01"]))
        self.assertIn("OPENSEA_API_KEY", make_bot(OPENSEA_API_KEY="").sweep_command([COLLECTION, "max=0.01"]))

    def test_preview_defaults_to_the_only_chain(self):
        b = make_bot()
        b.opensea = SweepOpenSea([row(1, 10**15), row(2, 10**16)])
        with mock.patch.object(b, "balance", return_value=ETH):
            text = b.sweep_command([COLLECTION, "max=0.002", "budget=0.01", "wallets=1"])
        self.assertIn("1 of the 2", text)
        self.assertIn("/sweep confirm", text)
        self.assertEqual(b.sweep_plan["chain"], 4663)
        self.assertEqual(b.sweep_plan["budget"], 10**16)

    def test_stop_and_confirm_without_plan(self):
        b = make_bot()
        self.assertIn("No sweep", b.sweep_command(["stop"]))
        self.assertIn("no sweep waiting", b.sweep_command(["confirm"]))
        b.sweep_plan = {"state": "planned"}
        self.assertIn("Cancelled", b.sweep_command(["stop"]))
        self.assertIsNone(b.sweep_plan)

    def test_item_link_gives_chain(self):
        b = make_bot()
        b.opensea = SweepOpenSea([])
        with mock.patch.object(b, "balance", return_value=ETH):
            b.sweep_command([f"https://opensea.io/item/robinhood/{NFT}/7", "max=0.01"])
        self.assertEqual(b.sweep_plan["collection"], "robo-cards")


class SweepRunTests(unittest.TestCase):
    def setup_run(self, rows, budget=None, dry=False):
        b = make_bot(DRY_RUN="true" if dry else "false")
        b.opensea = SweepOpenSea(rows)
        b.sweep_poll = 0.01
        with mock.patch.object(b, "balance", return_value=ETH):
            b.sweep_command([COLLECTION, "max=0.002"] + ([f"budget={budget}"] if budget else []))
        plan = b.sweep_plan
        plan["state"] = "running"
        return b, plan

    def go(self, b, plan, balance=ETH, stop_after=0.3):
        sent = []

        def simulate(chain, acct, to, data, value):
            token = int(b.opensea.calls[-1][1][3:])
            return [transfer_log(NFT, acct.address, token)], 200_000

        timer = threading.Timer(stop_after, b.sweep_stop.set)
        timer.start()
        with mock.patch.object(bot, "alert", side_effect=lambda msg, rich=None: sent.append((msg, rich))), \
             mock.patch.object(b, "balance", return_value=balance), \
             mock.patch.object(b, "fees", return_value=(10**9, 10**9)), \
             mock.patch.object(b, "simulate", side_effect=simulate), \
             mock.patch.object(b, "land", return_value=("0xtx", True, ["0xtx"])) as landed, \
             mock.patch.object(b, "record_holdings_later"):
            b.run_sweep(plan)
        timer.cancel()
        return sent, landed

    def test_buys_cheapest_under_price_once_and_rotates(self):
        b, plan = self.setup_run([row(1, 10**15), row(2, 5 * 10**14), row(3, 10**16)])
        sent, landed = self.go(b, plan)
        self.assertEqual(landed.call_count, 2)
        first, second = landed.call_args_list[0].args[1], landed.call_args_list[1].args[1]
        self.assertEqual(first.address, b.minters[0].address)
        self.assertEqual(second.address, b.minters[1].address)
        self.assertEqual([c[1] for c in b.opensea.calls], ["0xh2", "0xh1"])
        self.assertEqual(plan["bought"], 2)
        self.assertEqual(plan["spent"], 15 * 10**14)
        self.assertTrue(sent[-1][0].startswith("Sweep ended"))
        self.assertIsNone(b.sweep_plan)

    def test_budget_stops_before_overspend(self):
        b, plan = self.setup_run([row(1, 10**15), row(2, 10**15), row(3, 10**15)], budget="0.0025")
        sent, landed = self.go(b, plan)
        self.assertEqual(landed.call_count, 2)
        self.assertIn("budget reached", sent[-1][0])

    def test_dry_run_counts_but_sends_nothing(self):
        b, plan = self.setup_run([row(1, 10**15)], dry=True)
        sent, landed = self.go(b, plan)
        landed.assert_not_called()
        self.assertEqual(plan["bought"], 1)
        self.assertEqual(plan["spent"], 0)
        self.assertIn("dry run", sent[0][0])

    def test_short_funds_retry_later_not_every_poll(self):
        b, plan = self.setup_run([row(1, 10**15)])
        sent, landed = self.go(b, plan, balance=10)
        landed.assert_not_called()
        self.assertEqual(len(b.opensea.calls), 2)  # one try per wallet, then waits a minute
        self.assertIn("1", [str(k[3:]) for k in plan["later"]])

    def test_opensea_failures_end_the_sweep(self):
        b, plan = self.setup_run([])
        b.sweep_poll = 0.0005

        def boom(collection, limit=50):
            raise opensea.OpenSeaError("down", 500)

        b.opensea.collection_listings = boom
        sent = []
        with mock.patch.object(bot, "alert", side_effect=lambda msg, rich=None: sent.append(msg)), \
             mock.patch.object(threading.Event, "wait", return_value=False):
            b.run_sweep(plan)
        self.assertIn("kept failing", sent[-1])


if __name__ == "__main__":
    unittest.main()
