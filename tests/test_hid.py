"""The bytes arcturus sends the phone as a keyboard.

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
