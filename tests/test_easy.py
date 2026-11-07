import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot  # noqa: E402
from test_buy import ETH, LINK, NFT, FakeOpenSea, make_bot, transfer_log  # noqa: E402


def maker(**env):
    b = make_bot(**env)
    b.settings.clear()
    b.opensea = FakeOpenSea()
    b.has_code = lambda chain, contract: True
    return b


class PlainWords(unittest.TestCase):
    def test_buy_words_become_a_link_and_options(self):
        b = maker()
        args, delay, when, error = b.easy_args([NFT, "7", "under", "0.01", "wallet", "2"], "buy")
        self.assertIsNone(error)
        self.assertIsNone(delay)
        self.assertEqual(args, [LINK, "max=0.01", "wallets=2"])

    def test_wallet_lists_and_chain_words(self):
        b = maker()
        args, *_ = b.easy_args([NFT, "7", "wallets", "1", "and", "2", "on", "robinhood"], "buy")
        self.assertIn("wallets=1,2", args)
        self.assertEqual(args[0], LINK)  # the chain word went into the link

    def test_clock_time_is_pulled_out(self):
        b = maker()
        args, delay, when, error = b.easy_args([NFT, "7", "at", "5pm", "UTC"], "buy")
        self.assertEqual(args, [LINK])
        self.assertTrue(0 < delay <= 86400)
        self.assertIn("17:00 UTC", when)

    def test_countdown(self):
        b = maker()
        args, delay, when, _ = b.easy_args([NFT, "7", "in", "10m"], "buy")
        self.assertEqual((args, delay), ([LINK], 600))

    def test_missing_token_id_is_explained(self):
        b = maker()
        _args, _d, _w, error = b.easy_args([NFT], "buy")
        self.assertIn("token ID", error)

    def test_list_price_words(self):
        b = maker()
        args, *_ = b.easy_args([NFT, "7", "for", "2x", "wallet", "1"], "list")
        self.assertEqual(args, [LINK, "2x", "wallets=1"])

    def test_sweep_contract_becomes_a_collection_link(self):
        b = maker()
        b.opensea.contract_collection = lambda chain, contract: ("robo-cards", "Robo")
        args, *_ = b.easy_args([NFT, "under", "0.002", "budget", "0.5"], "sweep")
        self.assertEqual(args[0], "https://opensea.io/collection/robo-cards")
        self.assertIn("max=0.002", args)
        self.assertIn("budget=0.5", args)
        self.assertIn("chain=4663", args)

    def test_old_forms_still_work(self):
        b = maker()
        args, *_ = b.easy_args([LINK, "wallets=2", "max=0.01"], "buy")
        self.assertEqual(args, [LINK, "wallets=2", "max=0.01"])

    def test_snipe_keeps_its_clock_words(self):
        b = maker()
        args, delay, *_ = b.easy_args([NFT, "3", "0.00", "5pm", "UTC", "wallet", "2"], "snipe")
        self.assertIsNone(delay)
        self.assertEqual(args, [NFT, "3", "0.00", "5pm", "UTC", "wallets=2"])


class StartAtOnce(unittest.TestCase):
    def test_buy_starts_without_confirm_by_default(self):
        b = maker(DRY_RUN="true")
        with mock.patch.object(b, "balance", return_value=ETH), mock.patch.object(bot.threading, "Thread") as thread:
            text = b.buy_command([NFT, "7", "under", "0.01", "wallet", "2"])
        self.assertIn("Buying", text)
        self.assertNotIn("/buy confirm", text)
        self.assertEqual(b.buy_plan["state"], "running")
        self.assertTrue(thread.called)

    def test_setting_brings_the_confirm_step_back(self):
        b = maker(DRY_RUN="true")
        b.settings["confirm_trades"] = True
        with mock.patch.object(b, "balance", return_value=ETH):
            text = b.buy_command([NFT, "7"])
        self.assertIn("/buy confirm", text)
        self.assertEqual(b.buy_plan["state"], "planned")

    def test_a_broke_wallet_stops_the_automatic_start(self):
        b = maker()
        with mock.patch.object(b, "balance", return_value=0), mock.patch.object(bot.threading, "Thread") as thread:
            text = b.buy_command([NFT, "7"])
        self.assertIn("Not started", text)
        self.assertIn("needs about", text)
        self.assertFalse(thread.called)

    def test_scheduled_buy_waits_then_buys_a_fresh_listing(self):
        b = maker(DRY_RUN="true")
        with mock.patch.object(b, "balance", return_value=ETH), mock.patch.object(bot.threading, "Thread") as thread:
            text = b.buy_command([NFT, "7", "wallet", "2", "in", "1h"])
        self.assertIn("Starts: in 1h", text)
        plan = b.buy_plan
        self.assertTrue(plan["start_at"] > time.time() + 3000)
        plan["start_at"] = time.time() - 1  # the moment has come
        sent = []
        with mock.patch.object(bot, "alert", side_effect=lambda msg, rich=None: sent.append(msg)), \
             mock.patch.object(b, "balance", return_value=ETH), \
             mock.patch.object(b, "fees", return_value=(10**9, 10**9)), \
             mock.patch.object(b, "simulate", return_value=([transfer_log(NFT, b.minters[1].address, 7)], 200_000)):
            b.run_buy(plan)
        self.assertTrue(sent[0].startswith("Would buy"))

    def test_bulk_commands_still_ask_by_default(self):
        b = maker()
        self.assertTrue(b.setting("confirm_bulk"))
        self.assertFalse(b.setting("confirm_trades"))
        text = b.finish_plan("sellall", "plan", lambda: "started", bulk=True)
        self.assertIn("/sellall confirm", text)
        self.assertEqual(b.finish_plan("buy", "plan", lambda: "started"), "plan\n\nstarted")


if __name__ == "__main__":
    unittest.main()


class BridgePathRegression(unittest.TestCase):
    def test_a_wallet_short_of_gas_reaches_the_bridge_check_without_crashing(self):
        # `self.holdings` is the dict of minted NFTs; the bridge's balance reader must not share that name.
        b = maker()
        self.assertIsInstance(b.holdings, dict)
        with mock.patch.object(b, "balance", return_value=0), mock.patch.object(b, "token_balances", return_value=[]):
            self.assertFalse(b.ensure(b.minters[0], 4663, 10**18))


class HistoryLog(unittest.TestCase):
    def test_history_lists_upcoming_and_past_and_survives_a_restart(self):
        b = maker()
        self.assertIn("Nothing is scheduled", b.history_command([]))
        b.hist_add("snipe", "armed", 4663, NFT, "FCFS ×1")
        b.hist_add("snipe", "done", 4663, NFT, "1 of 4 minted")
        text = b.history_command([])
        self.assertLess(text.index("done"), text.index("armed"))  # newest first
        self.assertIn("1 of 4 minted", text)
        b.snipe_plan = {"state": "armed", "chain": 4663, "contract": NFT, "signature": "FCFS", "qty": 1, "fire_at": time.time() + 600}
        self.assertIn("Armed snipe", b.history_command([]))
        self.assertIn("History", b.command_reply("/history", []))
        with mock.patch.dict(os.environ, {"WATCH_ADDRESSES": "0x" + "ab" * 20, "MINTER_PRIVATE_KEYS": "0x" + "11" * 32, "CHAINS": "4663", "STATE_PATH": str(b.state_path)}, clear=True):
            again = bot.Bot()
        self.assertEqual(len(again.history), 2)
