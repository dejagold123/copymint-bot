import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot  # noqa: E402

WATCHED = "0x" + "ab" * 20
CONTRACT = "0x" + "ee" * 20
KEYS = "0x" + "11" * 32 + ",0x" + "22" * 32
MINT_UINT = bot.selector("mint(uint256)")[2:]


def make_bot(**env):
    values = {
        "WATCH_ADDRESSES": WATCHED,
        "MINTER_PRIVATE_KEYS": KEYS,
        "CHAINS": "4663",
        "STATE_PATH": os.path.join(tempfile.mkdtemp(), "state.json"),
        "SNIPE_RETRY_SECONDS": "0.1",
        "SNIPE_WINDOW_SECONDS": "1",
    }
    values.update(env)
    with mock.patch.dict(os.environ, values, clear=True):
        return bot.Bot()


def node(b, code=None, estimate=True, price=None):
    code = code if code is not None else "0x6080" + "63" + MINT_UINT + "00"
    drop_selectors = (bot.GET_PUBLIC_DROP, bot.GET_ALLOWED_FEE_RECIPIENTS, bot.GET_CREATOR_PAYOUT)
    price_selectors = tuple(bot.selector(sig) for sig in bot.PRICE_VIEWS)

    def call(chain_id, method, params):
        if method == "eth_getCode":
            return code
        if method == "eth_getStorageAt":
            return "0x" + "0" * 64
        if method == "eth_estimateGas":
            if not estimate:
                raise RuntimeError("execution reverted")
            return hex(100_000)
        if method == "eth_call":
            data = ""
            if params and isinstance(params[0], dict):
                data = (params[0].get("data") or "").lower()
            if data.startswith(drop_selectors):
                raise RuntimeError("execution reverted")
            if data.startswith(price_selectors):
                if price is None:
                    raise RuntimeError("execution reverted")
                return hex(price)
            if not estimate:
                raise RuntimeError("execution reverted")
            return "0x"
        if method == "eth_getTransactionCount":
            return "0x5"
        if method in ("eth_gasPrice",):
            return hex(10**9)
        return None

    b.rpc = mock.Mock()
    b.rpc.call.side_effect = call
    b.fees = lambda chain_id: (2 * 10**9, 10**9)
    return b


