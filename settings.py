"""Settings you set once from Telegram and change later. Saved in the state file, so they survive restarts.

/settings              shows every setting
/settings KEY VALUE    changes one
/settings reset KEY    puts one back to its default
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# key -> (kind, default, minimum, maximum, description)
SPEC = {
    "autolist": ("bool", False, None, None, "List NFTs your minters mint or buy on OpenSea, priced off the top offer"),
    "autolist_multiple": ("float", 2.0, 1.0, 50.0, "List at this multiple of the top offer (2 = double)"),
    "autolist_step": ("float", 10.0, 1.0, 500.0, "Reprice only when the new price is this many percent above the current listing"),
    "autolist_minimum": ("amount", "0", None, None, "Never list below this price, in the chain's coin (0 = no floor)"),
    "autolist_days": ("int", 14, 1, 180, "How many days a listing the bot makes stays valid"),
    "autolist_seconds": ("int", 60, 15, 3600, "How often listings are checked, in seconds"),
    "premint_min_sales": ("int", 0, 0, 100000, "Skip copying a collection OpenSea knows that has fewer sales than this (0 = off; new collections always pass)"),
    "opensea_names": ("bool", True, None, None, "Use OpenSea collection names in alerts when the contract has none"),
    "stream": ("bool", True, None, None, "Use OpenSea's live feed to wake /sweep the moment a listing appears"),
}

TRUE = {"on", "true", "yes", "1", "enable", "enabled"}
FALSE = {"off", "false", "no", "0", "disable", "disabled"}


def parse_value(key, text):
    """(value, error) for a setting. The value is stored as JSON-friendly data."""
    kind, _default, low, high, _doc = SPEC[key]
    text = str(text).strip().lower()
    if kind == "bool":
        if text in TRUE:
            return True, None
        if text in FALSE:
            return False, None
        return None, "use on or off"
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None, "that is not a number"
    if not number.is_finite():
        return None, "that is not a number"
    if kind == "int":
        if number != number.to_integral_value():
            return None, "use a whole number"
        number = int(number)
    elif kind == "float":
        number = float(number)
    elif kind == "amount":
        if number < 0 or number > 1000:
            return None, "use an amount between 0 and 1000"
        return format(number.normalize(), "f"), None
    if low is not None and number < low:
        return None, f"the minimum is {low}"
    if high is not None and number > high:
        return None, f"the maximum is {high}"
    return number, None


class SettingsMixin:
    def setting(self, key):
        """The current value of a setting, or its default."""
        return self.settings.get(key, SPEC[key][1])

    def settings_text(self):
        lines = ["⚙️ Settings (change with /settings KEY VALUE)"]
        for key, (kind, default, _low, _high, doc) in SPEC.items():
            value = self.setting(key)
            shown = ("on" if value else "off") if kind == "bool" else str(value)
            mark = "" if key not in self.settings else " ✏️"
            lines.append(f"• {key} = {shown}{mark}\n   {doc}")
        lines.append("\n✏️ marks a value you changed. /settings reset KEY restores the default.")
        return "\n".join(lines)

    def settings_command(self, args):
        args = list(args)
        if not args:
            return self.settings_text()
        if args[0].lower() == "reset":
            if len(args) < 2:
                return "✏️ Usage: /settings reset KEY (or /settings reset all)"
            key = args[1].lower()
            if key == "all":
                self.settings.clear()
                self._save()
                return "↩️ Every setting is back to its default."
            if key not in SPEC:
                return f"⚠️ I do not know a setting called {key}. Send /settings to see them."
            self.settings.pop(key, None)
            self._save()
            return f"↩️ {key} is back to its default."
        key = args[0].lower()
        if key not in SPEC:
            return f"⚠️ I do not know a setting called {key}. Send /settings to see them."
        if len(args) < 2:
            return f"{key} = {self.setting(key)}\n{SPEC[key][4]}"
        value, error = parse_value(key, args[1])
        if error:
            return f"⚠️ {key}: {error}."
        self.settings[key] = value
        self._save()
        self.setting_changed(key, value)
        shown = ("on" if value else "off") if SPEC[key][0] == "bool" else value
        return f"✅ {key} = {shown}. Saved; it stays until you change it."

    def setting_changed(self, key, value):
        """Hook for anything that must react to a change. Bot overrides it."""
