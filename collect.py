"""Forward part of every deposit from a collector wallet to a fixed destination, in the chain's own coin.

Set in Railway:  COLLECT_TO=0xDESTINATION   COLLECT_WALLET=1 (the minter number that receives, default 1)
Then /collect on. Each deposit is split: collect_percent (default 40) goes on to the destination, the rest stays.

How a deposit is seen: the wallet's balance goes up. A balance that rises and then holds for two checks is a
deposit; the extra is measured from a saved baseline. The part that stays becomes the new baseline, so it is
never forwarded again. Nothing already in the wallet when you switch it on is forwarded.
"""

from __future__ import annotations

import time

from helpers import *  # noqa: F401,F403


class CollectMixin:
    def collect_wallet(self):
        raw = env("COLLECT_WALLET", "1").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(self.minters):
            return self.minters[int(raw) - 1]
        return None

    def collect_destination(self):
        return valid_address(env("COLLECT_TO", ""))

    def collect_problem(self):
        """Why collecting cannot run, or ''."""
        if not self.collect_destination():
            return "COLLECT_TO is not set to a valid address in Railway."
        acct = self.collect_wallet()
        if acct is None:
            return "COLLECT_WALLET is not one of your minter numbers."
        if acct.address.lower() == self.collect_destination().lower():
            return "COLLECT_TO is the collector wallet itself."
        return ""

    def collect_rebase(self):
        acct = self.collect_wallet()
        if acct is None:
            return
        for chain_id in self.chains:
            try:
                self.collect_base[str(chain_id)] = self.balance(chain_id, "native", acct.address)
            except Exception as exc:
                self.log(f"collect baseline {chain_id} {type(exc).__name__}")
        self._save()

    def collect_cycle(self):
        """One look at every chain. Returns messages for deposits forwarded or problems."""
        if not self.setting("collect") or self.collect_problem():
            return []
        acct, to = self.collect_wallet(), self.collect_destination()
        percent = float(self.setting("collect_percent"))
        minimum = to_wei(self.setting("collect_minimum"))
        out = []
        for chain_id in self.chains:
            key = str(chain_id)
            try:
                bal = self.balance(chain_id, "native", acct.address)
            except Exception as exc:
                self.log(f"collect balance {chain_id} {type(exc).__name__}")
                continue
            if key not in self.collect_base:
                self.collect_base[key] = bal
                self._save()
                continue
            base = min(self.collect_base[key], bal)  # spending lowers the baseline
            self.collect_base[key] = base
            seen_before = self._collect_seen.get(key)
            self._collect_seen[key] = bal
            extra = bal - base
            if extra <= 0 or extra < max(minimum, 1) or seen_before != bal:
                continue  # nothing new, too small, or still arriving
            amount = int(extra * percent / 100)
            try:
                gas = self.estimate_gas(chain_id, acct, to, "0x", amount)
                fee, _tip = self.fees(chain_id)
            except Exception as exc:
                out.append(f"{self.chains[chain_id].name}: could not price the forward ({type(exc).__name__})")
                continue
            reserve = int(gas * fee * 1.5)
            if amount + reserve > bal:
                amount = bal - reserve
            if amount <= 0:
                out.append(f"{self.chains[chain_id].name}: {fmt_native(extra, chain_id)} arrived but is too small to forward after gas")
                self.collect_base[key] = bal
                continue
            try:
                txh, ok = self.send_native_tx(chain_id, acct, to, "0x", amount)
            except Exception as exc:
                out.append(f"{self.chains[chain_id].name}: forward failed ({str(exc)[:80]})")
                self.hist_add("collect", "failed", chain_id, to, str(exc)[:100])
                continue
            try:
                after = self.balance(chain_id, "native", acct.address)
            except Exception:
                after = bal - amount
            self.collect_base[key] = after if ok else base
            self._save()
            if ok:
                out.append(f"{self.chains[chain_id].name}: {fmt_native(extra, chain_id)} arrived, forwarded {fmt_native(amount, chain_id)} to {short_address(to)} · tx {txh}")
                self.hist_add("collect", "done", chain_id, to, f"{fmt_native(amount, chain_id)} of {fmt_native(extra, chain_id)} deposit")
            else:
                out.append(f"{self.chains[chain_id].name}: forward did not confirm in time ({txh})")
                self.hist_add("collect", "failed", chain_id, to, "did not confirm")
        return out

    def collect_loop(self):
        time.sleep(20)
        while True:
            try:
                for line in self.collect_cycle():
                    self.alert("Collect: " + line, card("💸 Collect") + "\n" + esc(line))
            except Exception as exc:
                self.log(f"collect loop {type(exc).__name__}: {exc}")
            time.sleep(max(2, float(env("COLLECT_POLL_SECONDS", "5"))))

    def collect_command(self, args):
        word = (args[0].lower() if args else "")
        if word in ("on", "off"):
            if word == "on":
                problem = self.collect_problem()
                if problem:
                    return f"⚠️ Cannot turn it on: {problem}"
                self.collect_rebase()
            self.settings["collect"] = word == "on"
            self._save()
            return "✅ Collecting is on. What is in the wallet now is the starting point; only new deposits are split." if word == "on" else "⏸ Collecting is off."
        if word == "reset":
            self.collect_rebase()
            return "↩️ Starting point reset to the wallet's current balance. Nothing there is forwarded."
        problem = self.collect_problem()
        acct = self.collect_wallet()
        lines = [f"💸 Collect: {'on' if self.setting('collect') else 'off'}"]
        if problem:
            lines.append(f"⚠️ {problem}")
        else:
            lines.append(f"Wallet: #{self.minters.index(acct) + 1} {short_address(acct.address)}")
            lines.append(f"Forwards to: {self.collect_destination()}")
        lines.append(f"Share of each deposit sent on: {self.setting('collect_percent')}% (/settings collect_percent)")
        lines.append(f"Ignores deposits under: {self.setting('collect_minimum')} (/settings collect_minimum)")
        lines.append("Commands: /collect on, /collect off, /collect reset")
        return "\n".join(lines)
