import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot  # noqa: E402

WATCHED = "0x" + "ab" * 20
DEST = "0x" + "d3" * 20
C721 = "0x" + "c1" * 20
C1155 = "0x" + "c2" * 20
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


class FakeChain:
    """Just enough of a node for ERC-721 and ERC-1155 reads."""

    def __init__(self):
        self.owner = {}  # (collection, id) -> wallet
        self.balance1155 = {}  # (collection, id, wallet) -> amount
        self.enumerable = True
        self.supply = {}
        self.refuse = set()  # (collection, id) whose transfer reverts

    def call(self, chain_id, method, params):
        if method == "eth_getCode":
            return "0x6080"
        if method == "eth_estimateGas":
            call = params[0]
            data = call["data"]
            if data.startswith(bot.SAFE_TRANSFER_721):
                token = int(data[-64:], 16)
                if (call["to"].lower(), token) in self.refuse:
                    raise RuntimeError("execution reverted: transfer locked")
            return hex(60_000)
        if method != "eth_call":
            return None
        to = params[0]["to"].lower()
        data = params[0]["data"]
        sel = data[:10]
        if sel == bot.SUPPORTS_INTERFACE:
            want = data[10:18]
            if to == C721 and want == "80ac58cd":
                return "0x" + "0" * 63 + "1"
            if to == C1155 and want == "d9b67a26":
                return "0x" + "0" * 63 + "1"
            return "0x" + "0" * 64
        if sel == bot.BALANCE_OF_721:
            wallet = "0x" + data[34:74]
            count = sum(1 for (c, _i), w in self.owner.items() if c == to and w == wallet)
            return "0x" + f"{count:064x}"
        if sel == bot.OWNER_OF:
            token = int(data[10:74], 16)
            owner = self.owner.get((to, token))
            if not owner:
                raise RuntimeError("execution reverted")
            return "0x" + "0" * 24 + owner[2:]
        if sel == bot.TOKEN_BY_INDEX:
            if not self.enumerable:
                raise RuntimeError("execution reverted")
            wallet = "0x" + data[34:74]
            mine = sorted(i for (c, i), w in self.owner.items() if c == to and w == wallet)
            index = int(data[74:138], 16)
            if index >= len(mine):
                raise RuntimeError("execution reverted")
            return "0x" + f"{mine[index]:064x}"
        if sel == bot.TOTAL_SUPPLY:
            return "0x" + f"{self.supply.get(to, 0):064x}"
        if sel == bot.BALANCE_OF_1155:
            wallet = "0x" + data[34:74]
            token = int(data[74:138], 16)
            return "0x" + f"{self.balance1155.get((to, token, wallet), 0):064x}"
        if sel == "0x06fdde03":
            return "0x"
        raise RuntimeError("execution reverted")


def node(b, chain=None):
    chain = chain or FakeChain()
    b.rpc = mock.Mock()
    b.rpc.call.side_effect = chain.call
    b.fake = chain
    return b


def topic(address):
    return "0x" + "0" * 24 + address[2:].lower()


def receipt(logs):
    return {"status": "0x1", "logs": logs}


class Recording(unittest.TestCase):
    def test_minted_token_ids_are_remembered(self):
        b = node(make_bot())
        me = b.minters[0].address
        logs = [
            {"address": C721, "topics": [bot.TRANSFER, bot.ZERO_TOPIC, topic(me), "0x" + f"{7:064x}"], "data": "0x"},
            {"address": C721, "topics": [bot.TRANSFER, bot.ZERO_TOPIC, topic(me), "0x" + f"{8:064x}"], "data": "0x"},
            {"address": C721, "topics": [bot.TRANSFER, bot.ZERO_TOPIC, topic(WATCHED), "0x" + f"{9:064x}"], "data": "0x"},
            {"address": C1155, "topics": [bot.TRANSFER_SINGLE, topic(WATCHED), bot.ZERO_TOPIC, topic(me)], "data": "0x" + f"{3:064x}" + f"{2:064x}"},
            {"address": "0x" + "ee" * 20, "topics": [bot.TRANSFER, bot.ZERO_TOPIC, topic(me)], "data": "0x" + f"{5:064x}"},
        ]
        b.rpc.call.side_effect = lambda c, m, p: receipt(logs) if m == "eth_getTransactionReceipt" else None
        b.record_holdings(4663, me, "0xtx")
        self.assertEqual(b.holdings[f"4663:{C721}:{me.lower()}"], {"std": 721, "ids": ["7", "8"]})
        self.assertEqual(b.holdings[f"4663:{C1155}:{me.lower()}"], {"std": 1155, "ids": ["3"]})
        self.assertEqual(len(b.holdings), 2)

    def test_batch_mints_are_remembered_and_state_round_trips(self):
        from eth_abi import encode

        b = node(make_bot())
        me = b.minters[0].address
        data = "0x" + encode(["uint256[]", "uint256[]"], [[1, 2], [1, 5]]).hex()
        logs = [{"address": C1155, "topics": [bot.TRANSFER_BATCH, topic(WATCHED), bot.ZERO_TOPIC, topic(me)], "data": data}]
        b.rpc.call.side_effect = lambda c, m, p: receipt(logs) if m == "eth_getTransactionReceipt" else None
        b.record_holdings(4663, me, "0xtx")
        self.assertEqual(b.holdings[f"4663:{C1155}:{me.lower()}"]["ids"], ["1", "2"])
        again = make_bot(STATE_PATH=str(b.state_path))
        self.assertEqual(again.holdings, b.holdings)

    def test_a_failed_receipt_read_never_raises(self):
        b = make_bot()
        b.rpc = mock.Mock()
        b.rpc.call.side_effect = RuntimeError("down")
        with mock.patch.object(bot, "log"):
            b.record_holdings(4663, b.minters[0].address, "0xtx")
        self.assertEqual(b.holdings, {})


