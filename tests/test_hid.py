"""The bytes the host sends the phone as a keyboard.

Kept away from D-Bus on purpose: a wrong usage id or a release of the wrong
length is a silent failure on the device, and these are the cheapest tests that
catch it.
"""

import pytest

from ios2ha_camera.hid import RELEASE, REPORT_MAP, UnknownKey, report


def test_a_plain_key_is_one_usage_in_the_third_byte():
    assert report("tab") == bytes([0, 0, 0x2B, 0, 0, 0, 0, 0])
    assert report("space") == bytes([0, 0, 0x2C, 0, 0, 0, 0, 0])
    assert report("down") == bytes([0, 0, 0x51, 0, 0, 0, 0, 0])


def test_a_modifier_sets_the_first_byte_and_keeps_the_key():
    assert report("shift+tab") == bytes([0x02, 0, 0x2B, 0, 0, 0, 0, 0])


def test_two_keys_can_be_held_together():
    """iOS Full Keyboard Access chords on Tab rather than a modifier: Tab+H is
    Home, Tab+Z lists its commands. Both usages go in the same report."""
    assert report("tab+h") == bytes([0, 0, 0x2B, 0x0B, 0, 0, 0, 0])


def test_letters_are_available_for_ios_shortcuts():
    """Cmd+H is Home on any hardware keyboard, with no accessibility setting
    involved, which makes it the honest test of whether iOS accepts us at all."""
    assert report("cmd+h") == bytes([0x08, 0, 0x0B, 0, 0, 0, 0, 0])
    assert report("a") == bytes([0, 0, 0x04, 0, 0, 0, 0, 0])
    assert report("z") == bytes([0, 0, 0x1D, 0, 0, 0, 0, 0])


def test_the_keys_ios_restart_shortcuts_need_are_present():
    """Ctrl+Cmd+Q locks the phone, and the power/minus keys are what the
    restart shortcuts use; a missing usage id would raise mid-sequence."""
    assert report("ctrl+cmd+q") == bytes([0x09, 0, 0x14, 0, 0, 0, 0, 0])
    assert report("minus") == bytes([0, 0, 0x2D, 0, 0, 0, 0, 0])
    assert report("ctrl+cmd+power") == bytes([0x09, 0, 0x66, 0, 0, 0, 0, 0])


def test_release_is_all_zero_and_the_same_length():
    assert RELEASE == bytes(8)
    assert len(RELEASE) == len(report("tab"))


def test_an_unknown_key_is_refused_rather_than_sent_as_nothing():
    """A typo must not become a report with no usage, which the phone accepts
    and ignores -- the sequence would then be silently wrong."""
    with pytest.raises(UnknownKey):
        report("enterr")


def test_a_modifier_on_its_own_is_refused():
    """Holding shift with no key moves no focus; asking for it is a mistake."""
    with pytest.raises(UnknownKey):
        report("shift")


def test_the_report_map_declares_report_id_1_and_is_a_keyboard():
    assert REPORT_MAP[:6] == bytes([0x05, 0x01, 0x09, 0x06, 0xA1, 0x01])
    assert REPORT_MAP[6:8] == bytes([0x85, 0x01])
    assert REPORT_MAP[-1] == 0xC0
