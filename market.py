"""Selling and bidding on OpenSea: /list, /unlist, /sellall, /offer, auto-listing and /pnl. Mixed into Bot.

Nothing here signs or posts until the user confirms, except auto-listing, which the user switches on once with
/settings autolist on. Every order is checked against the Seaport contract itself before it is posted.
"""

from __future__ import annotations

import re
import threading
import time
from decimal import Decimal

from eth_utils import to_checksum_address

from helpers import *  # noqa: F401,F403
import opensea
import seaport

# Wrapped native coin per chain, used for offers. Others come from OpenSea's own offers or WETH_<chain id>.
WETH = {
    1: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    10: "0x4200000000000000000000000000000000000006",
    8453: "0x4200000000000000000000000000000000000006",
    42161: "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    137: "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
}
BALANCE_OF_ERC20 = selector("balanceOf(address)")
MAX_PLAN_ITEMS = 40


def parse_price_spec(text):
    """('multiple', 2.0) for '2x', ('price', Decimal) for '0.05', or (None, None)."""
    text = str(text or "").strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)x", text)
    if match:
        value = float(match.group(1))
        return ("multiple", value) if 1.0 <= value <= 50.0 else (None, None)
    try:
        value = Decimal(text)
    except Exception:
        return None, None
    if not value.is_finite() or value <= 0 or value > 1000:
        return None, None
    return "price", value