class Finding(unittest.TestCase):
    def test_recorded_721_ids_are_checked_against_the_chain(self):
        b = node(make_bot())
        me = b.minters[0].address.lower()
        b.fake.owner = {(C721, 7): me, (C721, 8): "0x" + "99" * 20}
        b.add_holding(4663, C721, me, 721, [7, 8])
        found = b.find_nfts(4663, b.minters[0])
        self.assertEqual([(g["collection"], g["items"], g["missing"]) for g in found], [(C721, [(7, 1)], 0)])

    def test_unrecorded_tokens_are_listed_when_the_collection_is_enumerable(self):
        b = node(make_bot())
        me = b.minters[0].address.lower()
        b.fake.owner = {(C721, 3): me, (C721, 5): me}
        found = b.find_nfts(4663, b.minters[0], extra=[C721])
        self.assertEqual(found[0]["items"], [(3, 1), (5, 1)])

    def test_without_enumeration_the_missing_ones_are_counted_and_deep_finds_them(self):
        b = node(make_bot())
        me = b.minters[0].address.lower()
        b.fake.enumerable = False
        b.fake.owner = {(C721, 3): me, (C721, 5): me}
        b.fake.supply[C721] = 10
        found = b.find_nfts(4663, b.minters[0], extra=[C721])
        self.assertEqual((found[0]["items"], found[0]["missing"]), ([], 2))
        found = b.find_nfts(4663, b.minters[0], extra=[C721], deep=True)
        self.assertEqual((found[0]["items"], found[0]["missing"]), ([(3, 1), (5, 1)], 0))

    def test_1155_amounts_come_from_the_chain(self):
        b = node(make_bot())
        me = b.minters[0].address.lower()
        b.fake.balance1155 = {(C1155, 3, me): 4}
        b.add_holding(4663, C1155, me, 1155, [3, 4])
        found = b.find_nfts(4663, b.minters[0])
        self.assertEqual(found[0]["items"], [(3, 4)])

    def test_a_minter_that_holds_nothing_has_nothing_listed(self):
        b = node(make_bot())
        b.minted_before = {f"4663:{C721}": [b.minters[0].address.lower()]}
        self.assertEqual(b.find_nfts(4663, b.minters[0]), [])


class Command(unittest.TestCase):
    def start(self, b, args):
        with mock.patch.object(bot.threading, "Thread") as thread:
            text = b.command_reply("/sendnfts", args)
        return text, thread

    def test_it_is_in_the_menu_and_shows_usage(self):
        b = make_bot()
        self.assertIn("/sendnfts", b.command_reply("/help"))
        self.assertIn("Usage", b.command_reply("/sendnfts", []))

    def test_a_valid_command_starts_looking(self):
        b = make_bot()
        text, thread = self.start(b, [DEST, "wallets=1"])
        self.assertIn("Looking for NFTs in 1 wallet", text)
        args = thread.call_args.kwargs["args"]
        self.assertEqual(args[0], DEST)
        self.assertEqual([a.address for a in args[1]], [b.minters[0].address])

    def test_bad_input_starts_nothing(self):
        b = make_bot()
        for args in (
            ["0x123"],
            ["0x" + "0" * 40],
            [DEST, DEST],
            [DEST, "wallets=9"],
            [DEST, "collection=abc"],
            [DEST, "chain=solana"],
        ):
            text, thread = self.start(b, args)
            self.assertIn("⚠️", text, args)
            thread.assert_not_called()

    def test_confirm_without_a_plan_and_cancel_with_nothing(self):
        b = make_bot()
        self.assertIn("no plan", b.command_reply("/sendnfts", ["confirm"]))
        self.assertIn("Nothing", b.command_reply("/sendnfts", ["cancel"]))