class Countdown(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(bot.parse_countdown(["10m"]), 600)
        self.assertEqual(bot.parse_countdown(["90s"]), 90)
        self.assertEqual(bot.parse_countdown(["1h30m"]), 5400)
        self.assertEqual(bot.parse_countdown(["in", "10m"]), 600)
        self.assertEqual(bot.parse_countdown(["2H"]), 7200)

    def test_not_a_countdown(self):
        for tokens in ([], ["10"], ["soon"], ["m"], ["0xabc"], [""]):
            self.assertIsNone(bot.parse_countdown(tokens), tokens)


class Planning(unittest.TestCase):
    def test_a_plan_is_built_and_shown_with_the_worst_case(self):
        b = node(make_bot())
        text = b.command_reply("/snipe", ["4663", CONTRACT, "2", "0.001", "10m"])
        self.assertIn("Snipe plan", text)
        self.assertIn("mint(uint256)", text)
        self.assertIn("0.0020 ETH per minter", text)
        self.assertIn("estimated from the contract", text)
        self.assertIn("Worst case", text)
        self.assertIn("/snipe confirm", text)
        self.assertEqual(b.snipe_plan["state"], "planned")

    def test_the_chain_id_is_optional_with_one_chain_and_in_is_optional(self):
        b = node(make_bot())
        self.assertIn("Snipe plan", b.command_reply("/snipe", [CONTRACT, "1", "0", "in", "5m"]))

    def test_gas_falls_back_to_a_default_when_the_mint_is_not_open(self):
        b = node(make_bot(), estimate=False)
        text = b.command_reply("/snipe", [CONTRACT, "2", "0", "10m"])
        self.assertIn("default", text)
        self.assertEqual(b.snipe_plan["gas"], 100_000 + 120_000 * 2)

    def test_auto_price_is_read_from_the_contract(self):
        b = node(make_bot(), price=10**15)
        b.command_reply("/snipe", [CONTRACT, "3", "auto", "10m"])
        self.assertEqual(b.snipe_plan["value"], 3 * 10**15)

    def test_a_price_over_the_cap_is_refused(self):
        b = node(make_bot(MAX_MINT_ETH="0.05"))
        text = b.command_reply("/snipe", [CONTRACT, "1", "1", "10m"])
        self.assertIn("above your cap", text)
        self.assertIsNone(b.snipe_plan)

    def test_a_contract_without_a_mint_function_is_refused(self):
        b = node(make_bot(), code="0x6080")
        self.assertIn("could not find a standard mint function", b.command_reply("/snipe", [CONTRACT, "1", "0", "10m"]))

    def test_a_proxy_is_followed_to_its_implementation(self):
        b = make_bot()
        impl = "0x" + "cd" * 20
        proxy_code = "0x363d3d373d3d3d363d73" + impl[2:] + "5af43d82803e903d91602b57fd5bf3"

        def call(chain_id, method, params):
            if method == "eth_getCode":
                return proxy_code if params[0].lower() == bot.to_checksum_address(CONTRACT).lower() else "0x63" + MINT_UINT
            return "0x"

        b.rpc = mock.Mock()
        b.rpc.call.side_effect = call
        self.assertEqual([s for s, _ in b.mint_functions_in(4663, CONTRACT)], ["mint(uint256)"])

    def test_fn_picks_among_several_functions(self):
        code = "0x" + "63" + MINT_UINT + "63" + bot.selector("publicMint(uint256)")[2:]
        b = node(make_bot(), code=code)
        text = b.command_reply("/snipe", [CONTRACT, "1", "0", "10m", "fn=publicMint(uint256)"])
        self.assertIn("publicMint(uint256)", text.split("Call:")[1].split("\n")[0])
        self.assertIn("not found", b.command_reply("/snipe", [CONTRACT, "1", "0", "10m", "fn=claim(uint256)"]))

    def test_other_functions_are_listed(self):
        code = "0x" + "63" + MINT_UINT + "63" + bot.selector("publicMint(uint256)")[2:]
        b = node(make_bot(), code=code)
        self.assertIn("Other mint functions found: publicMint(uint256)", b.command_reply("/snipe", [CONTRACT, "1", "0", "10m"]))

    def test_bad_input_creates_no_plan(self):
        b = node(make_bot())
        for args in ([CONTRACT, "1", "0", "5s"], ["0x123", "1", "0", "10m"], [CONTRACT, "x", "0", "10m"], [CONTRACT, "0", "0", "10m"],
                     [CONTRACT, "1", "abc", "10m"], [CONTRACT, "1", "-1", "10m"], [CONTRACT, "1", "0"], ["999", CONTRACT, "1", "0", "10m"]):
            b.command_reply("/snipe", args)
            self.assertIsNone(b.snipe_plan, args)

    def test_with_several_chains_the_chain_is_required(self):
        b = node(make_bot(CHAINS="1,4663"))
        self.assertIn("chain=", b.command_reply("/snipe", [CONTRACT, "1", "0", "10m"]))

    def test_it_is_in_the_menu(self):
        self.assertIn("/snipe", make_bot().command_reply("/help"))


class Confirming(unittest.TestCase):
    def planned(self):
        b = node(make_bot())
        b.command_reply("/snipe", [CONTRACT, "1", "0", "10m"])
        return b

    def test_confirm_arms_it_in_the_background(self):
        b = self.planned()
        with mock.patch.object(bot.threading, "Thread") as thread:
            text = b.command_reply("/snipe", ["confirm"])
        self.assertIn("Armed", text)
        thread.return_value.start.assert_called_once()
        self.assertEqual(b.snipe_plan["state"], "armed")

    def test_a_second_plan_cannot_be_made_while_one_is_armed(self):
        b = self.planned()
        with mock.patch.object(bot.threading, "Thread"):
            b.command_reply("/snipe", ["confirm"])
        self.assertIn("already armed", b.command_reply("/snipe", [CONTRACT, "1", "0", "10m"]))

    def test_confirm_without_a_plan(self):
        self.assertIn("no plan", node(make_bot()).command_reply("/snipe", ["confirm"]))

    def test_confirm_too_close_to_the_start_is_refused(self):
        b = self.planned()
        b.snipe_plan["fire_at"] = bot.time.time() + 5
        self.assertIn("Too late", b.command_reply("/snipe", ["confirm"]))
        self.assertIsNone(b.snipe_plan)

    def test_cancel_clears_the_plan_and_stops_a_running_one(self):
        b = self.planned()
        self.assertIn("cancelled", b.command_reply("/snipe", ["cancel"]))
        self.assertIsNone(b.snipe_plan)
        self.assertTrue(b.snipe_cancel.is_set())
        self.assertIn("Nothing", b.command_reply("/snipe", ["cancel"]))

    def test_status_shows_the_plan(self):
        b = self.planned()
        self.assertIn("State: planned", b.command_reply("/snipe"))
        self.assertIn("Usage", node(make_bot()).command_reply("/snipe"))


def sample_plan(b):
    return {
        "chain": 4663, "contract": CONTRACT, "qty": 1, "value": 0, "signature": "mint(uint256)", "layout": "q",
        "gas": 200_000, "gas_note": "default", "fire_at": bot.time.time(), "attempts": 11, "worst": 0, "state": "armed", "others": [],
    }


class Worker(unittest.TestCase):
    def run_worker(self, b, receipts, send=None, window=1.0):
        report = {}
        acct = b.minters[0]
        receipts = list(receipts)
        sent = []

        def call(chain_id, method, params):
            if method == "eth_sendRawTransaction":
                if send:
                    send(params)
                sent.append(params[0])
                return f"0xtx{len(sent)}"
            if method == "eth_getTransactionReceipt":
                return receipts.pop(0) if receipts else None
            if method == "eth_getTransactionCount":
                return "0x9"
            return None

        b.rpc = mock.Mock()
        b.rpc.call.side_effect = call
        plan = sample_plan(b)
        with mock.patch.object(bot, "log"):
            b.snipe_worker(acct, plan, 5, 2 * 10**9, 10**9, None, bot.time.time() + window, report)
        return report[acct.address.lower()], sent

    def test_it_lands_on_the_first_try(self):
        b = make_bot()
        record, sent = self.run_worker(b, [{"status": "0x1"}])
        self.assertEqual(record["status"], "minted")
        self.assertEqual(record["txh"], "0xtx1")
        self.assertEqual(len(sent), 1)
        self.assertTrue(b.minted_before_any(4663, b.minters[0].address, {CONTRACT: 1}))

    def test_a_reverted_try_is_followed_by_one_more_and_never_a_duplicate(self):
        b = make_bot()
        record, sent = self.run_worker(b, [{"status": "0x0"}, {"status": "0x1"}])
        self.assertEqual(record["status"], "minted")
        self.assertEqual(record["failed_txs"], ["0xtx1"])
        self.assertEqual(len(sent), 2)

    def test_it_waits_for_a_pending_try_instead_of_sending_another(self):
        b = make_bot()
        record, sent = self.run_worker(b, [None, None, {"status": "0x1"}])
        self.assertEqual(record["status"], "minted")
        self.assertEqual(len(sent), 1)

    def test_it_stops_at_the_deadline_and_says_why(self):
        b = make_bot()
        record, sent = self.run_worker(b, [{"status": "0x0"}] * 100, window=0.5)
        self.assertEqual(record["status"], "failed")
        self.assertIn("reverted", record["reason"])
        self.assertGreater(len(record["failed_txs"]), 1)
        self.assertLess(len(sent), 12)

    def test_a_try_that_never_confirms_is_reported(self):
        b = make_bot()
        record, sent = self.run_worker(b, [], window=0.4)
        self.assertEqual(record["status"], "failed")
        self.assertIn("had not confirmed", record["reason"])

    def test_a_send_error_is_retried(self):
        b = make_bot()
        calls = []

        def send(params):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("nonce too low")

        record, sent = self.run_worker(b, [{"status": "0x1"}], send=send)
        self.assertEqual(record["status"], "minted")


class Running(unittest.TestCase):
    def test_a_dry_run_sends_nothing_and_still_summarises(self):
        b = node(make_bot(DRY_RUN="true"))
        b.ensure = lambda acct, chain_id, need: True
        plan = sample_plan(b)
        plan["fire_at"] = bot.time.time() + 0.2
        with mock.patch.object(bot, "SNIPE_PREP_SECONDS", 0), mock.patch.object(bot, "notify") as notify, mock.patch.object(bot, "log"):
            b.run_snipe(plan)
        methods = [c[0][1] for c in b.rpc.call.call_args_list]
        self.assertNotIn("eth_sendRawTransaction", methods)
        self.assertIn("Dry run", notify.call_args[0][0])

    def test_cancel_before_the_start_sends_nothing(self):
        b = node(make_bot())
        b.ensure = lambda acct, chain_id, need: True
        plan = sample_plan(b)
        plan["fire_at"] = bot.time.time() + 30
        b.snipe_plan = plan
        threading.Timer(0.3, b.snipe_cancel.set).start()
        with mock.patch.object(bot, "SNIPE_PREP_SECONDS", 0), mock.patch.object(bot, "notify") as notify, mock.patch.object(bot, "log"):
            b.run_snipe(plan)
        self.assertNotIn("eth_sendRawTransaction", [c[0][1] for c in b.rpc.call.call_args_list])
        notify.assert_not_called()

    def test_a_full_run_sends_for_every_minter_and_summarises(self):
        b = node(make_bot())
        b.ensure = lambda acct, chain_id, need: True
        sent = []
        original = b.rpc.call.side_effect

        def call(chain_id, method, params):
            if method == "eth_sendRawTransaction":
                sent.append(params[0])
                return f"0xtx{len(sent)}"
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "gasUsed": hex(50_000), "effectiveGasPrice": hex(10**9)}
            return original(chain_id, method, params)

        b.rpc.call.side_effect = call
        plan = sample_plan(b)
        plan["fire_at"] = bot.time.time() + 0.3
        with mock.patch.object(bot, "SNIPE_PREP_SECONDS", 0), mock.patch.object(bot, "notify") as notify, mock.patch.object(bot, "log"):
            b.run_snipe(plan)
        self.assertEqual(len(sent), 2)
        text = notify.call_args[0][0]
        self.assertIn("🎯 Sniped", text)
        self.assertIn("Minters minted: 2 of 2", text)
        self.assertNotIn("Source wallet", text)

    def test_a_minter_that_cannot_be_funded_is_skipped_and_reported(self):
        b = node(make_bot())
        funded = [b.minters[0].address]
        b.ensure = lambda acct, chain_id, need: acct.address in funded
        sent = []
        original = b.rpc.call.side_effect

        def call(chain_id, method, params):
            if method == "eth_sendRawTransaction":
                sent.append(1)
                return "0xtx"
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "gasUsed": hex(1), "effectiveGasPrice": hex(1)}
            return original(chain_id, method, params)

        b.rpc.call.side_effect = call
        plan = sample_plan(b)
        plan["fire_at"] = bot.time.time() + 0.3
        with mock.patch.object(bot, "SNIPE_PREP_SECONDS", 0), mock.patch.object(bot, "notify") as notify, mock.patch.object(bot, "log"):
            b.run_snipe(plan)
        self.assertEqual(len(sent), 1)
        text = notify.call_args[0][0]
        self.assertIn("Partially sniped", text)
        self.assertIn("not enough funds", text)

    def test_the_plan_is_cleared_when_it_finishes(self):
        b = node(make_bot(DRY_RUN="true"))
        b.ensure = lambda acct, chain_id, need: True
        plan = sample_plan(b)
        plan["fire_at"] = bot.time.time() + 0.2
        b.snipe_plan = plan
        with mock.patch.object(bot, "SNIPE_PREP_SECONDS", 0), mock.patch.object(bot, "notify"), mock.patch.object(bot, "log"):
            b.run_snipe(plan)
        self.assertIsNone(b.snipe_plan)


