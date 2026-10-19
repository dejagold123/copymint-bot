"""OpenSea purchases: /buy and /sweep. Mixed into Bot, which supplies self.opensea, wallets, RPC and sending."""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from eth_utils import to_checksum_address

from helpers import *  # noqa: F401,F403
import opensea


class TradeMixin:
    def opensea_missing(self):
        if self.opensea:
            return None
        return (
            "🔑 OpenSea is not set up. Add OPENSEA_API_KEY in your Railway variables "
            "(get a key from your OpenSea developer settings), redeploy, then try again."
        )

    def read_amount(self, text):
        """A positive coin amount such as 0.01 as wei, or None."""
        try:
            value = Decimal(str(text))
        except Exception:
            return None
        if not value.is_finite() or value <= 0 or value > 1000:
            return None
        return to_wei(value)

    def buy_command(self, args):
        args = list(args)
        usage = (
            "✏️ Usage: /buy OPENSEA_ITEM_LINK [wallets=1,3] [max=0.01]\n"
            "Buys that one NFT at its current lowest listing price, in the chain's own coin.\n"
            "wallets=1,3 picks the buyers (numbers as /minters lists them). With several wallets, I try them in order and stop at the first that buys.\n"
            f"max= sets the highest price I will pay. It can only go below the limit of {fmt_native(self.max_buy, 4663)} (MAX_BUY_ETH in Railway).\n"
            "I show you the price first. Then /buy confirm within 2 minutes, or /buy cancel.\n"
            "Every purchase is simulated before it is sent, and skipped if it would not give the wallet the NFT."
        )
        if not args:
            plan = self.buy_plan
            if plan:
                return f"State: {plan['state']}\n" + self.buy_plan_text(plan)
            return usage
        word0 = args[0].lower()
        if word0 == "cancel":
            with self._buy_lock:
                plan = self.buy_plan
                if not plan:
                    return "Nothing is waiting to be bought."
                if plan["state"] == "planned":
                    self.buy_plan = None
                    return "🛑 Cancelled."
                plan["state"] = "stopping"
                plan["cancel"].set()
            return "🛑 Stopping. A purchase already sent cannot be undone."
        if word0 == "confirm":
            with self._buy_lock:
                plan = self.buy_plan
                if not plan or plan["state"] != "planned":
                    return "There is no purchase waiting for confirmation. Create one with /buy first."
                if time.time() - plan["created"] > SEND_PLAN_SECONDS:
                    self.buy_plan = None
                    return "⌛ That purchase expired. Send the /buy command again."
                plan["state"] = "running"
            threading.Thread(target=self.run_buy, args=(plan,), daemon=True).start()
            mode = " (dry run, nothing will be sent)" if self.dry else ""
            return f"✅ Buying{mode}. I will message you when it is done."
        missing = self.opensea_missing()
        if missing:
            return missing
        with self._buy_lock:
            if self.buy_plan and self.buy_plan["state"] in ("running", "stopping"):
                return "A purchase is already running or stopping. Send /buy cancel to stop it."
        wallet_spec = self.pull_option(args, ("wallets=", "wallet=", "w="))
        max_spec = self.pull_option(args, ("max=", "limit="))
        accounts, error = self.choose_minters(wallet_spec)
        if error:
            return error
        max_wei = self.max_buy
        if max_spec is not None:
            wanted = self.read_amount(max_spec)
            if wanted is None:
                return f"⚠️ I could not read max={max_spec} as an amount. Use something like max=0.01."
            if wanted > self.max_buy:
                return f"⚠️ max={max_spec} is above your limit of {fmt_native(self.max_buy, 4663)}. Raise MAX_BUY_ETH in Railway to allow it."
            max_wei = wanted
        parsed = opensea.parse_item_link(" ".join(args))
        if not parsed:
            return "⚠️ I need an OpenSea item link like opensea.io/item/robinhood/0xCONTRACT/123.\n\n" + usage
        slug, contract, token_id = parsed
        chain_id = opensea.SLUG_TO_CHAIN.get(slug)
        if chain_id is None or chain_id not in self.chains:
            enabled = ", ".join(opensea.CHAIN_TO_SLUG.get(c, str(c)) for c in self.chains)
            return f"⚠️ That link is on \"{slug}\", which this bot is not running. Enabled: {enabled}."
        try:
            collection, name = self.opensea.collection_slug(slug, contract, token_id)
            listing = self.opensea.best_listing(slug, collection, contract, token_id)
        except opensea.OpenSeaError as exc:
            return f"⚠️ OpenSea said no: {exc}"
        if not listing:
            return "📭 That NFT has no active listing on OpenSea right now, so there is nothing to buy."
        if listing["native"] is False:
            return "⚠️ That listing is priced in a token, not the chain's own coin. I only buy native-coin listings for now."
        if listing["quantity"] > 1:
            return "⚠️ That listing sells more than one copy. I only buy single-item listings for now."
        if listing["price_wei"] > max_wei:
            return (
                f"💸 It is listed at {fmt_native(listing['price_wei'], chain_id)}, above your limit of {fmt_native(max_wei, chain_id)}. "
                "Add a higher max= (up to MAX_BUY_ETH) if you want it."
            )
        plan = {
            "state": "planned",
            "created": time.time(),
            "chain": chain_id,
            "slug": slug,
            "contract": contract,
            "token": token_id,
            "name": name or "",
            "collection": collection or "",
            "listing": listing,
            "max": max_wei,
            "wallets": [a.address.lower() for a in accounts],
            "cancel": threading.Event(),
        }
        with self._buy_lock:
            self.buy_plan = plan
        return self.buy_plan_text(plan) + "\n\nSend /buy confirm within 2 minutes to buy, or /buy cancel."

    def buy_plan_text(self, plan):
        chain_id = plan["chain"]
        lines = [
            f"🛒 Buy on {self.chains[chain_id].name}",
            f"NFT: {plan['name'] or 'token ' + str(plan['token'])} (#{plan['token']})",
            f"CA: {plan['contract']}",
            f"Price: {fmt_native(plan['listing']['price_wei'], chain_id)}   (highest I will pay: {fmt_native(plan['max'], chain_id)})",
            "Buyers, in order:",
        ]
        for number, acct in enumerate(self.minters, 1):
            if acct.address.lower() in plan["wallets"]:
                try:
                    have = fmt_native(self.balance(chain_id, "native", acct.address), chain_id)
                except Exception:
                    have = "balance unavailable"
                lines.append(f"  👛 {number}. {short_address(acct.address)} has {have}")
        if self.dry:
            lines.append("🧪 Dry run is on: nothing will be sent.")
        return "\n".join(lines)

    def buy_one(self, chain_id, acct, plan, listing):
        """Try to buy the listing with one wallet. Returns (status, detail, txh). Status: bought, would, skipped or failed."""
        slug, contract, token_id = plan["slug"], plan["contract"], plan["token"]
        try:
            to, value, data, function = self.opensea.fulfilment(listing, slug, acct.address)
        except opensea.OpenSeaError as exc:
            # A rate limit, outage or timeout can pass; a 4xx means OpenSea refused this listing.
            passing = exc.status is None or exc.status == 429 or exc.status >= 500
            return ("retry" if passing else "failed"), str(exc), None
        if to.lower() not in SEAPORT_ADDRESSES:
            return "failed", f"OpenSea pointed at an address I do not recognise as Seaport ({short_address(to)})", None
        if value <= 0:
            return "failed", "this listing is not paid in the chain's own coin", None
        if value > plan["max"]:
            return "skipped", f"price is now {fmt_native(value, chain_id)}, above your limit of {fmt_native(plan['max'], chain_id)}", None
        if value > listing["price_wei"] * 105 // 100:
            return "skipped", f"price moved to {fmt_native(value, chain_id)} from {fmt_native(listing['price_wei'], chain_id)}", None
        have = self.balance(chain_id, "native", acct.address)
        if have < value:
            return "skipped", f"has {fmt_native(have, chain_id)}, needs {fmt_native(value, chain_id)} plus gas", None
        sim = self.simulate(chain_id, acct, to, data, value)
        if sim is None:
            info = self._revert or {}
            reason = decode_revert(info.get("data"), info.get("message"))
            return "failed", "the purchase would fail: " + (classify_reason(reason) if reason else "it reverted (the listing may be sold, cancelled or expired)"), None
        if sim == "unsupported":
            return "failed", "your RPC cannot simulate, and I will not send a purchase I cannot check", None
        logs, gas_used = sim
        if not nft_received(logs, contract, acct.address, token_id):
            return "failed", "the simulation did not give this wallet the NFT, so I did not send", None
        gas = int(gas_used * 1.3) + 30_000
        fee, _tip = self.fees(chain_id)
        if have < value + gas * fee:
            return "skipped", f"has {fmt_native(have, chain_id)}, needs about {fmt_native(value + gas * fee, chain_id)} with gas", None
        if self.dry:
            return "would", fmt_native(value, chain_id), None
        with self._copy_lock:
            txh, ok, sent = self.land(chain_id, acct, to, data, value, gas)
        if not ok:
            # No receipt in time does not mean it failed: it may land a moment later. Look once more before deciding.
            try:
                hit = self.receipt_for(chain_id, sent)
            except Exception:
                hit = None
            if hit is None:
                return "pending", f"sent but not confirmed yet, price {fmt_native(value, chain_id)}", txh
            txh, ok = hit
        if not ok:
            return "failed", "the transaction reverted (someone else may have bought it first)", txh
        self.record_holdings_later(chain_id, acct.address, txh)
        return "bought", fmt_native(value, chain_id), txh

    def run_buy(self, plan):
        chain_id = plan["chain"]
        lines, done, waiting = [], False, False
        try:
            for acct in self.minters:
                if acct.address.lower() not in plan["wallets"]:
                    continue
                if plan["cancel"].is_set():
                    lines.append("🛑 stopped")
                    break
                number = self.minters.index(acct) + 1
                try:
                    status, detail, txh = self.buy_one(chain_id, acct, plan, plan["listing"])
                except Exception as exc:
                    status, detail, txh = "failed", f"{type(exc).__name__}: {str(exc)[:120]}", None
                icon = {"bought": "✅", "would": "🧪", "pending": "⏳", "skipped": "⏭", "failed": "❌", "retry": "❌"}[status]
                tail = f" · tx {code(txh)}" if txh else ""
                label = {"bought": "bought for", "would": "would buy for"}.get(status, "")
                lines.append(f"{icon} 👛 {number}. {code(short_address(acct.address))} {label} {esc(detail)}{tail}".replace("  ", " "))
                if status == "pending":
                    # It may still land, so never try another wallet and risk a second copy.
                    waiting = True
                    break
                if status in ("bought", "would"):
                    done = True
                    break
            if waiting:
                title = "Sent, waiting for confirmation"
            else:
                title = "Bought" if done and not self.dry else ("Would buy (dry run)" if done else "Not bought")
            self.alert(
                f"{title}: {plan['contract']} #{plan['token']}",
                card(
                    f"{'✅' if done else ('⏳' if waiting else '❌')} {title} · {self.chains[chain_id].name}",
                    ("NFT", esc(f"{plan['name'] or 'token'} #{plan['token']}")),
                    ("CA", code(plan["contract"])),
                    ("Price", esc(fmt_native(plan["listing"]["price_wei"], chain_id))),
                ) + "\n" + "\n".join(lines),
            )
        finally:
            with self._buy_lock:
                if self.buy_plan is plan:
                    self.buy_plan = None

    def sweep_command(self, args):
        args = list(args)
        usage = (
            "✏️ Usage: /sweep OPENSEA_COLLECTION_LINK max=0.002 [budget=0.5] [wallets=1,3] [chain=robinhood]\n"
            "Watches that collection and buys every listing at or below max= (a price per NFT, in the chain's coin), cheapest first, until you stop it.\n"
            "budget= is the most I may spend in total. When the next buy would pass it, I stop. Without it I run until /sweep stop.\n"
            "wallets=1,3 picks the buyers (numbers as /minters lists them). I rotate through them and skip a wallet that cannot pay.\n"
            "An item link also works instead of a collection link (then the chain comes from the link).\n"
            f"max= can only go up to your limit of {fmt_native(self.max_buy, 4663)} (MAX_BUY_ETH in Railway).\n"
            "Then /sweep confirm to start, /sweep stop to end, and /sweep alone shows progress.\n"
            "A restart of the bot ends a sweep. It never resumes by itself."
        )
        if not args:
            plan = self.sweep_plan
            if plan:
                return f"State: {plan['state']}\n" + self.sweep_plan_text(plan)
            return usage
        word0 = args[0].lower()
        if word0 in ("stop", "cancel"):
            with self._sweep_lock:
                plan = self.sweep_plan
                if not plan:
                    return "No sweep is set up."
                if plan["state"] == "planned":
                    self.sweep_plan = None
                    return "🛑 Cancelled."
            self.sweep_stop.set()
            return "🛑 Stopping the sweep. A purchase already sent cannot be undone. A summary follows."
        if word0 == "confirm":
            with self._sweep_lock:
                plan = self.sweep_plan
                if not plan or plan["state"] != "planned":
                    return "There is no sweep waiting for confirmation. Create one with /sweep first."
                if time.time() - plan["created"] > SEND_PLAN_SECONDS:
                    self.sweep_plan = None
                    return "⌛ That sweep expired. Send the /sweep command again."
                plan["state"] = "running"
                self.sweep_stop.clear()
            threading.Thread(target=self.run_sweep, args=(plan,), daemon=True).start()
            mode = " (dry run, nothing will be sent)" if self.dry else ""
            return f"✅ Sweep started{mode}. I message you for each NFT bought and when it ends. /sweep stop ends it."
        missing = self.opensea_missing()
        if missing:
            return missing
        with self._sweep_lock:
            if self.sweep_plan and self.sweep_plan["state"] == "running":
                return "A sweep is already running. Send /sweep stop first."
        wallet_spec = self.pull_option(args, ("wallets=", "wallet=", "w="))
        chain_word = self.pull_option(args, ("chain=", "c="))
        max_spec = self.pull_option(args, ("max=", "price=", "limit="))
        budget_spec = self.pull_option(args, ("budget=", "total="))
        accounts, error = self.choose_minters(wallet_spec)
        if error:
            return error
        if max_spec is None:
            return "⚠️ I need max= (the highest price per NFT).\n\n" + usage
        max_wei = self.read_amount(max_spec)
        if max_wei is None:
            return f"⚠️ I could not read max={max_spec} as an amount. Use something like max=0.002."
        if max_wei > self.max_buy:
            return f"⚠️ max={max_spec} is above your limit of {fmt_native(self.max_buy, 4663)}. Raise MAX_BUY_ETH in Railway to allow it."
        budget = None
        if budget_spec is not None:
            budget = self.read_amount(budget_spec)
            if budget is None:
                return f"⚠️ I could not read budget={budget_spec} as an amount. Use something like budget=0.5."
            if budget < max_wei:
                return "⚠️ budget= is below max=, so I could not buy even one."
        text = " ".join(args)
        slug_chain, item_contract, item_token = (None, None, None)
        item = opensea.parse_item_link(text)
        chain_id = None
        try:
            if item:
                slug_chain, item_contract, item_token = item
                chain_id = opensea.SLUG_TO_CHAIN.get(slug_chain)
                collection, _name = self.opensea.collection_slug(slug_chain, item_contract, item_token)
            else:
                collection = opensea.parse_collection_link(text)
        except opensea.OpenSeaError as exc:
            return f"⚠️ OpenSea said no: {exc}"
        if not collection:
            return "⚠️ I need an OpenSea collection link like opensea.io/collection/robo-cards.\n\n" + usage
        if chain_word is not None:
            chain_id = self.chain_from_word(chain_word)
            if chain_id is None:
                names = ", ".join(f"{c.name} ({cid})" for cid, c in self.chains.items())
                return f"⚠️ I do not know the chain \"{chain_word}\". Enabled chains: {names}."
        if chain_id is None:
            if len(self.chains) == 1:
                chain_id = next(iter(self.chains))
            elif 4663 in self.chains:
                chain_id = 4663
            else:
                return "⚠️ Add chain=robinhood (or another enabled chain), since a collection link does not say which chain."
        if chain_id not in self.chains or chain_id not in opensea.CHAIN_TO_SLUG:
            return f"⚠️ This bot is not running chain {chain_id} for OpenSea."
        try:
            rows = self.opensea.collection_listings(collection, limit=50)
        except opensea.OpenSeaError as exc:
            return f"⚠️ OpenSea said no: {exc}"
        affordable = [r for r in rows if r["native"] is not False and r["quantity"] == 1 and r["price_wei"] <= max_wei]
        plan = {
            "state": "planned",
            "created": time.time(),
            "chain": chain_id,
            "slug": opensea.CHAIN_TO_SLUG[chain_id],
            "collection": collection,
            "max": max_wei,
            "budget": budget,
            "wallets": [a.address.lower() for a in accounts],
            "seen": set(),
            "later": {},
            "retries": {},
            "bought": 0,
            "spent": 0,
            "tried": 0,
            "error": "",
            "started": None,
        }
        with self._sweep_lock:
            self.sweep_plan = plan
        now = f"Right now {len(affordable)} of the {len(rows)} cheapest listings are at or below your price." if rows else "I see no listings right now."
        return self.sweep_plan_text(plan) + f"\n{now}\n\nSend /sweep confirm within 2 minutes to start, or /sweep stop."

    def sweep_plan_text(self, plan):
        chain_id = plan["chain"]
        budget = fmt_native(plan["budget"], chain_id) if plan["budget"] else "none (runs until /sweep stop)"
        lines = [
            f"🧹 Sweep on {self.chains[chain_id].name}",
            f"Collection: {plan['collection']}",
            f"Buys at or below: {fmt_native(plan['max'], chain_id)} each",
            f"Total budget: {budget}",
            f"Bought so far: {plan['bought']} for {fmt_native(plan['spent'], chain_id)} ({plan['tried']} attempts)",
            "Buyers:",
        ]
        for number, acct in enumerate(self.minters, 1):
            if acct.address.lower() in plan["wallets"]:
                try:
                    have = fmt_native(self.balance(chain_id, "native", acct.address), chain_id)
                except Exception:
                    have = "balance unavailable"
                lines.append(f"  👛 {number}. {short_address(acct.address)} has {have}")
        if plan["error"]:
            lines.append(f"Last problem: {plan['error']}")
        if self.dry:
            lines.append("🧪 Dry run is on: nothing will be sent.")
        return "\n".join(lines)

    def run_sweep(self, plan):
        chain_id = plan["chain"]
        plan["started"] = time.time()
        wallets = [a for a in self.minters if a.address.lower() in plan["wallets"]]
        turn = 0
        failures = 0
        reason = "stopped by you"
        try:
            while not self.sweep_stop.is_set():
                try:
                    rows = self.opensea.collection_listings(plan["collection"], limit=50)
                    failures = 0
                    plan["error"] = ""
                except opensea.OpenSeaError as exc:
                    failures += 1
                    plan["error"] = str(exc)[:160]
                    self.log(f"sweep listings {exc}")
                    if failures >= 20:
                        reason = f"OpenSea kept failing: {plan['error']}"
                        break
                    self.sweep_stop.wait(30 if exc.status == 429 else self.sweep_poll * 2)
                    continue
                rows = sorted(rows, key=lambda r: r["price_wei"])
                for row in rows:
                    if self.sweep_stop.is_set():
                        break
                    if row["hash"] in plan["seen"] or plan["later"].get(row["hash"], 0) > time.time():
                        continue
                    if row["native"] is False or row["quantity"] != 1 or row["price_wei"] > plan["max"]:
                        continue
                    if not row.get("token_id") and row.get("token_id") != 0:
                        continue
                    if plan["budget"] and plan["spent"] + row["price_wei"] > plan["budget"]:
                        reason = "budget reached"
                        self.sweep_stop.set()
                        break
                    sub = {"slug": plan["slug"], "contract": row["contract"], "token": row["token_id"], "max": plan["max"]}
                    for step in range(len(wallets)):
                        acct = wallets[(turn + step) % len(wallets)]
                        plan["tried"] += 1
                        try:
                            status, detail, txh = self.buy_one(chain_id, acct, sub, row)
                        except Exception as exc:
                            status, detail, txh = "retry", f"{type(exc).__name__}: {str(exc)[:120]}", None
                        if status == "pending":
                            # Sent but not confirmed. Count the spend so the budget holds, and never retry this listing.
                            plan["seen"].add(row["hash"])
                            plan["spent"] += row["price_wei"]
                            plan["bought"] += 1
                            turn = (turn + step + 1) % len(wallets)
                            self.alert(
                                f"Sweep sent, waiting for confirmation: {row['contract']} #{row['token_id']}",
                                card(
                                    f"⏳ Sweep sent, waiting for confirmation · {self.chains[chain_id].name}",
                                    ("CA", code(row["contract"])),
                                    ("Token", esc(f"#{row['token_id']}")),
                                    ("Tx", code(txh)),
                                ),
                            )
                            break
                        if status == "retry":
                            # A passing problem (rate limit, RPC hiccup). Look at this listing again soon, a few times at most.
                            count = plan["retries"].get(row["hash"], 0) + 1
                            plan["retries"][row["hash"]] = count
                            plan["error"] = detail[:160]
                            self.log(f"sweep retry {row['hash'][:12]} {detail}")
                            if count >= 5:
                                plan["seen"].add(row["hash"])
                            else:
                                plan["later"][row["hash"]] = time.time() + 30
                            break
                        if status in ("bought", "would"):
                            plan["seen"].add(row["hash"])
                            turn = (turn + step + 1) % len(wallets)
                            if status == "bought":
                                plan["bought"] += 1
                                plan["spent"] += row["price_wei"]
                            else:
                                plan["bought"] += 1
                            number = self.minters.index(acct) + 1
                            title = "Sweep bought" if status == "bought" else "Sweep would buy (dry run)"
                            self.alert(
                                f"{title}: {row['contract']} #{row['token_id']} for {detail}",
                                card(
                                    f"{'✅' if status == 'bought' else '🧪'} {title} · {self.chains[chain_id].name}",
                                    ("Collection", esc(plan["collection"])),
                                    ("CA", code(row["contract"])),
                                    ("Token", esc(f"#{row['token_id']}")),
                                    ("Price", esc(detail)),
                                    ("Wallet", f"👛 {number}. {code(short_address(acct.address))}"),
                                    ("Tx", code(txh) if txh else ""),
                                    ("Total", esc(f"{plan['bought']} bought for {fmt_native(plan['spent'], chain_id)}")),
                                ),
                            )
                            break
                        if status == "failed":
                            plan["seen"].add(row["hash"])
                            plan["error"] = detail[:160]
                            self.log(f"sweep skip {row['hash'][:12]} {detail}")
                            break
                        plan["error"] = detail[:160]
                    else:
                        # Every wallet was skipped (funds or price). Look at it again in a minute rather than every poll.
                        plan["later"][row["hash"]] = time.time() + 60
                self.sweep_stop.wait(self.sweep_poll)
        except Exception as exc:
            reason = f"error: {type(exc).__name__}: {str(exc)[:120]}"
            self.log(f"sweep crashed {exc}")
        finally:
            self.alert(
                f"Sweep ended: {reason}",
                card(
                    f"🧹 Sweep ended · {self.chains[chain_id].name}",
                    ("Collection", esc(plan["collection"])),
                    ("Why", esc(reason)),
                    ("Bought", esc(f"{plan['bought']} for {fmt_native(plan['spent'], chain_id)}")),
                    ("Last problem", esc(plan["error"])),
                ),
            )
            with self._sweep_lock:
                if self.sweep_plan is plan:
                    self.sweep_plan = None
