"""Plain-words typing for /buy, /sweep, /list, /offer, /sellall and /snipe.

    /buy 0xabc… 123 under 0.01 wallet 2 at 5pm UTC
    /sweep 0xabc… under 0.002 budget 0.5 wallets 1 and 2 on robinhood
    /list 0xabc… 123 for 2x wallet 1
    /offer 0xabc… 123 0.01 hours 6

Each is rewritten into the exact option form the commands already read (wallets=, max=, budget=, chain=,
an OpenSea link), so the old forms keep working. Also holds the shared pre-check and the confirm switch.
"""

from __future__ import annotations

import re
import time

import opensea
from helpers import *  # noqa: F401,F403

FILLER = {"of", "the", "nft", "nfts", "token", "tokens", "id", "using", "with", "please", "a", "an", "to", "each", "per"}
WALLET_WORDS = {"wallet", "wallets", "minter", "minters"}
MAX_WORDS = {"under", "below", "max", "cap", "limit", "upto"}
PRICE_WORDS = {"at", "for", "price"}
DURATION = re.compile(r"(?:\d+[hms])+")
KINDS_WITH_ITEM = ("buy", "list", "offer")


class EasyMixin:
    def easy_args(self, args, kind):
        """(args, start_delay, start_label, error). start_delay is None unless a clock time or countdown was typed."""
        tokens = [t for t in args]
        delay, label = None, ""
        if kind in ("buy", "sweep"):
            clock = find_clock(tokens, time.time())
            if clock:
                delay, label, first, last = clock
                del tokens[first:last]
            else:
                for i, token in enumerate(tokens):
                    if DURATION.fullmatch(token.lower()):
                        seconds = sum(int(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in re.findall(r"(\d+)([hms])", token.lower()))
                        first = i - 1 if i and tokens[i - 1].lower() == "in" else i
                        delay, label = seconds, f"in {fmt_duration(seconds)}"
                        del tokens[first : i + 1]
                        break
        out = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            low = token.lower().strip(",")
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if "=" in token or low.startswith(("http", "opensea")) or valid_address(token):
                out.append(token)
                i += 1
            elif low in WALLET_WORDS or (low == "w" and nxt[:1].isdigit()):
                numbers, j = [], i + 1
                while j < len(tokens) and (re.fullmatch(r"[\d,]+", tokens[j]) or tokens[j].lower() in ("and", "all")):
                    if tokens[j].lower() == "all":
                        numbers = ["all"]
                        j += 1
                        break
                    if tokens[j].lower() != "and":
                        numbers += [n for n in tokens[j].split(",") if n]
                    j += 1
                if numbers:
                    if numbers != ["all"]:
                        out.append("wallets=" + ",".join(numbers))
                    i = j
                else:
                    i += 1
            elif low in MAX_WORDS and kind in ("buy", "sweep") and nxt:
                out.append("max=" + nxt)
                i += 2
            elif low == "up" and nxt.lower() == "to" and i + 2 < len(tokens) and kind in ("buy", "sweep"):
                out.append("max=" + tokens[i + 2])
                i += 3
            elif low in ("budget", "total", "spend") and nxt and kind == "sweep":
                out.append("budget=" + nxt)
                i += 2
            elif low in ("days", "hours") and nxt.isdigit():
                out.append(f"{low}={nxt}")
                i += 2
            elif low == "chain" and nxt:
                out.append("chain=" + nxt)
                i += 2
            elif low == "on" and nxt and self.chain_from_word(nxt) is not None:
                out.append("chain=" + nxt)
                i += 2
            elif low in PRICE_WORDS and kind in ("list", "offer", "buy") and nxt and low != "price":
                # "for 2x", "at 0.05": the price is the word that follows, so the filler word goes.
                i += 1
            elif low == "price" and nxt:
                i += 1
            elif low in FILLER or low == "#":
                i += 1
            else:
                out.append(token.lstrip("#") if token.startswith("#") and token[1:].isdigit() else token)
                i += 1
        if kind in KINDS_WITH_ITEM or kind == "sweep":
            out, error = self.easy_target(out, kind)
            if error:
                return out, delay, label, error
        return out, delay, label, None

    def easy_target(self, args, kind):
        """Turn a bare contract address (and token ID) into the OpenSea link the commands read."""
        if opensea.parse_item_link(" ".join(args)) or opensea.parse_collection_link(" ".join(args)):
            return args, None
        position = next((i for i, t in enumerate(args) if valid_address(t)), None)
        if position is None:
            return args, None
        contract = valid_address(args[position])
        chain_word = next((a.split("=", 1)[1] for a in args if a.lower().startswith("chain=")), None)
        chain_id = None
        if chain_word is not None:
            chain_id = self.chain_from_word(chain_word)
            if chain_id is None:
                names = ", ".join(f"{c.name} ({cid})" for cid, c in self.chains.items())
                return args, f"⚠️ I do not know the chain \"{chain_word}\". Enabled chains: {names}."
        else:
            usable = [c for c in self.chains if c in opensea.CHAIN_TO_SLUG]
            if len(usable) == 1:
                chain_id = usable[0]
            else:
                found = [c for c in usable if self.has_code(c, contract)]
                if len(found) == 1:
                    chain_id = found[0]
                elif not found:
                    return args, "⚠️ I could not find a contract at that address on any enabled chain. Check it, or add chain=NAME."
                else:
                    names = ", ".join(self.chains[c].name for c in found)
                    return args, f"⚠️ That address has a contract on several chains ({names}). Add chain=NAME."
        if chain_id not in opensea.CHAIN_TO_SLUG:
            return args, f"⚠️ OpenSea does not cover chain {chain_id}, so this command cannot use it."
        slug = opensea.CHAIN_TO_SLUG[chain_id]
        rest = [a for a in args if not a.lower().startswith("chain=")]
        position = rest.index(args[position]) if args[position] in rest else position
        token = None
        if position + 1 < len(rest) and rest[position + 1].isdigit():
            token = int(rest[position + 1])
            rest = rest[: position + 1] + rest[position + 2 :]
        if token is not None:
            rest[position] = f"https://opensea.io/item/{slug}/{contract}/{token}"
            return rest, None
        if kind != "sweep":
            return args, f"⚠️ Add the token ID after the contract, for example /{kind} {contract} 123."
        try:
            collection, _name = self.opensea.contract_collection(slug, contract)
        except opensea.OpenSeaError as exc:
            return args, f"⚠️ OpenSea said no: {exc}"
        if not collection:
            return args, "⚠️ OpenSea does not know that collection yet, so I cannot sweep it."
        rest[position] = f"https://opensea.io/collection/{collection}"
        rest.append(f"chain={chain_id}")
        return rest, None

    # ---- shared pre-check and confirm switch ------------------------------------------

    def gas_margin(self, chain_id, gas=250_000):
        """A rough cost of one transaction's gas, so a wallet with nothing left for gas is caught early."""
        try:
            fee, _tip = self.fees(chain_id)
            return gas * fee
        except Exception:
            return 0

    def short_wallets(self, chain_id, wallet_addresses, need_wei):
        """[(number, address, balance)] for wallets whose native balance is under need_wei."""
        out = []
        for number, acct in enumerate(self.minters, 1):
            if acct.address.lower() not in wallet_addresses:
                continue
            try:
                have = self.balance(chain_id, "native", acct.address)
            except Exception:
                continue
            if have < need_wei:
                out.append((number, acct.address, have))
        return out

    def funds_note(self, chain_id, wallet_addresses, need_wei, what="the price plus gas"):
        """(warning text or '', True when no chosen wallet can pay)."""
        short = self.short_wallets(chain_id, wallet_addresses, need_wei)
        if not short:
            return "", False
        lines = [
            f"⚠️ 👛 {n}. {short_address(a)} has {fmt_native(have, chain_id)}, needs about {fmt_native(need_wei, chain_id)} for {what}."
            for n, a, have in short
        ]
        return "\n".join(lines), len(short) == len(wallet_addresses)

    def finish_plan(self, kind, text, confirm, problem="", bulk=False):
        """End of a plan: ask for confirm, or start at once when the confirm switch is off.

        confirm is the function that starts the plan. problem, when set, stops an automatic start.
        """
        key = "confirm_bulk" if bulk else "confirm_trades"
        if self.setting(key):
            return text + f"\n\nSend /{kind} confirm within 2 minutes, or /{kind} cancel."
        if problem:
            return text + f"\n\n⛔ Not started: {problem}\nFix that and send the command again."
        return text + "\n\n" + confirm()