if __name__ == "__main__":
    unittest.main()


class WalletChoice(unittest.TestCase):
    def three(self):
        keys = "0x" + "11" * 32 + ",0x" + "22" * 32 + ",0x" + "33" * 32
        return node(make_bot(MINTER_PRIVATE_KEYS=keys))

    def test_default_is_every_minter(self):
        b = self.three()
        b.command_reply("/snipe", [CONTRACT, "1", "0.001", "10m"])
        self.assertEqual(len(b.snipe_plan["wallets"]), 3)
        self.assertIn("Minters: all 3", b.snipe_command([CONTRACT, "1", "0.001", "10m"]))

    def test_numbers_pick_minters_and_the_plan_names_them(self):
        b = self.three()
        text = b.snipe_command([CONTRACT, "1", "0.001", "10m", "wallets=1,3"])
        self.assertEqual(b.snipe_plan["wallets"], [b.minters[0].address.lower(), b.minters[2].address.lower()])
        self.assertIn("2 of 3", text)
        self.assertIn("#1", text)
        self.assertIn("#3", text)

    def test_a_space_after_the_comma_and_an_address_both_work(self):
        b = self.three()
        b.snipe_command([CONTRACT, "1", "0.001", "wallets=2,", "3", "10m"])
        self.assertEqual(len(b.snipe_plan["wallets"]), 2)
        b.snipe_plan = None
        b.snipe_command([CONTRACT, "1", "0.001", "10m", f"wallets={b.minters[1].address[:10]}"])
        self.assertEqual(b.snipe_plan["wallets"], [b.minters[1].address.lower()])

    def test_bad_choices_are_refused_and_make_no_plan(self):
        b = self.three()
        for spec in ("wallets=4", "wallets=0", "wallets=abc", "wallets=0x1234567890", "wallets="):
            text = b.snipe_command([CONTRACT, "1", "0.001", "10m", spec])
            self.assertIn("⚠️", text, spec)
            self.assertIsNone(b.snipe_plan, spec)

    def test_only_the_chosen_minters_send_and_the_summary_counts_them(self):
        b = self.three()
        b.ensure = lambda acct, chain_id, need: True
        sent = []
        original = b.rpc.call.side_effect

        def call(chain_id, method, params):
            if method == "eth_sendRawTransaction":
                sent.append(params[0])
                return f"0xtx{len(sent)}"
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "gasUsed": hex(50_000), "effectiveGasPrice": hex(10**9)}
            return original(chain_id, method, params)

        b.rpc.call.side_effect = call
        b.snipe_command([CONTRACT, "1", "0.001", "10m", "wallets=2"])
        plan = b.snipe_plan
        plan["fire_at"] = bot.time.time() + 0.3
        with mock.patch.object(bot, "SNIPE_PREP_SECONDS", 0), mock.patch.object(bot, "notify") as notify, mock.patch.object(bot, "log"):
            b.run_snipe(plan)
        self.assertEqual(len(sent), 1)
        self.assertIn("Minters minted: 1 of 1", notify.call_args[0][0])

    def test_minters_are_numbered(self):
        b = self.three()
        text = b.command_reply("/minters", [])
        self.assertIn("1. " + b.minters[0].address, text)
        self.assertIn("3. " + b.minters[2].address, text)