class Planning(unittest.TestCase):
    def plan(self, b, accounts=None, **kwargs):
        with mock.patch.object(bot, "notify") as notify:
            b.prepare_send(DEST, accounts or list(b.minters), [4663], kwargs.get("extra", []), kwargs.get("deep", False))
        return notify.call_args[0][0]

    def test_the_plan_lists_what_will_be_sent_and_waits_for_confirmation(self):
        b = node(make_bot())
        me = b.minters[0].address.lower()
        b.fake.owner = {(C721, 7): me, (C721, 8): me}
        b.add_holding(4663, C721, me, 721, [7, 8])
        text = self.plan(b)
        self.assertIn(DEST, text)
        self.assertIn("×2", text)
        self.assertIn("#1", text)
        self.assertIn("/sendnfts confirm", text)
        self.assertEqual(b.send_plan["state"], "planned")
        self.assertEqual(len(b.send_plan["transfers"]), 2)

    def test_nfts_the_contract_refuses_are_reported_and_not_planned(self):
        b = node(make_bot())
        me = b.minters[0].address.lower()
        b.fake.owner = {(C721, 7): me, (C721, 8): me}
        b.fake.refuse = {(C721, 8)}
        b.add_holding(4663, C721, me, 721, [7, 8])
        text = self.plan(b)
        self.assertIn("Cannot send 1", text)
        self.assertIn("transfer locked", text)
        self.assertEqual([t["id"] for t in b.send_plan["transfers"]], ["7"])

    def test_nothing_found_makes_no_plan(self):
        b = node(make_bot())
        text = self.plan(b)
        self.assertIn("no NFTs", text)
        self.assertIsNone(b.send_plan)

    def test_the_destination_wallet_itself_is_skipped(self):
        b = node(make_bot())
        me = b.minters[0].address.lower()
        b.fake.owner = {(C721, 7): me}
        b.add_holding(4663, C721, me, 721, [7])
        with mock.patch.object(bot, "notify") as notify:
            b.prepare_send(me, list(b.minters), [4663], [], False)
        self.assertIn("Skipped", notify.call_args[0][0])
        self.assertIsNone(b.send_plan)

    def test_untraceable_holdings_are_mentioned(self):
        b = node(make_bot())
        me = b.minters[0].address.lower()
        b.fake.enumerable = False
        b.fake.owner = {(C721, 3): me}
        text = self.plan(b, extra=[C721])
        self.assertIn("cannot list their token IDs", text)
        self.assertIn("deep", text)


