import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot  # noqa: E402

WATCHED = "0x" + "ab" * 20
CONTRACT = "0x" + "ee" * 20
KEYS = "0x" + "11" * 32 + ",0x" + "22" * 32


def make_bot(**env):
    values = {
        "WATCH_ADDRESSES": WATCHED,
        "MINTER_PRIVATE_KEYS": KEYS,
        "CHAINS": "4663",
        "STATE_PATH": os.path.join(tempfile.mkdtemp(), "state.json"),
    }
    values.update(env)
    with mock.patch.dict(os.environ, values, clear=True):
        return bot.Bot()


def node(b, price=None, code="0x6080"):
    def call(chain_id, method, params):
        if method == "eth_getCode":
            return code
        if method == "eth_call":
            if price is None:
                raise RuntimeError("execution reverted")
            return hex(price)
        return None

    b.rpc = mock.Mock()
    b.rpc.call.side_effect = call
    return b


class Options(unittest.TestCase):
    def test_free_mint_builds_the_common_calls_without_a_price(self):
        b = node(make_bot())
        options = b.manual_options(4663, CONTRACT, 3)
        datas = [o[1] for o in options]
        self.assertIn(bot.selector("mint(uint256)") + f"{3:064x}", datas)
        self.assertIn(bot.selector("publicMint(uint256)") + f"{3:064x}", datas)
        self.assertNotIn("0x", datas)  # no plain ETH send when nothing is paid
        self.assertTrue(all(o[2] == 0 for o in options))
        self.assertNotIn(bot.selector("mint()"), datas)  # only for a quantity of 1

    def test_mint_with_no_arguments_is_tried_for_one(self):
        b = node(make_bot())
        self.assertIn(bot.selector("mint()"), [o[1] for o in b.manual_options(4663, CONTRACT, 1)])

    def test_an_explicit_price_is_multiplied_by_the_quantity_and_adds_a_plain_send(self):
        b = node(make_bot())
        options = b.manual_options(4663, CONTRACT, 4, price_wei=10**15)
        self.assertTrue(all(o[2] == 4 * 10**15 for o in options))
        self.assertIn("0x", [o[1] for o in options])

    def test_the_contracts_price_is_read_when_none_is_given(self):
        b = node(make_bot(), price=10**15)
        self.assertEqual(b.manual_options(4663, CONTRACT, 2)[0][2], 2 * 10**15)

    def test_a_price_over_the_cap_is_ignored(self):
        b = node(make_bot(MAX_MINT_ETH="0.05"), price=10**18)
        self.assertEqual(b.manual_options(4663, CONTRACT, 1)[0][2], 0)

    def test_the_recipient_is_a_watched_placeholder_that_each_minter_swaps_for_itself(self):
        b = node(make_bot())
        data = [o[1] for o in b.manual_options(4663, CONTRACT, 1) if o[1].startswith(bot.selector("mint(address,uint256)"))][0]
        minter = b.minters[0].address
        self.assertIn(WATCHED[2:], data)
        self.assertIn(minter[2:].lower(), b.rewrite_watched(data, minter).lower())


class Command(unittest.TestCase):
    def reply(self, b, args, started=None):
        with mock.patch.object(bot.threading, "Thread") as thread:
            text = b.command_reply("/mint", args)
        if started is not None:
            started.append(thread)
        return text

    def test_it_is_in_the_menu_and_help(self):
        self.assertIn("/mint", make_bot().command_reply("/help"))

    def test_a_valid_command_starts_the_mint_and_answers_at_once(self):
        b = node(make_bot())
        started = []
        text = self.reply(b, [CONTRACT, "2"], started)
        self.assertIn("Minting 2", text)
        self.assertIn("2 minter(s)", text)
        started[0].assert_called_once()
        self.assertEqual(started[0].call_args.kwargs["args"], (4663, CONTRACT, 2, None))

    def test_the_chain_id_can_come_first(self):
        b = node(make_bot(CHAINS="1,4663"))
        started = []
        self.reply(b, ["4663", CONTRACT], started)
        self.assertEqual(started[0].call_args.kwargs["args"], (4663, CONTRACT, 1, None))

    def test_with_several_chains_the_chain_is_required(self):
        b = node(make_bot(CHAINS="1,4663"))
        self.assertIn("chain=", self.reply(b, [CONTRACT]))

    def test_bad_input_is_refused_without_starting_anything(self):
        b = node(make_bot())
        for args in ([], ["0x123"], [CONTRACT, "x"], [CONTRACT, "0"], [CONTRACT, "9999"], [CONTRACT, "1", "abc"], [CONTRACT, "1", "-1"], ["999", CONTRACT]):
            started = []
            self.reply(b, args, started)
            started[0].assert_not_called()

    def test_an_address_with_no_contract_is_refused(self):
        b = node(make_bot(), code="0x")
        started = []
        self.assertIn("no contract", self.reply(b, [CONTRACT], started))
        started[0].assert_not_called()

    def test_a_price_is_passed_on_in_wei(self):
        b = node(make_bot())
        started = []
        self.reply(b, [CONTRACT, "2", "0.001"], started)
        self.assertEqual(started[0].call_args.kwargs["args"], (4663, CONTRACT, 2, 10**15))