class ShortForm(unittest.TestCase):
    def test_quantity_and_price_are_optional(self):
        b = node(make_bot(), price=10**15)
        text = b.snipe_command([CONTRACT, "42m", "wallets=1"])
        self.assertIn("Snipe plan", text)
        self.assertEqual(b.snipe_plan["qty"], 1)
        self.assertEqual(b.snipe_plan["value"], 10**15)
        self.assertEqual(b.snipe_plan["wallets"], [b.minters[0].address.lower()])

    def test_a_number_before_the_contract_is_the_quantity_when_it_is_not_a_chain(self):
        b = node(make_bot(), price=10**15)
        b.snipe_command(["2", CONTRACT, "42m", "wallets=1"])
        self.assertEqual(b.snipe_plan["qty"], 2)
        self.assertEqual(b.snipe_plan["chain"], 4663)

    def test_the_countdown_can_come_before_the_contract_and_the_long_form_still_works(self):
        b = node(make_bot(), price=10**15)
        b.snipe_command(["in", "20m", CONTRACT])
        self.assertEqual(b.snipe_plan["qty"], 1)
        b.snipe_plan = None
        b.snipe_command(["4663", CONTRACT, "3", "0.001", "10m"])
        self.assertEqual((b.snipe_plan["qty"], b.snipe_plan["value"]), (3, 3 * 10**15))

    def test_stray_words_are_refused(self):
        b = node(make_bot(), price=10**15)
        text = b.snipe_command(["1", CONTRACT, "42m", "Add", "wallets=1"])
        self.assertIn("⚠️", text)
        self.assertIsNone(b.snipe_plan)


