import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot  # noqa: E402
import phases  # noqa: E402


def stage(**over):
    body = {
        "label": "Godlist (GTD)",
        "kind": "SIGNED_PRESALE",
        "start": 1_000,
        "end": 2_000,
        "eligible": True,
        "max": 3,
        "price_wei": 0,
        "minted": 0,
    }
    body.update(over)
    return body


class ChooseStage(unittest.TestCase):
    def test_an_allowlist_beats_a_later_public_sale(self):
        public = stage(label="Public", kind="PUBLIC_SALE", start=5_000, end=9_000, max=1, price_wei=10**16)
        chosen = phases.choose_stage([public, stage()], 500)
        self.assertEqual(chosen["label"], "Godlist (GTD)")

    def test_an_open_allowlist_beats_public_even_if_public_is_also_open(self):
        public = stage(label="Public", kind="PUBLIC_SALE", start=50, end=9_000, max=1, price_wei=10**16)
        chosen = phases.choose_stage([public, stage(start=100, end=2_000)], 150)
        self.assertEqual(chosen["kind"], "SIGNED_PRESALE")

    def test_a_used_up_phase_is_skipped(self):
        public = stage(label="Public", kind="PUBLIC_SALE", start=5_000, end=9_000, max=1, price_wei=10**16)
        chosen = phases.choose_stage([stage(minted=3), public], 500)
        self.assertEqual(chosen["label"], "Public")

    def test_a_closed_phase_is_skipped(self):
        chosen = phases.choose_stage([stage(end=100)], 150)
        self.assertIsNone(chosen)

    def test_not_eligible_is_skipped(self):
        self.assertIsNone(phases.choose_stage([stage(eligible=False)], 500))


class Prices(unittest.TestCase):
    def test_a_coin_amount_becomes_wei(self):
        self.assertEqual(phases.price_wei(0), 0)
        self.assertEqual(phases.price_wei(0.005), 5 * 10**15)
        self.assertEqual(phases.price_wei("0.0002"), 2 * 10**14)

    def test_payment_revert_is_named(self):
        data = bot.selector("IncorrectPayment(uint256,uint256)") + "00" * 64
        self.assertEqual(bot.decode_revert(data), "wrong payment amount")


class MintAction(unittest.TestCase):
    def test_a_free_seadrop_mint_is_accepted(self):
        nft = "0x" + "ab" * 20
        data = "0x4b61cd6f" + nft[2:].rjust(64, "0")
        body = {
            "data": {
                "swap": {
                    "actions": [
                        {
                            "transactionSubmissionData": {
                                "to": bot.SEADROP,
                                "data": data,
                                "value": "0",
                                "chain": {"identifier": "robinhood", "networkId": 4663},
                            }
                        }
                    ],
                    "errors": [],
                }
            }
        }
        tx, err = phases.mint_call_from_action(body, 4663, "robinhood", nft, 0, bot.SEADROP)
        self.assertIsNone(err)
        self.assertEqual(tx[0], 0)
        self.assertEqual(tx[1], bot.SEADROP)

    def test_a_different_price_is_refused(self):
        nft = "0x" + "ab" * 20
        body = {
            "data": {
                "swap": {
                    "actions": [
                        {
                            "transactionSubmissionData": {
                                "to": bot.SEADROP,
                                "data": "0x4b61cd6f" + nft[2:].rjust(64, "0"),
                                "value": "1000",
                                "chain": {"identifier": "robinhood"},
                            }
                        }
                    ],
                    "errors": [],
                }
            }
        }
        tx, err = phases.mint_call_from_action(body, 4663, "robinhood", nft, 0, bot.SEADROP)
        self.assertIsNone(tx)
        self.assertIn("different price", err)

    def test_not_minting_yet_is_distinct_from_not_eligible(self):
        waiting = {"data": {"swap": {"actions": [], "errors": [{"__typename": "DropNotMintingError"}]}}}
        blocked = {"data": {"swap": {"actions": [], "errors": [{"__typename": "InsufficientMintsRemainingError"}]}}}
        self.assertEqual(phases.mint_call_from_action(waiting, 1, "ethereum", "0x" + "ab" * 20, 0, bot.SEADROP)[1], "not minting yet")
        self.assertIn("no mints", phases.mint_call_from_action(blocked, 1, "ethereum", "0x" + "ab" * 20, 0, bot.SEADROP)[1])


if __name__ == "__main__":
    unittest.main()