class Running(unittest.TestCase):
    def test_the_mint_goes_through_the_normal_copy_path_with_a_manual_source(self):
        b = node(make_bot())
        b.copy = mock.Mock(return_value=2)
        b.run_manual_mint(4663, CONTRACT, 2, None)
        chain_id, tx, want = b.copy.call_args[0]
        self.assertEqual((chain_id, tx["from"], tx["to"], want), (4663, "manual", CONTRACT, {CONTRACT: 2}))
        self.assertTrue(b.copy.call_args.kwargs["options"])

    def test_a_failure_is_reported_not_raised(self):
        b = node(make_bot())
        b.copy = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.object(bot, "notify") as notify, mock.patch.object(bot, "log"):
            b.run_manual_mint(4663, CONTRACT, 1, None)
        self.assertIn("/mint failed", notify.call_args[0][0])

    def test_the_calls_are_saved_for_retries(self):
        b = node(make_bot())
        options = [(CONTRACT, "0x1234abcd", 0)]
        b.pick = lambda chain_id, acct, opts, want: None
        b.last_pick_kind = "reverted"
        tx = {"hash": "manual-1", "from": "manual", "to": CONTRACT, "value": "0x0", "input": "0x"}
        seen_options = {}
        original = b.pick

        def pick(chain_id, acct, opts, want):
            seen_options["opts"] = opts
            return None

        b.pick = pick
        with mock.patch.object(bot, "notify"), mock.patch.object(bot, "log"):
            b.copy(4663, tx, {CONTRACT: 1}, options=options)
        self.assertEqual(seen_options["opts"], options)


KEYS3 = "0x" + "11" * 32 + ",0x" + "22" * 32 + ",0x" + "33" * 32


class WalletChoice(unittest.TestCase):
    def three(self, **env):
        return node(make_bot(MINTER_PRIVATE_KEYS=KEYS3, **env))

    def start(self, b, args):
        with mock.patch.object(bot.threading, "Thread") as thread:
            text = b.command_reply("/mint", args)
        return text, thread

    def test_wallets_pick_the_minters_and_are_passed_on(self):
        b = self.three()
        text, thread = self.start(b, [CONTRACT, "2", "wallets=1,3"])
        self.assertIn("minter #1, #3", text)
        self.assertEqual(
            thread.call_args.kwargs["args"],
            (4663, CONTRACT, 2, None, [b.minters[0].address, b.minters[2].address]),
        )

    def test_all_or_nothing_passes_no_subset(self):
        b = self.three()
        for args in ([CONTRACT], [CONTRACT, "wallets=all"], [CONTRACT, "wallets=1,2,3"]):
            text, thread = self.start(b, args)
            self.assertIn("3 minter(s)", text)
            self.assertEqual(thread.call_args.kwargs["args"], (4663, CONTRACT, 1, None))

    def test_wallets_can_sit_anywhere_and_take_a_space_after_the_comma(self):
        b = self.three()
        _text, thread = self.start(b, ["wallets=2,", "3", CONTRACT, "1"])
        self.assertEqual(len(thread.call_args.kwargs["args"][4]), 2)

    def test_bad_wallets_start_nothing(self):
        b = self.three()
        for spec in ("wallets=4", "wallets=0", "wallets=abc", "wallets="):
            text, thread = self.start(b, [CONTRACT, spec])
            self.assertIn("⚠️", text, spec)
            thread.assert_not_called()

    def test_chain_by_name_and_detection_by_where_the_contract_is(self):
        b = node(make_bot(CHAINS="1,4663"))
        _text, thread = self.start(b, [CONTRACT, "chain=robinhood"])
        self.assertEqual(thread.call_args.kwargs["args"][0], 4663)
        original = b.rpc.call.side_effect

        def call(chain_id, method, params):
            if method == "eth_getCode" and chain_id == 1:
                return "0x"
            return original(chain_id, method, params)

        b.rpc.call.side_effect = call
        _text, thread = self.start(b, [CONTRACT])
        self.assertEqual(thread.call_args.kwargs["args"][0], 4663)

    def test_only_the_chosen_minters_are_tried_and_counted(self):
        b = self.three()
        tried = []

        def pick(chain_id, acct, options, want):
            tried.append(acct.address.lower())
            return (0, CONTRACT, "0x1234abcd", 100_000)

        b.pick = pick
        b.fees = lambda chain_id: (1, 1)
        b.ensure = lambda acct, chain_id, need: True
        b.land = lambda *a, **k: ("0xmine", True, ["x"])
        tx = {"hash": "manual-9", "from": "manual", "to": CONTRACT, "value": "0x0", "input": "0x"}
        with mock.patch.object(bot, "notify") as notify, mock.patch.object(bot, "log"):
            b.copy(4663, tx, {CONTRACT: 1}, options=[(CONTRACT, "0x1234abcd", 0)], wallets=[b.minters[1].address])
        self.assertEqual(tried, [b.minters[1].address.lower()])
        self.assertIn("1 of 1", notify.call_args[0][0])
        self.assertEqual(b.pending, {})


if __name__ == "__main__":
    unittest.main()