class ChainChoice(unittest.TestCase):
    def two_chains(self, code_on):
        b = node(make_bot(CHAINS="1,4663"), price=10**15)
        original = b.rpc.call.side_effect

        def call(chain_id, method, params):
            if method == "eth_getCode" and chain_id not in code_on:
                return "0x"
            return original(chain_id, method, params)

        b.rpc.call.side_effect = call
        return b

    def test_the_chain_is_found_by_where_the_contract_is(self):
        b = self.two_chains({4663})
        b.snipe_command([CONTRACT, "42m", "wallets=1"])
        self.assertEqual(b.snipe_plan["chain"], 4663)

    def test_a_leading_one_is_the_quantity_when_the_contract_is_not_on_chain_one(self):
        b = self.two_chains({4663})
        b.snipe_command(["1", CONTRACT, "42m", "wallets=1"])
        self.assertEqual((b.snipe_plan["chain"], b.snipe_plan["qty"]), (4663, 1))
        b.snipe_plan = None
        b.snipe_command(["2", CONTRACT, "42m"])
        self.assertEqual((b.snipe_plan["chain"], b.snipe_plan["qty"]), (4663, 2))

    def test_a_chain_number_still_works_when_the_contract_is_there(self):
        b = self.two_chains({1, 4663})
        b.snipe_command(["4663", CONTRACT, "3", "0.001", "10m"])
        self.assertEqual((b.snipe_plan["chain"], b.snipe_plan["qty"]), (4663, 3))

    def test_chain_by_name_and_unknown_chain(self):
        b = self.two_chains({1, 4663})
        b.snipe_command([CONTRACT, "42m", "chain=robinhood"])
        self.assertEqual(b.snipe_plan["chain"], 4663)
        b.snipe_plan = None
        self.assertIn("do not know the chain", b.snipe_command([CONTRACT, "42m", "chain=solana"]))

    def test_no_contract_anywhere_is_said(self):
        b = self.two_chains(set())
        self.assertIn("any enabled chain", b.snipe_command([CONTRACT, "42m"]))

    def test_the_error_names_the_chain_and_suggests_fn(self):
        b = node(make_bot(), code="0x6080")
        text = b.snipe_command([CONTRACT, "42m"])
        self.assertIn("on Robinhood (chain 4663)", text)
        self.assertIn("fn=", text)

    def test_a_named_custom_function_is_used_when_it_is_in_the_contract(self):
        sig = "claimNow(uint256)"
        code = "0x6080" + "63" + bot.selector(sig)[2:] + "00"
        b = node(make_bot(), code=code, price=10**15)
        text = b.snipe_command([CONTRACT, "2", "42m", f"fn={sig}"])
        self.assertIn(sig, text)
        self.assertEqual(b.snipe_plan["layout"], "q")
        b.snipe_plan = None
        self.assertIn("⚠️", b.snipe_command([CONTRACT, "2", "42m", "fn=other(uint256)"]))
        self.assertIn("⚠️", b.snipe_command([CONTRACT, "2", "42m", "fn=weird(bytes)"]))


