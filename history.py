"""A log of what the bot is about to do and what it did: snipes, manual mints and purchases.

/history        the next scheduled jobs, then the latest events (newest first)
/history 40     show 40 events
Kept in the state file, so it survives restarts.
"""

from __future__ import annotations

import time

from helpers import *  # noqa: F401,F403

MAX_HISTORY = 300
ICONS = {"armed": "⏳", "cancelled": "🛑", "done": "✅", "failed": "❌", "started": "▶️", "dry": "🧪", "partial": "⚠️"}


class HistoryMixin:
    def hist_add(self, kind, event, chain_id, contract, detail=""):
        try:
            self.history.append(
                {"t": int(time.time()), "kind": kind, "event": event, "chain": chain_id, "contract": str(contract or "").lower(), "detail": str(detail)[:200]}
            )
            del self.history[:-MAX_HISTORY]
            self._save()
        except Exception as exc:  # a log must never break a mint
            self.log(f"history {type(exc).__name__}: {exc}")

    def upcoming_text(self):
        lines = []
        plan = self.snipe_plan
        if plan and plan["state"] in ("armed", "planned"):
            seconds = max(0, int(plan["fire_at"] - time.time()))
            at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(plan["fire_at"]))
            wallets = self.plan_wallets_text(plan)
            lines.append(
                f"🎯 {'Armed' if plan['state'] == 'armed' else 'Planned (not armed)'} snipe · {self.chains[plan['chain']].name}\n"
                f"   {plan['contract']}\n   {plan['signature']} ×{plan['qty']} · wallets: {wallets}\n   fires {at} (in {fmt_duration(seconds)})"
            )
        buy = self.buy_plan
        if buy and buy.get("start_at") and buy["state"] in ("planned", "running"):
            at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(buy["start_at"]))
            lines.append(f"🛒 Scheduled buy · {buy['contract']} #{buy['token']} · starts {at}")
        sweep = self.sweep_plan
        if sweep and sweep.get("start_at") and sweep["state"] in ("planned", "running") and sweep["start_at"] > time.time():
            at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(sweep["start_at"]))
            lines.append(f"🧹 Scheduled sweep · {sweep['collection']} · starts {at}")
        return lines

    def history_command(self, args):
        count = 15
        if args and str(args[0]).isdigit():
            count = max(1, min(int(args[0]), 100))
        out = ["🗓 Coming up"]
        upcoming = self.upcoming_text()
        out.extend(upcoming or ["Nothing is scheduled."])
        out.append("\n📜 History (newest first)")
        rows = list(reversed(self.history))[:count]
        if not rows:
            out.append("Nothing yet.")
        for row in rows:
            when = time.strftime("%m-%d %H:%M UTC", time.gmtime(row["t"]))
            chain = self.chains[row["chain"]].name if row.get("chain") in self.chains else str(row.get("chain") or "")
            icon = ICONS.get(row["event"], "•")
            detail = f" — {row['detail']}" if row.get("detail") else ""
            out.append(f"{icon} {when} · {row['kind']} {row['event']} · {chain} · {short_address(row['contract']) if row['contract'] else ''}{detail}")
        return "\n".join(out)
