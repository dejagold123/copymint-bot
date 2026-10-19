import os
import sys
import threading
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot  # noqa: E402
import opensea  # noqa: E402
from test_buy import ETH, LINK, NFT, FakeOpenSea, make_bot, transfer_log  # noqa: E402
from test_sweep import SweepOpenSea, row, COLLECTION  # noqa: E402

WATCHED = "0x" + "ab" * 20


def log_entry(topics, address="0x" + "c1" * 20, data="0x"):
    return {"address": address, "topics": topics, "data": data}


class MintDetectionTests(unittest.TestCase):
    def test_erc20_transfer_from_zero_is_not_an_nft_mint(self):
        b = make_bot(WATCH_ADDRESSES=WATCHED)
        zero = "0x" + "0" * 64
        me = "0x" + "0" * 24 + WATCHED[2:]
        erc20 = log_entry([bot.TRANSFER, zero, me], data="0x" + "0" * 63 + "1")
        erc721 = log_entry([bot.TRANSFER, zero, me, hex(5)])
        self.assertEqual(b.minted_map([erc20]), {})
        self.assertEqual(b.minted_map([erc721]), {("0x" + "c1" * 20): 1})


class BlockRetryTests(unittest.TestCase):
    def test_a_passing_error_does_not_skip_the_block(self):
        b = make_bot()
        with mock.patch.object(b, "logs_for", side_effect=RuntimeError("429")):
            self.assertEqual(b.take_range(4663, 100, 100), 99)
        with mock.patch.object(b, "logs_for", return_value=[]):
            self.assertEqual(b.take_range(4663, 100, 100), 100)

    def test_a_block_that_keeps_failing_is_given_up(self):
        b = make_bot()
        with mock.patch.object(b, "logs_for", side_effect=RuntimeError("bad")):
            results = [b.take_range(4663, 100, 100) for _ in range(bot.BLOCK_RETRIES)]
        self.assertEqual(results[:-1], [99] * (bot.BLOCK_RETRIES - 1))
        self.assertEqual(results[-1], 100)

    def test_chunk_stops_at_the_failed_block(self):
        b = make_bot()

        def logs_for(chain, start, end):
            if start <= 102 <= end and (start, end) != (103, 104):
                if start == end == 102 or end - start > 0:
                    raise RuntimeError("429")
            return []

        with mock.patch.object(b, "logs_for", side_effect=logs_for):
            self.assertEqual(b.take_range(4663, 100, 104), 101)


class LateTransactionTests(unittest.TestCase):
    def test_unreadable_tx_is_kept_and_retried(self):
        b = make_bot()
        zero = "0x" + "0" * 64
        me = "0x" + "0" * 24 + WATCHED[2:]
        logs = [log_entry([bot.TRANSFER, zero, me, hex(5)])]
        b.rpc = mock.Mock()
        b.rpc.call.return_value = None
        b.handle_tx(4663, "0xaa", logs)
        self.assertIn("4663:0xaa", b.refetch)
        self.assertNotIn("4663:0xaa", b.seen)
        with mock.patch.object(b, "handle_tx") as handled:
            b.refetch["4663:0xaa"]["next"] = 0
            b.retry_pending()
        handled.assert_called_once()

    def test_unreadable_tx_is_dropped_with_an_alert_after_the_window(self):
        b = make_bot()
        zero = "0x" + "0" * 64
        me = "0x" + "0" * 24 + WATCHED[2:]
        logs = [log_entry([bot.TRANSFER, zero, me, hex(5)])]
        b.rpc = mock.Mock()
        b.rpc.call.return_value = None
        b.handle_tx(4663, "0xaa", logs)
        b.refetch["4663:0xaa"]["until"] = 0
        with mock.patch.object(bot, "alert") as alert:
            b.handle_tx(4663, "0xaa", logs)
        self.assertNotIn("4663:0xaa", b.refetch)
        self.assertIn("could not read", alert.call_args[0][0])