class PublicDrop(unittest.TestCase):
    def drop(self, **over):
        start = int(over.get("start", bot.time.time() + 3600))
        body = {"price": 2 * 10**14, "start": start, "end": start + 1800, "max": 1, "recipient": "0x" + "12" * 20}
        body.update(over)
        return body

    def test_short_rpc_returns_are_not_a_drop(self):
        self.assertIsNone(bot.decode_public_drop("0x"))
        self.assertIsNone(bot.decode_public_drop(hex(10**15)))
        self.assertIsNone(bot.decode_public_drop(None))

    def test_a_six_word_drop_is_read(self):
        words = [2 * 10**14, 100, 500, 3, 1000, 1]
        raw = "0x" + "".join(f"{n:064x}" for n in words)
        self.assertEqual(bot.decode_public_drop(raw), {"price": 2 * 10**14, "start": 100, "end": 500, "max": 3})

    def test_open_sets_the_fire_time_to_the_drop(self):
        b = node(make_bot())
        drop = self.drop()
        b.public_drop = lambda *a, **k: drop
        text = b.command_reply("/snipe", [CONTRACT, "1", "open"])
        self.assertIn("mintPublic", text)
        self.assertIn("Nothing is broadcast", text)
        self.assertEqual(b.snipe_plan["fire_at"], drop["start"])
        self.assertEqual(b.snipe_plan["target"], bot.SEADROP)
        self.assertEqual(b.snipe_plan["value"], drop["price"])
        self.assertTrue(b.snipe_plan["calldata"].startswith(bot.MINT_PUBLIC))
        self.assertNotIn(bot.selector("mint(uint256)"), b.snipe_plan["calldata"])

    def test_open_without_a_drop_is_refused(self):
        b = node(make_bot())
        text = b.command_reply("/snipe", [CONTRACT, "1", "open"])
        self.assertIn("no SeaDrop", text)
        self.assertIsNone(b.snipe_plan)

    def test_an_early_countdown_moves_to_the_drop_start(self):
        b = node(make_bot())
        drop = self.drop()
        b.public_drop = lambda *a, **k: drop
        b.command_reply("/snipe", [CONTRACT, "1", "0", "20m"])
        self.assertEqual(b.snipe_plan["fire_at"], drop["start"])
        self.assertEqual(b.snipe_plan["target"], bot.SEADROP)
        self.assertTrue(b.snipe_plan["calldata"].startswith(bot.MINT_PUBLIC))