class MarketMixin:
    # ---- small helpers ----------------------------------------------------------------

    def account_for(self, wallet):
        wallet = (wallet or "").lower()
        return next((a for a in self.minters if a.address.lower() == wallet), None)

    def ledger_add(self, kind, chain_id, wallet, contract, wei, token=None, note=""):
        self.ledger.append(
            {"t": int(time.time()), "kind": kind, "chain": chain_id, "wallet": (wallet or "").lower(), "contract": str(contract or ""), "token": token, "wei": int(wei), "note": note}
        )
        self._save()

    def estimate_gas(self, chain_id, acct, to, data, value=0):
        est = int(self.rpc.call(chain_id, "eth_estimateGas", [{"from": acct.address, "to": to_checksum_address(to), "data": data, "value": hex(value)}]), 16)
        return int(est * 1.3) + 20_000

    def owns(self, chain_id, contract, token_id, wallet, std):
        if int(std) == 721:
            return self.owns_721(chain_id, contract, int(token_id), wallet)
        have = self.view_int(chain_id, contract, BALANCE_OF_1155 + addr_word(wallet) + uint_word(int(token_id)))
        return bool(have)

    def seaport_ready(self, chain_id):
        """(info, error). Confirms on-chain that Seaport's domain and OpenSea's conduit match what this bot signs for."""
        cached = self._seaport.get(chain_id)
        if cached:
            return cached, None
        try:
            raw = self.view(chain_id, seaport.SEAPORT, seaport.INFORMATION)
            if not raw:
                return None, "Seaport did not answer on this chain"
            version, separator, controller = seaport.decode_information(raw)
            if separator != "0x" + seaport.domain_separator(chain_id, seaport.SEAPORT, version).hex():
                return None, "my Seaport signing does not match the contract's domain, so I will not sign"
            raw = self.view(chain_id, controller, seaport.conduit_call(seaport.OPENSEA_CONDUIT_KEY))
            if not raw:
                return None, "the conduit controller did not answer"
            conduit, exists = seaport.decode_conduit(raw)
            if not exists:
                return None, "OpenSea's conduit is not set up on this chain"
        except Exception as exc:
            return None, f"could not verify Seaport ({type(exc).__name__})"
        info = {"version": version, "conduit": conduit}
        self._seaport[chain_id] = info
        return info, None

    def collection_fees(self, slug):
        cached = self._fee_cache.get(slug)
        if cached and time.time() - cached[0] < 3600:
            return cached[1]
        info = self.opensea.collection_info(slug)
        self._fee_cache[slug] = (time.time(), info["fees"])
        return info["fees"]

    def collection_slug_for(self, chain_id, contract):
        key = f"{chain_id}:{contract.lower()}"
        cached = self._slug_cache.get(key)
        if cached and (cached[1] or time.time() - cached[0] < 600):
            return cached[1]
        slug, name = self.opensea.contract_collection(opensea.CHAIN_TO_SLUG[chain_id], contract)
        self._slug_cache[key] = (time.time(), slug)
        if name:
            self._names.setdefault(key, name)
        return slug

    def send_native_tx(self, chain_id, acct, to, data, value=0):
        """Send a plain transaction and wait for it. Returns (hash, ok)."""
        gas = self.estimate_gas(chain_id, acct, to, data, value)
        with self._copy_lock:
            txh, ok, sent = self.land(chain_id, acct, to, data, value, gas)
        if not ok:
            hit = None
            try:
                hit = self.receipt_for(chain_id, sent)
            except Exception:
                pass
            if hit:
                txh, ok = hit
        return txh, ok

    def prepare_order(self, chain_id, acct, components):
        """Check the order against the contract, sign it, and return (signature, error)."""
        local = "0x" + seaport.order_hash(components).hex()
        raw = self.view(chain_id, seaport.SEAPORT, seaport.get_order_hash_call(components))
        if not raw or raw[:66].lower() != local.lower():
            return None, "my order encoding does not match the Seaport contract's hash, so I did not sign"
        return seaport.sign(components, acct.key, chain_id), None

    # ---- listing ----------------------------------------------------------------------

    def list_one(self, chain_id, acct, contract, token_id, std, price_wei, slug, days, managed):
        """List one NFT for a native-coin price. Returns (status, detail, entry). Status: listed, would, failed."""
        chain_slug = opensea.CHAIN_TO_SLUG[chain_id]
        info, error = self.seaport_ready(chain_id)
        if error:
            return "failed", error, None
        try:
            fees = self.collection_fees(slug)
        except opensea.OpenSeaError as exc:
            return "retry", str(exc), None
        if not fees:
            return "failed", "OpenSea gave no fee details for this collection, so I will not guess them", None
        if not self.owns(chain_id, contract, token_id, acct.address, std):
            return "failed", "this wallet does not hold it", None
        conduit = info["conduit"]
        approved = self.view_int(chain_id, contract, seaport.approved_call(acct.address, conduit))
        if not approved:
            if self.dry:
                pass
            else:
                txh, ok = self.send_native_tx(chain_id, acct, contract, seaport.approval_call(conduit))
                if not ok:
                    return "failed", "the approval transaction did not confirm", None
        counter = self.view_int(chain_id, seaport.SEAPORT, seaport.get_counter_call(acct.address))
        if counter is None:
            return "failed", "could not read the wallet's Seaport counter", None
        components = seaport.build_listing(acct.address, contract, token_id, std, price_wei, fees, seaport.OPENSEA_CONDUIT_KEY, counter, days=days)
        signature, error = self.prepare_order(chain_id, acct, components)
        if error:
            return "failed", error, None
        entry = {
            "chain": chain_id,
            "contract": contract.lower(),
            "token": str(token_id),
            "std": int(std),
            "wallet": acct.address.lower(),
            "price": str(price_wei),
            "net": components["consideration"][0]["startAmount"],
            "hash": "",
            "components": components,
            "managed": bool(managed),
            "slug": slug,
            "ends": int(components["endTime"]),
        }
        if self.dry:
            return "would", fmt_native(price_wei, chain_id), entry
        try:
            entry["hash"] = self.opensea.post_order(chain_slug, "listings", seaport.api_parameters(components), signature)
        except opensea.OpenSeaError as exc:
            passing = exc.status is None or exc.status == 429 or exc.status >= 500
            return ("retry" if passing else "failed"), f"OpenSea refused the listing: {exc}", None
        return "listed", fmt_native(price_wei, chain_id), entry

    def cancel_entry(self, entry):
        """Cancel a listing this bot made. The on-chain cancel is what truly stops it; OpenSea is told too. Returns (ok, detail)."""
        chain_id = int(entry["chain"])
        acct = self.account_for(entry["wallet"])
        if not acct:
            return False, "that wallet is not one of your minters"
        if self.dry:
            return True, "dry run"
        try:
            txh, ok = self.send_native_tx(chain_id, acct, seaport.SEAPORT, seaport.cancel_call([entry["components"]]))
        except Exception as exc:
            return False, f"{type(exc).__name__}: {str(exc)[:100]}"
        if not ok:
            return False, "the cancel transaction did not confirm"
        if entry.get("hash"):
            try:
                self.opensea.cancel_order(opensea.CHAIN_TO_SLUG[chain_id], entry["hash"])
            except Exception:
                pass
        return True, txh

    def entry_key(self, entry):
        return f"{entry['chain']}:{entry['contract']}:{entry['token']}"

    # ---- plan / confirm plumbing ------------------------------------------------------

    def market_set(self, kind, plan):
        with self._market_lock:
            if self.market_plan and self.market_plan["state"] in ("running", "stopping"):
                return False
            plan.update({"kind": kind, "state": "planned", "created": time.time()})
            self.market_plan = plan
        return True

    def market_confirm(self, kind, runner):
        with self._market_lock:
            plan = self.market_plan
            if not plan or plan["kind"] != kind or plan["state"] != "planned":
                return f"There is no {kind} waiting for confirmation. Create one with /{kind} first."
            if time.time() - plan["created"] > SEND_PLAN_SECONDS:
                self.market_plan = None
                return f"⌛ That {kind} expired. Send the /{kind} command again."
            plan["state"] = "running"
        threading.Thread(target=runner, args=(plan,), daemon=True).start()
        mode = " (dry run, nothing will be sent)" if self.dry else ""
        return f"✅ Working on it{mode}. I will message you when it is done."

    def market_cancel(self, kind):
        with self._market_lock:
            plan = self.market_plan
            if not plan or plan["kind"] != kind:
                return "Nothing is waiting."
            if plan["state"] == "planned":
                self.market_plan = None
                return "🛑 Cancelled."
            plan["state"] = "stopping"
            plan["cancel"].set()
        return "🛑 Stopping. Anything already sent cannot be undone."

    def market_done(self, plan):
        with self._market_lock:
            if self.market_plan is plan:
                self.market_plan = None

    def market_busy(self):
        plan = self.market_plan
        return bool(plan and plan["state"] in ("running", "stopping"))

    # ---- /list ------------------------------------------------------------------------

    def resolve_nft(self, link, wallet_spec):
        """(info, error) for an OpenSea item link and the minter that holds it."""
        parsed = opensea.parse_item_link(link)
        if not parsed:
            return None, "I need an OpenSea item link like opensea.io/item/robinhood/0xCONTRACT/123."
        slug, contract, token_id = parsed
        chain_id = opensea.SLUG_TO_CHAIN.get(slug)
        if chain_id is None or chain_id not in self.chains:
            return None, f"That link is on \"{slug}\", which this bot is not running."
        accounts, error = self.choose_minters(wallet_spec)
        if error:
            return None, error.lstrip("⚠️ ")
        for acct in accounts:
            std = self.detect_standard(chain_id, contract, acct.address)
            if std and self.owns(chain_id, contract, token_id, acct.address, std):
                return {"chain": chain_id, "contract": contract, "token": token_id, "std": std, "acct": acct}, None
        return None, "None of the chosen wallets holds that NFT."

    def list_command(self, args):
        args = list(args)
        usage = (
            "✏️ Usage: /list OPENSEA_ITEM_LINK PRICE [wallets=1,3] [days=14]\n"
            "PRICE is an amount in the chain's coin (0.05) or a multiple of the top offer (2x).\n"
            "Lists an NFT one of your minters holds. A multiple is also repriced upward by auto-listing when it is on.\n"
            "I show the price and what you receive after fees. Then /list confirm within 2 minutes, or /list cancel."
        )
        if not args:
            return usage
        word = args[0].lower()
        if word == "confirm":
            return self.market_confirm("list", self.run_list)
        if word == "cancel":
            return self.market_cancel("list")
        missing = self.opensea_missing()
        if missing:
            return missing
        if self.market_busy():
            return "Another sell or offer job is running. Wait for it or send /list cancel."
        wallet_spec = self.pull_option(args, ("wallets=", "wallet=", "w="))
        days_spec = self.pull_option(args, ("days=",))
        try:
            days = int(days_spec) if days_spec else int(self.setting("autolist_days"))
        except ValueError:
            return "⚠️ days= must be a whole number."
        if not 1 <= days <= 180:
            return "⚠️ days= must be between 1 and 180."
        parsed = opensea.parse_item_link(" ".join(args))
        if not parsed:
            return "⚠️ I need an OpenSea item link.\n\n" + usage
        price_text = next((a for a in args if not a.lower().startswith(("http", "opensea")) and parse_price_spec(a)[0]), None)
        if price_text is None:
            return "⚠️ I need a price, like 0.05 or 2x.\n\n" + usage
        kind, value = parse_price_spec(price_text)
        nft, error = self.resolve_nft(" ".join(args), wallet_spec)
        if error:
            return f"⚠️ {error}"
        chain_id = nft["chain"]
        try:
            collection, name = self.opensea.collection_slug(opensea.CHAIN_TO_SLUG[chain_id], nft["contract"], nft["token"])
            if not collection:
                return "⚠️ OpenSea does not know this collection yet, so I cannot list it."
            if kind == "multiple":
                offer = self.opensea.best_offer(collection, nft["token"])
                if not offer:
                    return "📭 There is no offer on it to base a multiple on. Give a price instead, like 0.05."
                price_wei = int(offer["price_wei"] * Decimal(str(value)))
            else:
                price_wei = to_wei(value)
            fees = self.collection_fees(collection)
        except opensea.OpenSeaError as exc:
            return f"⚠️ OpenSea said no: {exc}"
        if not fees:
            return "⚠️ OpenSea gave no fee details for this collection, so I will not guess them."
        minimum = to_wei(self.setting("autolist_minimum"))
        if price_wei < minimum:
            return f"⚠️ That is below your autolist_minimum of {fmt_native(minimum, chain_id)}."
        net = price_wei - sum(a for _r, a in seaport.split_fees(price_wei, fees))
        plan = {
            "actions": [
                {
                    "chain": chain_id,
                    "contract": nft["contract"],
                    "token": nft["token"],
                    "std": nft["std"],
                    "wallet": nft["acct"].address.lower(),
                    "price": price_wei,
                    "slug": collection,
                    "days": days,
                    "managed": kind == "multiple",
                    "name": name or "",
                }
            ],
            "cancel": threading.Event(),
        }
        if not self.market_set("list", plan):
            return "Another sell or offer job is running."
        return (
            f"🏷 List on {self.chains[chain_id].name}\n"
            f"NFT: {name or 'token'} #{nft['token']}\nCA: {nft['contract']}\n"
            f"Price: {fmt_native(price_wei, chain_id)}   You receive about {fmt_native(net, chain_id)} after fees\n"
            f"From: 👛 {self.minters.index(nft['acct']) + 1}. {short_address(nft['acct'].address)}   Lasts {days} days\n"
            + ("Priced as a multiple of the top offer, so auto-listing may raise it later.\n" if kind == "multiple" else "")
            + ("🧪 Dry run is on: nothing will be sent.\n" if self.dry else "")
            + "\nThe first listing from a wallet for a collection also sends one approval transaction (gas)."
            + "\nSend /list confirm within 2 minutes, or /list cancel."
        )

    def run_list(self, plan):
        lines = []
        try:
            for act in plan["actions"]:
                if plan["cancel"].is_set():
                    lines.append("🛑 stopped")
                    break
                acct = self.account_for(act["wallet"])
                try:
                    status, detail, entry = self.list_one(act["chain"], acct, act["contract"], act["token"], act["std"], act["price"], act["slug"], act["days"], act["managed"])
                except Exception as exc:
                    status, detail, entry = "failed", f"{type(exc).__name__}: {str(exc)[:100]}", None
                icon = {"listed": "✅", "would": "🧪", "failed": "❌", "retry": "❌"}[status]
                label = {"listed": "listed at", "would": "would list at"}.get(status, "")
                lines.append(f"{icon} {code(act['contract'][:10] + '…')} #{esc(act['token'])} {label} {esc(detail)}".replace("  ", " "))
                if status == "listed" and entry:
                    self.autolist[self.entry_key(entry)] = entry
                    self._save()
            ok = any(l.startswith(("✅", "🧪")) for l in lines)
            self.alert(
                f"List finished: {len(lines)} item(s)",
                card(f"{'✅' if ok else '❌'} List · {len(plan['actions'])} item(s)") + "\n" + "\n".join(lines),
            )
        finally:
            self.market_done(plan)

    # ---- /unlist ----------------------------------------------------------------------

    def unlist_command(self, args):
        args = list(args)
        usage = (
            "✏️ Usage: /unlist OPENSEA_ITEM_LINK  or  /unlist all\n"
            "Cancels listings this bot made. It sends a cancel transaction (gas), because that is what really stops a listing.\n"
            "Then /unlist confirm within 2 minutes, or /unlist cancel."
        )
        if not args:
            if not self.autolist:
                return "The bot has no listings of its own right now.\n\n" + usage
            return self.listings_text() + "\n\n" + usage
        word = args[0].lower()
        if word == "confirm":
            return self.market_confirm("unlist", self.run_unlist)
        if word == "cancel":
            return self.market_cancel("unlist")
        if self.market_busy():
            return "Another sell or offer job is running."
        if word == "all":
            targets = [e for e in self.autolist.values() if e.get("hash")]
        else:
            parsed = opensea.parse_item_link(" ".join(args))
            if not parsed:
                return "⚠️ I need an OpenSea item link, or the word all.\n\n" + usage
            slug, contract, token_id = parsed
            chain_id = opensea.SLUG_TO_CHAIN.get(slug)
            entry = self.autolist.get(f"{chain_id}:{contract}:{token_id}")
            targets = [entry] if entry and entry.get("hash") else []
        if not targets:
            return "📭 I have no active listing of mine for that."
        plan = {"targets": targets, "cancel": threading.Event()}
        if not self.market_set("unlist", plan):
            return "Another sell or offer job is running."
        return f"🗑 Cancel {len(targets)} listing(s)? Each sends a cancel transaction.\nSend /unlist confirm within 2 minutes, or /unlist cancel."

    def run_unlist(self, plan):
        lines = []
        try:
            for entry in plan["targets"]:
                ok, detail = self.cancel_entry(entry)
                lines.append(f"{'✅' if ok else '❌'} {code(entry['contract'][:10] + '…')} #{esc(entry['token'])} {'cancelled' if ok else esc(detail)}")
                if ok:
                    self.autolist.pop(self.entry_key(entry), None)
                    self._save()
            self.alert(f"Unlist finished: {len(lines)}", card(f"🗑 Unlist · {len(lines)} listing(s)") + "\n" + "\n".join(lines))
        finally:
            self.market_done(plan)

    def listings_text(self):
        rows = [e for e in self.autolist.values() if e.get("hash")]
        lines = [f"🏷 {len(rows)} listing(s) the bot manages:"]
        for entry in rows[:25]:
            lines.append(f"• {short_address(entry['contract'])} #{entry['token']} at {fmt_native(int(entry['price']), int(entry['chain']))}{' (auto)' if entry.get('managed') else ''}")
        if len(rows) > 25:
            lines.append(f"…and {len(rows) - 25} more")
        return "\n".join(lines)

    # ---- /sellall ---------------------------------------------------------------------

    def sellall_command(self, args):
        args = list(args)
        usage = (
            "✏️ Usage: /sellall PRICE [wallets=1,3] [chain=robinhood] [collection=0xabc…]\n"
            "PRICE is a multiple of each NFT's top offer (2x). NFTs with no offer are skipped. A fixed amount works too and lists them all at that price.\n"
            f"Lists up to {MAX_PLAN_ITEMS} NFTs your minters hold (the ones the bot recorded). Run it again for the rest.\n"
            "Then /sellall confirm within 2 minutes, or /sellall cancel."
        )
        if not args:
            return usage
        word = args[0].lower()
        if word == "confirm":
            return self.market_confirm("sellall", self.run_sellall)
        if word == "cancel":
            return self.market_cancel("sellall")
        missing = self.opensea_missing()
        if missing:
            return missing
        if self.market_busy():
            return "Another sell or offer job is running."
        wallet_spec = self.pull_option(args, ("wallets=", "wallet=", "w="))
        chain_word = self.pull_option(args, ("chain=", "c="))
        collection_word = self.pull_option(args, ("collection=",))
        kind, value = parse_price_spec(args[0])
        if not kind:
            return "⚠️ I need a price like 2x or 0.05.\n\n" + usage
        accounts, error = self.choose_minters(wallet_spec)
        if error:
            return error
        chain_filter = None
        if chain_word is not None:
            chain_filter = self.chain_from_word(chain_word)
            if chain_filter is None:
                return f"⚠️ I do not know the chain \"{chain_word}\"."
        collection_filter = (valid_address(collection_word) if collection_word else None)
        wanted = {a.address.lower() for a in accounts}
        days = int(self.setting("autolist_days"))
        minimum = to_wei(self.setting("autolist_minimum"))
        actions, no_offer, unknown, more = [], 0, 0, 0
        for key, entry in list(self.holdings.items()):
            chain_s, contract, wallet = key.split(":")
            chain_id = int(chain_s)
            if wallet not in wanted or chain_id not in self.chains or chain_id not in opensea.CHAIN_TO_SLUG:
                continue
            if chain_filter and chain_id != chain_filter:
                continue
            if collection_filter and contract != collection_filter:
                continue
            try:
                slug = self.collection_slug_for(chain_id, contract)
            except opensea.OpenSeaError as exc:
                return f"⚠️ OpenSea said no: {exc}"
            if not slug:
                unknown += len(entry["ids"])
                continue
            for token in entry["ids"]:
                if len(actions) >= MAX_PLAN_ITEMS:
                    more += 1
                    continue
                if not self.owns(chain_id, contract, token, wallet, entry["std"]):
                    continue
                try:
                    if kind == "multiple":
                        offer = self.opensea.best_offer(slug, token)
                        if not offer:
                            no_offer += 1
                            continue
                        price_wei = int(offer["price_wei"] * Decimal(str(value)))
                    else:
                        price_wei = to_wei(value)
                except opensea.OpenSeaError as exc:
                    return f"⚠️ OpenSea said no: {exc}"
                price_wei = max(price_wei, minimum)
                actions.append({"chain": chain_id, "contract": contract, "token": token, "std": entry["std"], "wallet": wallet, "price": price_wei, "slug": slug, "days": days, "managed": kind == "multiple", "name": ""})
        if not actions:
            extra = []
            if no_offer:
                extra.append(f"{no_offer} have no offer")
            if unknown:
                extra.append(f"{unknown} are on collections OpenSea does not know yet")
            return "📭 Nothing to list" + (": " + ", ".join(extra) + "." if extra else ". I only know NFTs the bot minted or bought.")
        plan = {"actions": actions, "cancel": threading.Event()}
        if not self.market_set("sellall", plan):
            return "Another sell or offer job is running."
        total = sum(a["price"] for a in actions)
        lines = [f"🏷 List {len(actions)} NFT(s), {fmt_native(total, actions[0]['chain'])} in total at those prices"]
        if no_offer:
            lines.append(f"Skipped {no_offer} with no offer")
        if unknown:
            lines.append(f"Skipped {unknown} on collections OpenSea does not know yet")
        if more:
            lines.append(f"{more} more are not in this batch. Run /sellall again after.")
        if self.dry:
            lines.append("🧪 Dry run is on: nothing will be sent.")
        lines.append("Each wallet's first listing in a collection also sends one approval transaction (gas).")
        lines.append("Send /sellall confirm within 2 minutes, or /sellall cancel.")
        return "\n".join(lines)

    def run_sellall(self, plan):
        lines = []
        done = failed = 0
        try:
            for act in plan["actions"]:
                if plan["cancel"].is_set():
                    lines.append("🛑 stopped")
                    break
                acct = self.account_for(act["wallet"])
                try:
                    status, detail, entry = self.list_one(act["chain"], acct, act["contract"], act["token"], act["std"], act["price"], act["slug"], act["days"], act["managed"])
                except Exception as exc:
                    status, detail, entry = "failed", f"{type(exc).__name__}: {str(exc)[:100]}", None
                if status in ("listed", "would"):
                    done += 1
                    if entry and status == "listed":
                        self.autolist[self.entry_key(entry)] = entry
                        self._save()
                else:
                    failed += 1
                    lines.append(f"❌ {short_address(act['contract'])} #{esc(act['token'])}: {esc(detail)}")
                time.sleep(0.3)
            head = f"{'🧪 Would list' if self.dry else '✅ Listed'} {done} of {len(plan['actions'])}"
            self.alert(head, card(head) + ("\n" + "\n".join(lines[:15]) if lines else ""))
        finally:
            self.market_done(plan)

    # ---- /offer -----------------------------------------------------------------------

    def weth_for(self, chain_id, offer_token=None):
        env_value = env(f"WETH_{chain_id}")
        return valid_address(env_value) if env_value else (valid_address(offer_token) if offer_token else WETH.get(chain_id))

    def offer_command(self, args):
        args = list(args)
        usage = (
            "✏️ Usage: /offer OPENSEA_ITEM_LINK PRICE [wallets=1] [hours=24]\n"
            "Bids PRICE (in wrapped coin, e.g. WETH) on one NFT. The wallet needs that much wrapped coin. I send one approval transaction the first time.\n"
            f"PRICE can be at most {fmt_native(self.max_buy, 4663)} (MAX_BUY_ETH).\n"
            "Then /offer confirm within 2 minutes, or /offer cancel."
        )
        if not args:
            return usage
        word = args[0].lower()
        if word == "confirm":
            return self.market_confirm("offer", self.run_offer)
        if word == "cancel":
            return self.market_cancel("offer")
        missing = self.opensea_missing()
        if missing:
            return missing
        if self.market_busy():
            return "Another sell or offer job is running."
        wallet_spec = self.pull_option(args, ("wallets=", "wallet=", "w="))
        hours_spec = self.pull_option(args, ("hours=",))
        try:
            hours = int(hours_spec) if hours_spec else 24
        except ValueError:
            return "⚠️ hours= must be a whole number."
        if not 1 <= hours <= 24 * 30:
            return "⚠️ hours= must be between 1 and 720."
        parsed = opensea.parse_item_link(" ".join(args))
        if not parsed:
            return "⚠️ I need an OpenSea item link.\n\n" + usage
        price_text = next((a for a in args if not a.lower().startswith(("http", "opensea")) and parse_price_spec(a)[0] == "price"), None)
        if price_text is None:
            return "⚠️ I need a price like 0.01.\n\n" + usage
        price_wei = to_wei(parse_price_spec(price_text)[1])
        if price_wei > self.max_buy:
            return f"⚠️ That is above your limit of {fmt_native(self.max_buy, 4663)}. Raise MAX_BUY_ETH in Railway to allow it."
        slug, contract, token_id = parsed
        chain_id = opensea.SLUG_TO_CHAIN.get(slug)
        if chain_id is None or chain_id not in self.chains:
            return f"⚠️ That link is on \"{slug}\", which this bot is not running."
        accounts, error = self.choose_minters(wallet_spec or "1")
        if error:
            return error
        acct = accounts[0]
        try:
            collection, name = self.opensea.collection_slug(slug, contract, token_id)
            if not collection:
                return "⚠️ OpenSea does not know this collection yet."
            top = self.opensea.best_offer(collection, token_id)
            fees = self.collection_fees(collection)
        except opensea.OpenSeaError as exc:
            return f"⚠️ OpenSea said no: {exc}"
        weth = self.weth_for(chain_id, (top or {}).get("token"))
        if not weth:
            return f"⚠️ I do not know the wrapped-coin address on this chain. Set WETH_{chain_id} in Railway."
        std = self.detect_standard(chain_id, contract, acct.address) or 721
        plan = {
            "chain": chain_id,
            "wallet": acct.address.lower(),
            "contract": contract,
            "token": token_id,
            "std": std,
            "price": price_wei,
            "slug": collection,
            "weth": weth,
            "hours": hours,
            "fees": fees,
            "cancel": threading.Event(),
        }
        if not self.market_set("offer", plan):
            return "Another sell or offer job is running."
        held = self.view_int(chain_id, weth, BALANCE_OF_ERC20 + addr_word(acct.address))
        return (
            f"🤝 Offer on {self.chains[chain_id].name}\n"
            f"NFT: {name or 'token'} #{token_id}\nCA: {contract}\n"
            f"Offer: {fmt_native(price_wei, chain_id)} (wrapped)   Lasts {hours}h\n"
            f"Top offer now: {fmt_native(top['price_wei'], chain_id) if top else 'none'}\n"
            f"From: 👛 {self.minters.index(acct) + 1}. {short_address(acct.address)} holds {fmt_native(held, chain_id) if held is not None else 'unknown'} wrapped\n"
            + ("🧪 Dry run is on: nothing will be sent.\n" if self.dry else "")
            + "\nSend /offer confirm within 2 minutes, or /offer cancel."
        )

    def run_offer(self, plan):
        chain_id = plan["chain"]
        try:
            acct = self.account_for(plan["wallet"])
            detail, ok = self.offer_one(chain_id, acct, plan)
            self.alert(f"Offer: {detail}", card(f"{'✅' if ok else '❌'} Offer · {self.chains[chain_id].name}", ("NFT", code(plan["contract"]) + f" #{plan['token']}"), ("Result", esc(detail))))
        except Exception as exc:
            self.alert(f"Offer failed: {type(exc).__name__}")
        finally:
            self.market_done(plan)

    def offer_one(self, chain_id, acct, plan):
        info, error = self.seaport_ready(chain_id)
        if error:
            return error, False
        held = self.view_int(chain_id, plan["weth"], BALANCE_OF_ERC20 + addr_word(acct.address))
        if held is None or held < plan["price"]:
            return f"the wallet holds {fmt_native(held or 0, chain_id)} wrapped, not enough for this offer", False
        conduit = info["conduit"]
        allowance = self.view_int(chain_id, plan["weth"], seaport.erc20_allowance_call(acct.address, conduit)) or 0
        if allowance < plan["price"] and not self.dry:
            txh, ok = self.send_native_tx(chain_id, acct, plan["weth"], seaport.erc20_approve_call(conduit))
            if not ok:
                return "the approval transaction did not confirm", False
        counter = self.view_int(chain_id, seaport.SEAPORT, seaport.get_counter_call(acct.address))
        if counter is None:
            return "could not read the wallet's Seaport counter", False
        components = seaport.build_offer(acct.address, plan["contract"], plan["token"], plan["std"], plan["price"], plan["weth"], plan["fees"], seaport.OPENSEA_CONDUIT_KEY, counter, hours=plan["hours"])
        signature, error = self.prepare_order(chain_id, acct, components)
        if error:
            return error, False
        if self.dry:
            return f"would offer {fmt_native(plan['price'], chain_id)}", True
        try:
            order = self.opensea.post_order(opensea.CHAIN_TO_SLUG[chain_id], "offers", seaport.api_parameters(components), signature)
        except opensea.OpenSeaError as exc:
            return f"OpenSea refused the offer: {exc}", False
        return f"offered {fmt_native(plan['price'], chain_id)}" + (f" ({order[:12]}…)" if order else ""), True

    # ---- auto-listing -----------------------------------------------------------------

    def autolist_cycle(self):
        """One pass: list new holdings, raise listings when the top offer rose, forget NFTs that left. Never lowers a price."""
        if not self.opensea or not self.setting("autolist"):
            return []
        multiple = Decimal(str(self.setting("autolist_multiple")))
        step = Decimal(str(self.setting("autolist_step"))) / 100
        minimum = to_wei(self.setting("autolist_minimum"))
        days = int(self.setting("autolist_days"))
        lines = []
        for key, held in list(self.holdings.items()):
            chain_s, contract, wallet = key.split(":")
            chain_id = int(chain_s)
            if chain_id not in self.chains or chain_id not in opensea.CHAIN_TO_SLUG or not self.account_for(wallet):
                continue
            for token in held["ids"]:
                self.autolist.setdefault(
                    f"{chain_id}:{contract}:{token}",
                    {"chain": chain_id, "contract": contract, "token": str(token), "std": held["std"], "wallet": wallet, "price": "0", "hash": "", "managed": True},
                )
        for key, entry in list(self.autolist.items()):
            if not entry.get("managed"):
                continue
            chain_id = int(entry["chain"])
            if chain_id not in self.chains:
                continue
            acct = self.account_for(entry["wallet"])
            if not acct:
                continue
            try:
                if not self.owns(chain_id, entry["contract"], entry["token"], acct.address, entry["std"]):
                    sold = int(entry.get("price") or 0) > 0
                    self.autolist.pop(key, None)
                    if sold:
                        self.ledger_add("sale", chain_id, entry["wallet"], entry["contract"], int(entry.get("net") or 0), entry["token"], "estimated, it left while listed")
                        lines.append(f"💰 {short_address(entry['contract'])} #{entry['token']} left while listed at {fmt_native(int(entry['price']), chain_id)}")
                    self._save()
                    continue
                slug = self.collection_slug_for(chain_id, entry["contract"])
                if not slug:
                    continue
                offer = self.opensea.best_offer(slug, entry["token"])
                if not offer:
                    continue
                target = max(int(offer["price_wei"] * multiple), minimum)
                current = int(entry.get("price") or 0)
                if current and target < int(current * (1 + step)):
                    continue
                if current and entry.get("hash"):
                    ok, detail = self.cancel_entry(entry)
                    if not ok:
                        lines.append(f"❌ could not cancel the old listing of {short_address(entry['contract'])} #{entry['token']}: {detail}")
                        continue
                status, detail, new = self.list_one(chain_id, acct, entry["contract"], entry["token"], entry["std"], target, slug, days, True)
                if status == "listed":
                    self.autolist[key] = new
                    self._save()
                    verb = "raised to" if current else "listed at"
                    lines.append(f"🏷 {short_address(entry['contract'])} #{entry['token']} {verb} {detail}")
                elif status == "would":
                    lines.append(f"🧪 would list {short_address(entry['contract'])} #{entry['token']} at {detail}")
                else:
                    if current:
                        # The old listing is already cancelled, so start again from nothing next time.
                        entry["price"], entry["hash"] = "0", ""
                        self._save()
                    lines.append(f"❌ {short_address(entry['contract'])} #{entry['token']}: {detail}")
            except opensea.OpenSeaError as exc:
                self.log(f"autolist {exc}")
                if exc.status == 429:
                    break
            except Exception as exc:
                self.log(f"autolist {type(exc).__name__}: {exc}")
            time.sleep(0.2)
        return lines

    def autolist_loop(self):
        time.sleep(30)
        while True:
            try:
                lines = self.autolist_cycle()
                if lines:
                    self.alert("Auto-list: " + "; ".join(lines[:3]), card("🏷 Auto-list") + "\n" + "\n".join(esc(l) for l in lines[:20]))
            except Exception as exc:
                self.log(f"autolist loop {type(exc).__name__}: {exc}")
            time.sleep(max(15, int(self.setting("autolist_seconds"))))

    # ---- profit and loss --------------------------------------------------------------

    def pnl_command(self, args):
        args = list(args)
        wallet_spec = self.pull_option(args, ("wallets=", "wallet=", "w="))
        accounts, error = self.choose_minters(wallet_spec)
        if error:
            return error
        wanted = {a.address.lower() for a in accounts}
        rows = [e for e in self.ledger if e.get("wallet") in wanted]
        if not rows and not self.autolist:
            return "📒 Nothing recorded yet. I count mints and buys from now on."
        lines = ["📒 Profit and loss (from what the bot recorded)"]
        for chain_id in sorted({int(e["chain"]) for e in rows} | {int(e["chain"]) for e in self.autolist.values()}):
            spent = {k: sum(int(e["wei"]) for e in rows if int(e["chain"]) == chain_id and e["kind"] == k) for k in ("mint", "buy")}
            sales = sum(int(e["wei"]) for e in rows if int(e["chain"]) == chain_id and e["kind"] == "sale")
            open_listings = [e for e in self.autolist.values() if int(e["chain"]) == chain_id and e.get("hash") and e["wallet"] in wanted]
            listed_value = sum(int(e.get("net") or 0) for e in open_listings)
            net = sales - spent["mint"] - spent["buy"]
            sign = "+" if net >= 0 else "-"
            lines.append(
                f"\n{self.chains[chain_id].name}\n"
                f"  Minted: {fmt_native(spent['mint'], chain_id)}\n"
                f"  Bought: {fmt_native(spent['buy'], chain_id)}\n"
                f"  Sold (estimated): {fmt_native(sales, chain_id)}\n"
                f"  Net so far: {sign}{fmt_native(abs(net), chain_id)}\n"
                f"  Open listings: {len(open_listings)} worth {fmt_native(listed_value, chain_id)} after fees if they sell"
            )
        lines.append("\nSales are estimated: an NFT counts as sold when it leaves a wallet while the bot had it listed. Mint costs include gas.")
        return "\n".join(lines)
