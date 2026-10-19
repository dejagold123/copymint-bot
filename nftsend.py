"""/sendnfts and NFT holdings: finding what the minters own and moving it. Mixed into Bot."""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from eth_utils import to_checksum_address

from helpers import *  # noqa: F401,F403


class SendMixin:
    def sendnfts_command(self, args):
        args = list(args)
        usage = (
            "✏️ Usage: /sendnfts 0xDESTINATION [wallets=1,3] [chain=robinhood] [collection=0xabc…] [deep]\n"
            "Sends every NFT the chosen minters hold (default: all minters, all enabled chains) to the destination.\n"
            "I list what I found and what it will cost, then you send /sendnfts confirm within 2 minutes, or /sendnfts cancel.\n"
            "I know the NFTs your minters minted through this bot since this feature was added. For older NFTs add collection=0xabc… "
            "(finds them if the collection can list a wallet's tokens), deep (checks token by token, collections up to "
            f"{DEEP_SCAN_LIMIT} NFTs), or ids=5,7,100-120 with one collection= if you know the token IDs or a range to check."
        )
        if not args:
            return usage
        word0 = args[0].lower()
        if word0 == "cancel":
            with self._send_lock:
                plan = self.send_plan
                if not plan:
                    return "Nothing is waiting to be sent."
                if plan["state"] == "planned":
                    self.send_plan = None
                    return "🛑 Cancelled."
                # A run is going. Stop that run only, and keep the plan in place until its thread ends.
                plan["state"] = "stopping"
                plan["cancel"].set()
            return "🛑 Stopping. Transfers already sent cannot be undone."
        if word0 == "confirm":
            with self._send_lock:
                plan = self.send_plan
                if not plan or plan["state"] != "planned":
                    return "There is no plan waiting for confirmation. Create one with /sendnfts first."
                if time.time() - plan["created"] > SEND_PLAN_SECONDS:
                    self.send_plan = None
                    return "⌛ That plan expired. Send the /sendnfts command again."
                plan["state"] = "running"
            threading.Thread(target=self.run_send, args=(plan,), daemon=True).start()
            mode = " (dry run, nothing will be sent)" if self.dry else ""
            return f"✅ Sending {len(plan['transfers'])} NFT transfer(s) to {plan['dest']}{mode}. A summary follows."
        with self._send_lock:
            if self.send_plan and self.send_plan["state"] in ("running", "stopping"):
                return "A send is already running or stopping. Send /sendnfts cancel to stop it."
        deep = False
        for token in list(args):
            if token.lower() == "deep":
                deep = True
                args.remove(token)
        wallet_spec = self.pull_option(args, ("wallets=", "wallet=", "w="))
        chain_word = self.pull_option(args, ("chain=", "c="))
        collection_spec = self.pull_option(args, ("collection=", "collections="))
        ids_spec = self.pull_option(args, ("ids=", "id="))
        accounts, error = self.choose_minters(wallet_spec)
        if error:
            return error
        chain_ids = list(self.chains)
        if chain_word is not None:
            only = self.chain_from_word(chain_word)
            if only is None:
                names = ", ".join(f"{c.name} ({cid})" for cid, c in self.chains.items())
                return f"⚠️ I do not know the chain \"{chain_word}\". Enabled chains: {names}."
            chain_ids = [only]
        extra = []
        for part in re.split(r"[,;\s]+", collection_spec or ""):
            if not part:
                continue
            if valid_address(part) is None:
                return f"⚠️ \"{part}\" is not a valid collection address."
            extra.append(valid_address(part))
        given = []
        if ids_spec is not None:
            if len(extra) != 1:
                return "⚠️ ids= goes with exactly one collection=0x…, so I know which collection the IDs belong to."
            given, id_error = parse_id_list(ids_spec)
            if id_error:
                return "⚠️ " + id_error
        if len(args) != 1:
            return "⚠️ I need exactly one destination address.\n" + usage
        dest = valid_address(args[0])
        if dest is None:
            return "⚠️ That is not a valid destination address.\n" + usage
        if int(dest, 16) == 0:
            return "⚠️ I will not send NFTs to the zero address."
        threading.Thread(target=self.prepare_send, args=(dest, accounts, chain_ids, extra, deep, given), daemon=True).start()
        return f"🔎 Looking for NFTs in {len(accounts)} wallet(s) on {', '.join(self.chains[c].name for c in chain_ids)}. The plan follows in a moment."

    def prepare_send(self, dest, accounts, chain_ids, extra, deep, given=()):
        try:
            transfers, blocked, missing, skipped = [], [], [], []
            names = {}
            for chain_id in chain_ids:
                for acct in accounts:
                    if acct.address.lower() == dest:
                        skipped.append(acct.address)
                        continue
                    where = [c for c in extra if self.has_code(chain_id, c)]
                    for group in self.find_nfts(chain_id, acct, where, deep, given):
                        name = self.collection_name(chain_id, group["collection"]) or short_address(group["collection"])
                        names[(chain_id, group["collection"])] = name
                        if group["missing"]:
                            missing.append((chain_id, acct.address, group["collection"], group["missing"], group.get("note", "")))
                        for token_id, amount in group["items"]:
                            data = self.transfer_data(group["std"], acct.address, dest, token_id, amount)
                            transfer = {
                                "chain": chain_id,
                                "wallet": acct.address.lower(),
                                "collection": group["collection"],
                                "std": group["std"],
                                "id": str(token_id),
                                "amount": amount,
                            }
                            try:
                                estimate = as_int(
                                    self.rpc.call(chain_id, "eth_estimateGas", [{"from": acct.address, "to": to_checksum_address(group["collection"]), "data": data, "value": "0x0"}])
                                )
                                transfer["gas"] = int(estimate * 1.3) + 20_000
                                transfers.append(transfer)
                            except Exception as exc:
                                reason = decode_revert("", str(exc)) or "the contract refused the transfer"
                                blocked.append((transfer, reason))
            plan = {"dest": dest, "transfers": transfers, "created": time.time(), "state": "planned", "blocked": len(blocked), "cancel": threading.Event()}
            text = self.send_plan_text(plan, names, blocked, missing, skipped, deep)
            with self._send_lock:
                if transfers and not (self.send_plan and self.send_plan["state"] == "running"):
                    self.send_plan = plan
            self.notify(text)
        except Exception as exc:
            self.log(f"/sendnfts plan {type(exc).__name__}: {exc}")
            self.notify(f"⚠️ /sendnfts failed while looking for NFTs: {type(exc).__name__}")

    def send_plan_text(self, plan, names, blocked, missing, skipped, deep):
        transfers = plan["transfers"]
        numbers = {a.address.lower(): i + 1 for i, a in enumerate(self.minters)}
        groups = {}
        for t in transfers:
            groups.setdefault((t["chain"], t["wallet"], t["collection"]), []).append(t)
        lines = []
        for (chain_id, wallet, collection), items in list(groups.items())[:25]:
            count = sum(t["amount"] for t in items)
            label = names.get((chain_id, collection)) or short_address(collection)
            lines.append(f"• {self.chains[chain_id].name} · #{numbers.get(wallet, '?')} {short_address(wallet)} · {label} ×{count}")
        if len(groups) > 25:
            lines.append(f"…and {len(groups) - 25} more group(s)")
        head = f"📦 Send NFTs plan\nTo: {plan['dest']}\n"
        if not transfers:
            body = "I found no NFTs that can be sent.\n"
        else:
            total = sum(t["amount"] for t in transfers)
            body = "\n".join(lines) + f"\nTotal: {total} NFT(s) in {len(transfers)} transfer(s)\n"
            if any(self.has_code(c, plan["dest"]) for c in {t["chain"] for t in transfers}):
                body += "⚠️ The destination is a contract on one of these chains. Double-check that it can receive NFTs.\n"
        if blocked:
            body += f"Cannot send {len(blocked)} NFT(s) (the contract refused the transfer): {blocked[0][1]}\n"
        if missing:
            more = sum(m[3] for m in missing)
            body += f"{more} more NFT(s) are held but I cannot list their token IDs."
            notes = []
            for m in missing:
                if m[4] and m[4] not in notes:
                    notes.append(m[4])
            if notes:
                body += " Why: " + "; ".join(notes) + "."
            elif not deep:
                body += " Add collection=0xabc… and deep to look harder."
            body += "\n"
        if skipped:
            body += "Skipped (already the destination): " + ", ".join(short_address(a) for a in skipped) + "\n"
        if self.dry:
            body += "Dry run is on: nothing will be sent\n"
        tail = f"\nSend /sendnfts confirm within {SEND_PLAN_SECONDS // 60} minutes to send, or /sendnfts cancel." if transfers else ""
        return head + body + tail

    def send_worker(self, plan, chain_id, acct, items, results):
        for t in items:
            if self.send_cancel.is_set() or plan["cancel"].is_set():
                results.append((t, "cancelled", "stopped before this one"))
                continue
            data = self.transfer_data(t["std"], acct.address, plan["dest"], int(t["id"]), t["amount"])
            try:
                with self._copy_lock:
                    txh, ok, _sent = self.land(chain_id, acct, t["collection"], data, 0, t["gas"])
            except Exception as exc:
                results.append((t, "failed", str(exc)[:80]))
                continue
            if ok:
                results.append((t, "dry" if self.dry else "sent", txh))
                if not self.dry:
                    self.drop_holding(chain_id, t["collection"], acct.address, t["id"])
            else:
                results.append((t, "failed", "did not confirm in time"))
        self._save()

    def run_send(self, plan):
        results = []
        try:
            by_wallet = {}
            for t in plan["transfers"]:
                by_wallet.setdefault((t["chain"], t["wallet"]), []).append(t)
            accounts = {a.address.lower(): a for a in self.minters}
            with ThreadPoolExecutor(max_workers=max(1, len(by_wallet))) as pool:
                jobs = [
                    pool.submit(self.send_worker, plan, chain_id, accounts[wallet], items, results)
                    for (chain_id, wallet), items in by_wallet.items()
                    if wallet in accounts
                ]
                for job in jobs:
                    job.result()
            sent = [r for r in results if r[1] in ("sent", "dry")]
            failed = [r for r in results if r[1] == "failed"]
            stopped = [r for r in results if r[1] == "cancelled"]
            total = len(plan["transfers"])
            title = "🧪 Would send" if self.dry else ("✅ Sent" if len(sent) == total else "⚠️ Sent")
            lines = [f"{title} {len(sent)} of {total} NFT transfer(s) to {plan['dest']}"]
            if failed:
                reasons = {}
                for _t, _status, reason in failed:
                    reasons[reason] = reasons.get(reason, 0) + 1
                lines.append("Failed: " + "; ".join(f"{n}× {reason}" for reason, n in reasons.items()))
            if stopped:
                lines.append(f"Stopped before sending {len(stopped)}.")
            self.notify("\n".join(lines))
        except Exception as exc:
            self.log(f"/sendnfts {type(exc).__name__}: {exc}")
            self.notify(f"⚠️ /sendnfts failed: {type(exc).__name__}")
        finally:
            with self._send_lock:
                if self.send_plan is plan:
                    self.send_plan = None

    def record_holdings(self, chain_id, minter, txh):
        """Remember the token IDs a minter received in one landed transaction, so they can be sent on later."""
        try:
            receipt = self.rpc.call(chain_id, "eth_getTransactionReceipt", [txh])
            if not receipt:
                return
            who = minter.lower()
            for entry in receipt.get("logs") or []:
                topics = [t.lower() for t in entry.get("topics") or []]
                collection = (entry.get("address") or "").lower()
                data = entry.get("data") or "0x"
                if not collection or len(topics) != 4:
                    continue
                if topics[0] == TRANSFER and topic_address(topics[2]) == who:
                    self.add_holding(chain_id, collection, who, 721, [int(topics[3], 16)])
                elif topics[0] == TRANSFER_SINGLE and topic_address(topics[3]) == who and len(data) >= 2 + 128:
                    self.add_holding(chain_id, collection, who, 1155, [int(data[2:66], 16)])
                elif topics[0] == TRANSFER_BATCH and topic_address(topics[3]) == who:
                    from eth_abi import decode

                    ids, _values = decode(["uint256[]", "uint256[]"], bytes.fromhex(data[2:]))
                    self.add_holding(chain_id, collection, who, 1155, list(ids))
            self._save()
        except Exception as exc:
            self.log(f"record holdings {type(exc).__name__}: {exc}")

    def record_holdings_later(self, chain_id, minter, txh):
        threading.Thread(target=self.record_holdings, args=(chain_id, minter, txh), daemon=True).start()

    def add_holding(self, chain_id, collection, wallet, std, ids):
        key = f"{chain_id}:{collection.lower()}:{wallet.lower()}"
        entry = self.holdings.setdefault(key, {"std": std, "ids": []})
        entry["std"] = std
        known = set(entry["ids"])
        for token_id in ids:
            if str(token_id) not in known:
                entry["ids"].append(str(token_id))
        del entry["ids"][:-MAX_HOLDINGS]
        while len(self.holdings) > 2000:
            self.holdings.pop(next(iter(self.holdings)))

    def drop_holding(self, chain_id, collection, wallet, token_id):
        key = f"{chain_id}:{collection.lower()}:{wallet.lower()}"
        entry = self.holdings.get(key)
        if not entry:
            return
        entry["ids"] = [i for i in entry["ids"] if i != str(token_id)]
        if not entry["ids"]:
            self.holdings.pop(key, None)

    def find_nfts(self, chain_id, acct, extra=(), deep=False, given=()):
        """Everything this wallet holds on one chain: a list of dicts with collection, std, items and missing."""
        wallet = acct.address.lower()
        hints = {}
        for key, entry in list(self.holdings.items()):
            cid, collection, holder = key.split(":")
            if int(cid) == chain_id and holder == wallet:
                hints[collection] = (entry["std"], entry["ids"])
        for key, holders in self.minted_before.items():
            cid, _, collection = key.partition(":")
            if cid == str(chain_id) and wallet in holders:
                hints.setdefault(collection.lower(), (None, []))
        for collection in extra:
            hints.setdefault(collection.lower(), (None, []))
        found = []
        for collection, (std, recorded) in hints.items():
            std = std or self.detect_standard(chain_id, collection, wallet)
            note = ""
            extra_ids = list(given) if collection in {c.lower() for c in extra} else []
            if std == 721:
                ids, missing, note = self.nfts_721(chain_id, collection, wallet, recorded, deep, extra_ids)
                items = [(i, 1) for i in ids]
            elif std == 1155:
                items, missing = self.nfts_1155(chain_id, collection, wallet, list(recorded) + [str(i) for i in extra_ids]), 0
            else:
                continue
            if items or missing:
                found.append({"collection": collection, "std": std, "items": items, "missing": missing, "note": note})
        return found

    def nfts_721(self, chain_id, collection, wallet, recorded, deep, given=()):
        """Token IDs this wallet owns in an ERC-721 collection, how many more it holds that were not found, and a note on why."""
        balance = self.view_int(chain_id, collection, BALANCE_OF_721 + addr_word(wallet))
        if not balance:
            return [], 0, ""
        owned = set(self.check_owned(chain_id, collection, wallet, [int(i) for i in list(recorded) + list(given)]))
        note = ""
        if len(owned) < balance:
            for index in range(balance):
                token = self.view_int(chain_id, collection, TOKEN_BY_INDEX + addr_word(wallet) + uint_word(index))
                if token is None:
                    break
                owned.add(token)
        if len(owned) < balance and deep:
            supply = self.view_int(chain_id, collection, TOTAL_SUPPLY)
            if not supply:
                note = "its totalSupply() could not be read, so there is no range to scan; add ids=1-5000 (your own range) to scan one"
            elif supply > DEEP_SCAN_LIMIT:
                note = f"it has {supply} NFTs, above the deep scan limit of {DEEP_SCAN_LIMIT}; add ids=FIRST-LAST to scan a range you choose"
            else:
                candidates = [i for i in range(0, supply + 2) if i not in owned]
                owned.update(self.check_owned(chain_id, collection, wallet, candidates))
                if len(owned) < balance:
                    note = f"I checked token IDs 0 to {supply + 1} and the rest are not in that range; add ids=FIRST-LAST for a wider range"
        elif len(owned) < balance and given:
            note = "none of the token IDs you gave, or the rest, are owned by this wallet"
        return sorted(owned), max(0, balance - len(owned)), note

    def nfts_1155(self, chain_id, collection, wallet, recorded):
        found = []
        for raw_id in recorded:
            amount = self.view_int(chain_id, collection, BALANCE_OF_1155 + addr_word(wallet) + uint_word(int(raw_id)))
            if amount:
                found.append((int(raw_id), amount))
        return sorted(found)

    def detect_standard(self, chain_id, collection, wallet):
        for interface, std in (("80ac58cd", 721), ("d9b67a26", 1155)):
            if self.view_int(chain_id, collection, SUPPORTS_INTERFACE + interface + "0" * 56):
                return std
        if self.view_int(chain_id, collection, BALANCE_OF_721 + addr_word(wallet)) is not None:
            return 721
        return None

    def owns_721(self, chain_id, collection, token_id, wallet):
        raw = self.view(chain_id, collection, OWNER_OF + uint_word(token_id))
        return bool(raw) and len(raw) >= 66 and raw[-40:].lower() == wallet[2:].lower()

    def check_owned(self, chain_id, collection, wallet, token_ids):
        """The token IDs from the list that the wallet owns, checked in parallel."""
        token_ids = list(token_ids)
        if len(token_ids) < 20:
            return [t for t in token_ids if self.owns_721(chain_id, collection, t, wallet)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            flags = list(pool.map(lambda t: self.owns_721(chain_id, collection, t, wallet), token_ids))
        return [t for t, mine in zip(token_ids, flags) if mine]

    def transfer_data(self, std, sender, dest, token_id, amount):
        from eth_abi import encode

        if std == 721:
            return SAFE_TRANSFER_721 + addr_word(sender) + addr_word(dest) + uint_word(token_id)
        body = encode(["address", "address", "uint256", "uint256", "bytes"], [to_checksum_address(sender), to_checksum_address(dest), token_id, amount, b""])
        return SAFE_TRANSFER_1155 + body.hex()

    def view(self, chain_id, to, data):
        try:
            raw = self.rpc.call(chain_id, "eth_call", [{"to": to_checksum_address(to), "data": data}, "latest"])
        except Exception:
            return None
        return raw if isinstance(raw, str) and raw.startswith("0x") and len(raw) > 2 else None

    def view_int(self, chain_id, to, data):
        raw = self.view(chain_id, to, data)
        if not raw or len(raw) < 66:
            return None
        try:
            return int(raw[:66], 16)
        except ValueError:
            return None
