"""The keyboard report layout arcturus presents to the phone over Bluetooth.

Separate from the D-Bus plumbing in `keyboard.py` so the bytes can be tested
without a bus. Report ID 1, the usual boot-keyboard shape: modifiers, a reserved
byte, then up to six key usages. More than one usage at a time is genuinely
needed: iOS Full Keyboard Access treats Tab as a chord key, so Home is Tab+H
rather than a modifier and a letter.
"""


class UnknownKey(Exception):
    """A key name that is not in KEYS, or a modifier with no key."""


# USB HID usage ids, keyboard/keypad page 0x07.
KEYS = {"enter": 0x28, "escape": 0x29, "tab": 0x2B, "space": 0x2C,
        "minus": 0x2D, "equals": 0x2E, "power": 0x66,
        "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52}
# Letters, for iOS's own shortcuts: Cmd+H is Home on any hardware keyboard, and
# Full Keyboard Access uses Tab as a chord key (Tab+H, Tab+Z and so on).
KEYS.update({chr(ord("a") + i): 0x04 + i for i in range(26)})
MODS = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "cmd": 0x08}

# A report carries up to six keys at once; more than that is a rollover error,
# which no sequence here needs.
MAX_KEYS = 6

# Every key up. Sent after each press: without it the phone sees the key held.
RELEASE = bytes(8)

REPORT_MAP = bytes([
    0x05, 0x01,        # usage page: generic desktop
    0x09, 0x06,        # usage: keyboard
    0xA1, 0x01,        # collection: application
    0x85, 0x01,        #   report id 1
    0x05, 0x07,        #   usage page: keyboard/keypad
    0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x08, 0x81, 0x02,        # 8 modifier bits
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,        # reserved byte
    0x95, 0x05, 0x75, 0x01, 0x05, 0x08,        # 5 LED bits (output)
    0x19, 0x01, 0x29, 0x05, 0x91, 0x02,
    0x95, 0x01, 0x75, 0x03, 0x91, 0x01,        # LED padding
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65,
    0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,   # 6 key usages
    0xC0,              # end collection
])


def report(name):
    """One key-down report for a name like "tab", "shift+tab" or "tab+h".

    Several non-modifier keys may be held together, because iOS's Full Keyboard
    Access uses Tab as a chord key rather than a modifier: Tab+H is Home, Tab+Z
    shows its command list. An unknown name raises rather than producing a
    report with no usage, which the phone would accept and ignore, leaving the
    caller believing a key was pressed.
    """
    mods, keys = 0, []
    for part in name.lower().split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in KEYS:
            keys.append(KEYS[part])
        else:
            raise UnknownKey(name)
    if not keys:
        raise UnknownKey(name)
    if len(keys) > MAX_KEYS:
        raise UnknownKey(f"{name}: more than {MAX_KEYS} keys at once")
    return bytes([mods, 0, *keys, *([0] * (MAX_KEYS - len(keys)))])