class Sending(unittest.TestCase):
    def ready(self, count=2, std=721):
        b = node(make_bot())
        me = b.minters[0].address.lower()
        b.fake.owner = {(C721, i): me for i in range(1, count + 1)}
        b.add_holding(4663, C721, me, 721, range(1, count + 1))
        with mock.patch.object(bot, "notify"):
            b.prepare_send(DEST, [b.minters[0]], [4663], [], False)
        return b, me

    def test_confirm_sends_every_transfer_and_forgets_them(self):
        b, me = self.ready()
        sent = []

        def land(chain_id, acct, to, data, value, gas):
            sent.append((acct.address.lower(), to, data))
            return f"0xtx{len(sent)}", True, ["x"]

        b.land = land
        with mock.patch.object(bot.threading, "Thread") as thread:
            self.assertIn("Sending 2", b.command_reply("/sendnfts", ["confirm"]))
        with mock.patch.object(bot, "notify") as notify:
            b.run_send(thread.call_args.kwargs["args"][0])
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(to == C721 and d.startswith(bot.SAFE_TRANSFER_721) for _w, to, d in sent))
        self.assertTrue(all(DEST[2:] in d for _w, _to, d in sent))
        self.assertIn("Sent 2 of 2", notify.call_args[0][0])
        self.assertEqual(b.holdings, {})
        self.assertIsNone(b.send_plan)

    def test_a_failed_transfer_is_reported_and_kept(self):
        b, me = self.ready()
        calls = iter([True, False])
        b.land = lambda *a: ("0xtx", next(calls), ["x"])
        with mock.patch.object(bot, "notify") as notify:
            b.run_send(b.send_plan)
        text = notify.call_args[0][0]
        self.assertIn("Sent 1 of 2", text)
        self.assertIn("Failed", text)
        self.assertEqual(len(b.holdings[f"4663:{C721}:{me}"]["ids"]), 1)

    def test_a_dry_run_sends_nothing_and_keeps_the_record(self):
        b, me = self.ready()
        b.dry = True
        b.send = lambda *a, **k: ("dry", True)
        with mock.patch.object(bot, "notify") as notify, mock.patch.object(bot, "log"):
            b.run_send(b.send_plan)
        self.assertIn("Would send 2 of 2", notify.call_args[0][0])
        self.assertEqual(len(b.holdings[f"4663:{C721}:{me}"]["ids"]), 2)

    def test_1155_transfers_carry_the_amount(self):
        b = node(make_bot())
        me = b.minters[0].address.lower()
        b.fake.balance1155 = {(C1155, 3, me): 4}
        b.add_holding(4663, C1155, me, 1155, [3])
        with mock.patch.object(bot, "notify"):
            b.prepare_send(DEST, [b.minters[0]], [4663], [], False)
        data = bot.Bot.transfer_data(b, 1155, me, DEST, 3, 4)
        self.assertTrue(data.startswith(bot.SAFE_TRANSFER_1155))
        self.assertEqual(b.send_plan["transfers"][0]["amount"], 4)

    def test_an_old_plan_expires(self):
        b, _me = self.ready()
        b.send_plan["created"] -= bot.SEND_PLAN_SECONDS + 5
        self.assertIn("expired", b.command_reply("/sendnfts", ["confirm"]))
        self.assertIsNone(b.send_plan)

    def test_cancel_discards_a_waiting_plan(self):
        b, _me = self.ready()
        self.assertIn("Cancelled", b.command_reply("/sendnfts", ["cancel"]))
        self.assertIsNone(b.send_plan)

    def test_cancel_stops_the_remaining_transfers(self):
        b, _me = self.ready(count=3)
        count = []

        def land(chain_id, acct, to, data, value, gas):
            count.append(1)
            b.send_cancel.set()
            return "0xtx", True, ["x"]

        b.land = land
        with mock.patch.object(bot, "notify") as notify:
            b.run_send(b.send_plan)
        self.assertEqual(len(count), 1)
        self.assertIn("Stopped before sending 2", notify.call_args[0][0])

    def test_a_second_send_cannot_start_while_one_runs(self):
        b, _me = self.ready()
        b.send_plan["state"] = "running"
        self.assertIn("already running", b.command_reply("/sendnfts", [DEST]))


class Ids(unittest.TestCase):
    def test_id_lists_and_ranges_are_parsed(self):
        self.assertEqual(bot.parse_id_list("5,7,9-11")[0], [5, 7, 9, 10, 11])
        self.assertEqual(bot.parse_id_list("3,3,2")[0], [3, 2])
        for bad in ("", "x", "5-2", "1-99999999"):
            self.assertIsNotNone(bot.parse_id_list(bad)[1], bad)

    def test_ids_need_exactly_one_collection(self):
        b = make_bot()
        with mock.patch.object(bot.threading, "Thread") as thread:
            self.assertIn("exactly one collection", b.command_reply("/sendnfts", [DEST, "ids=1,2"]))
            self.assertIn("⚠️", b.command_reply("/sendnfts", [DEST, f"collection={C721}", "ids=x"]))
            thread.assert_not_called()
            b.command_reply("/sendnfts", [DEST, f"collection={C721}", "ids=1-3"])
        self.assertEqual(thread.call_args.kwargs["args"][5], [1, 2, 3])

    def test_given_ids_find_tokens_the_collection_cannot_list(self):
        b = node(make_bot())
        me = b.minters[0].address.lower()
        b.fake.enumerable = False
        b.fake.owner = {(C721, 40): me, (C721, 41): me, (C721, 99): "0x" + "77" * 20}
        found = b.find_nfts(4663, b.minters[0], extra=[C721], given=list(range(30, 60)))
        self.assertEqual(found[0]["items"], [(40, 1), (41, 1)])
        self.assertEqual(found[0]["missing"], 0)

    def test_the_plan_says_why_deep_found_nothing(self):
        b = node(make_bot())
        me = b.minters[0].address.lower()
        b.fake.enumerable = False
        b.fake.owner = {(C721, 500): me}
        with mock.patch.object(bot, "notify") as notify:
            b.prepare_send(DEST, [b.minters[0]], [4663], [C721], True, [])
        self.assertIn("totalSupply() could not be read", notify.call_args[0][0])
        b.fake.supply[C721] = bot.DEEP_SCAN_LIMIT + 1
        with mock.patch.object(bot, "notify") as notify:
            b.prepare_send(DEST, [b.minters[0]], [4663], [C721], True, [])
        self.assertIn("above the deep scan limit", notify.call_args[0][0])
        b.fake.supply[C721] = 10
        with mock.patch.object(bot, "notify") as notify:
            b.prepare_send(DEST, [b.minters[0]], [4663], [C721], True, [])
        self.assertIn("not in that range", notify.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
