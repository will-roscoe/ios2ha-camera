"""The control values Home Assistant writes, kept across restarts.

Without this every restart put every control back to its default, and restarts
are not rare: each phone reboot restarts the service through the usbmuxd hook,
nine times in the week to 2026-09-28. Autofocus switched off at 22:22 on the
24th was back on at 14:20 on the 25th, and nothing said so.

Only controls are kept -- the names in `settings.CONTROLS` -- and only controls
are ever read back, so nothing in the file can reach a deployment setting such
as the phone's address or password, which come from the environment alone.
Every value read back goes through `Settings.apply_control`, the same
validation a live write gets, so a hand-edited or stale file cannot put a value
in place that Home Assistant could not have.
"""

import json
import logging
import os

from .settings import CONTROLS

log = logging.getLogger(__name__)

# Derived from the switch and the three numbers by Settings._sync_black_level,
# so it is rebuilt on load rather than stored. `black_level` is stored in its
# place: the three numbers are one tuple field, not controls of their own.
_DERIVED = {"black_level_override"}
_KEPT = CONTROLS - _DERIVED


class ControlStore:
    def __init__(self, path):
        self.path = path

    def load(self, settings):
        """Apply every kept value to `settings`. Returns the names restored.

        Never raises. A missing file is a first start; an unreadable one is
        reported and ignored, leaving the defaults, rather than stopping a
        camera over a setting.
        """
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("controls: could not read %s, keeping the defaults: %s", self.path, e)
            return []
        if not isinstance(data, dict):
            log.warning("controls: %s does not hold an object, keeping the defaults", self.path)
            return []

        restored = []
        # The numbers before the switch, so the switch's own sync sees them.
        channels = data.get("black_level")
        if channels is not None:
            try:
                values = tuple(float(v) for v in channels)
                if len(values) != 3:
                    raise ValueError(f"expected three channels, not {len(values)}")
                settings.black_level = values
                restored.append("black_level")
            except (TypeError, ValueError) as e:
                log.warning("controls: ignoring black_level %r: %s", channels, e)
        for name in sorted(data):
            if name not in _KEPT:
                continue
            try:
                settings.apply_control(name, data[name])
            except (TypeError, ValueError) as e:
                log.warning("controls: ignoring %s %r: %s", name, data[name], e)
                continue
            restored.append(name)
        settings._sync_black_level()
        return [n for n in restored if n != "black_level"]

    def save(self, settings):
        """Write every kept value. Returns whether it was written.

        Written beside the file and renamed over it, so a crash mid-write leaves
        the previous values rather than half a file. A failure is logged, never
        raised: the control has already taken effect, and losing it at the next
        restart is the smaller harm.
        """
        data = {name: getattr(settings, name) for name in sorted(_KEPT)}
        data["black_level"] = list(settings.black_level)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("controls: could not save to %s: %s", self.path, e)
            return False
        return True
