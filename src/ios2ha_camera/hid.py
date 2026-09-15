"""The keyboard report layout arcturus presents to the phone over Bluetooth.

Separate from the D-Bus plumbing in `keyboard.py` so the bytes can be tested
without a bus. Report ID 1, the usual boot-keyboard shape: modifiers, a reserved
byte, then up to six key usages, of which only one is ever sent -- iOS Full
Keyboard Access needs single keys, never chords beyond a modifier.
"""


class UnknownKey(Exception):
    """A key name that is not in KEYS, or a modifier with no key."""


# USB HID usage ids, keyboard/keypad page 0x07.
KEYS = {"enter": 0x28, "escape": 0x29, "tab": 0x2B, "space": 0x2C,
        "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52}
MODS = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "cmd": 0x08}

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
    """One key-down report for a name like "tab" or "shift+tab".

    An unknown name raises rather than producing a report with no usage: the
    phone would accept that and do nothing, leaving the caller believing a key
    was pressed.
    """
    mods, key = 0, None
    for part in name.lower().split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in KEYS:
            key = KEYS[part]
        else:
            raise UnknownKey(name)
    if key is None:
        raise UnknownKey(name)
    return bytes([mods, 0, key, 0, 0, 0, 0, 0])