class LandTests(unittest.TestCase):
    def test_land_never_raises_after_broadcast(self):
        b = make_bot()
        acct = b.minters[0]
        b.rpc = mock.Mock()
        b.rpc.call.side_effect = lambda chain, method, params: {"eth_getTransactionCount": "0x1", "eth_blockNumber": "0x10"}.get(method, "0x0")
        with mock.patch.object(b, "fees", return_value=(10**9, 10**9)), \
             mock.patch.object(b, "send", return_value=("0xhash", False)), \
             mock.patch.object(b, "receipt_for", side_effect=RuntimeError("rpc down")), \
             mock.patch.object(bot, "LAND_SECONDS", 0.05), \
             mock.patch.object(bot.time, "sleep"):
            txh, ok, sent = b.land(4663, acct, "0x" + "c1" * 20, "0x", 0, 100_000)
        self.assertEqual((txh, ok, sent), ("0xhash", False, ["0xhash"]))

    def test_unclear_broadcast_keeps_the_local_hash(self):
        b = make_bot()
        acct = b.minters[0]
        calls = []

        def call(chain, method, params):
            calls.append(method)
            if method == "eth_sendRawTransaction":
                raise urllib.error.URLError("timed out")
            return "0x1"

        b.rpc = mock.Mock()
        b.rpc.call.side_effect = call
        with mock.patch.object(b, "balance", return_value=ETH):
            txh, ok = b.send(4663, acct, "0x" + "c1" * 20, "0x", 0, 100_000, 10**9, 10**9, 1, wait=False)
        self.assertTrue(txh.startswith("0x") and len(txh) == 66)
        self.assertFalse(ok)


class MiscFixTests(unittest.TestCase):
    def test_retry_waits_between_attempts(self):
        b = make_bot()
        b.pending["k"] = {"chain": 4663, "hash": "0x1", "from": WATCHED, "to": "0x" + "c1" * 20, "input": "0x", "value": "0", "want": {}, "next_try": 10**12}
        with mock.patch.object(b, "copy") as copy:
            b.retry_pending()
        copy.assert_not_called()

    def test_nan_amount_is_rejected(self):
        b = make_bot()
        for text in ("nan", "inf", "-1", "0", "abc"):
            self.assertIsNone(b.read_amount(text))
        self.assertEqual(b.read_amount("0.5"), 5 * 10**17)
        self.assertIn("could not read", b.buy_command([LINK, "max=nan"]))

    def test_listing_error_is_not_hidden_as_no_listing(self):
        client = opensea.OpenSea("k")

        def request(method, path, params=None, body=None, timeout=20):
            if "/best" in path:
                raise opensea.OpenSeaError("OpenSea 500", 500)
            return {"orders": []}

        client.request = request
        with self.assertRaises(opensea.OpenSeaError):
            client.best_listing("robinhood", "robo", NFT, 7)


class SweepRetryTests(unittest.TestCase):
    def start(self, rows):
        b = make_bot()
        b.opensea = SweepOpenSea(rows)
        b.sweep_poll = 0.01
        with mock.patch.object(b, "balance", return_value=ETH):
            b.sweep_command([COLLECTION, "max=0.002"])
        plan = b.sweep_plan
        plan["state"] = "running"
        return b, plan

    def go(self, b, plan, buy_one):
        timer = threading.Timer(0.3, b.sweep_stop.set)
        timer.start()
        with mock.patch.object(bot, "alert"), mock.patch.object(b, "buy_one", side_effect=buy_one):
            b.run_sweep(plan)
        timer.cancel()

    def test_passing_failure_is_retried_not_dropped(self):
        b, plan = self.start([row(1, 10**15)])
        self.go(b, plan, lambda *a: ("retry", "OpenSea 429", None))
        self.assertNotIn("0xh1", plan["seen"] if plan else set())

    def test_pending_counts_toward_the_budget_and_is_not_retried(self):
        b, plan = self.start([row(1, 10**15)])
        calls = []

        def buy_one(*a):
            calls.append(1)
            return "pending", "sent", "0xtx"

        self.go(b, plan, buy_one)
        self.assertEqual(len(calls), 1)
        self.assertEqual(plan["spent"], 10**15)


if __name__ == "__main__":
    unittest.main()