class ClosedSale(unittest.TestCase):
    def test_a_reverting_call_sends_nothing(self):
        b = make_bot()
        report = {}
        acct = b.minters[0]
        sent = []

        def call(chain_id, method, params):
            if method == "eth_sendRawTransaction":
                sent.append(params[0])
                return "0xtx"
            if method == "eth_call":
                raise RuntimeError("execution reverted: not live")
            if method == "eth_getTransactionCount":
                return "0x1"
            return None

        b.rpc = mock.Mock()
        b.rpc.call.side_effect = call
        with mock.patch.object(bot, "log"):
            b.snipe_worker(acct, sample_plan(b), 5, 2 * 10**9, 10**9, None, bot.time.time() + 0.35, report)
        record = report[acct.address.lower()]
        self.assertEqual(sent, [])
        self.assertEqual(record["failed_txs"], [])
        self.assertIn("nothing was sent", record["reason"])

    def test_it_sends_once_the_call_starts_succeeding(self):
        b = make_bot()
        report = {}
        acct = b.minters[0]
        sent = []
        probes = {"n": 0}

        def call(chain_id, method, params):
            if method == "eth_call":
                probes["n"] += 1
                if probes["n"] < 3:
                    raise RuntimeError("execution reverted")
                return "0x"
            if method == "eth_sendRawTransaction":
                sent.append(params[0])
                return "0xtx1"
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1"}
            if method == "eth_getTransactionCount":
                return "0x3"
            return None

        b.rpc = mock.Mock()
        b.rpc.call.side_effect = call
        with mock.patch.object(bot, "log"):
            b.snipe_worker(acct, sample_plan(b), None, 2 * 10**9, 10**9, None, bot.time.time() + 2, report)
        self.assertEqual(len(sent), 1)
        self.assertEqual(report[acct.address.lower()]["status"], "minted")

    def test_an_rpc_blip_is_retried_instead_of_forgotten(self):
        b = make_bot()
        b.simulate = lambda *a, **k: "temporary"
        tx = {"hash": "0x" + "ab" * 32, "from": WATCHED, "to": CONTRACT, "value": "0x0", "input": "0x1234"}
        with mock.patch.object(bot, "notify"), mock.patch.object(bot, "log"):
            b.copy(4663, tx, {CONTRACT.lower(): 1}, options=[(CONTRACT, bot.selector("mint(uint256)") + "0" * 64, 0)])
        self.assertNotIn(f"4663:{tx['hash']}", b.seen)
        self.assertIn(f"4663:{tx['hash']}", b.pending)
