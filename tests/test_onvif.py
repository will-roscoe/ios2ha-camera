"""WS-Security header construction and canned-response parsing.

No network is touched here: get_autofocus/set_autofocus are exercised against
fixed response strings by monkeypatching urllib.request.urlopen. Live calls
against the real phone are verified by hand, never from a test -- reaching it
from a test risks wedging the app.
"""

import base64
import hashlib
import re

from ios2ha_camera import onvif
from ios2ha_camera.onvif import OnvifClient, wsse_header

# A placeholder, deliberately not the real device password: secrets belong in
# the deployment .env, never in a committed file. The digest is recomputed from
# this same constant, so the value itself is arbitrary.
PASSWORD = "not-the-real-password"


def test_wsse_digest_is_correct():
    h = wsse_header("root", PASSWORD)
    assert "UsernameToken" in h and "root" in h
    # extract nonce, created, digest and recompute
    nonce_b64 = re.search(r"Nonce[^>]*>([^<]+)<", h).group(1)
    created = re.search(r"Created>([^<]+)<", h).group(1)
    digest = re.search(r'PasswordDigest">([^<]+)<', h).group(1)
    expect = base64.b64encode(hashlib.sha1(
        base64.b64decode(nonce_b64) + created.encode() + PASSWORD.encode()).digest()).decode()
    assert digest == expect


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


GET_AUTO_RESPONSE = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    b'<s:Body><timg:GetImagingSettingsResponse xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl">'
    b'<timg:ImagingSettings><tt:Focus xmlns:tt="http://www.onvif.org/ver10/schema">'
    b'<tt:AutoFocusMode>AUTO</tt:AutoFocusMode></tt:Focus></timg:ImagingSettings>'
    b'</timg:GetImagingSettingsResponse></s:Body></s:Envelope>'
)

GET_NO_FOCUS_RESPONSE = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    b'<s:Body><timg:GetImagingSettingsResponse xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl">'
    b'<timg:ImagingSettings/></timg:GetImagingSettingsResponse></s:Body></s:Envelope>'
)

SET_OK_RESPONSE = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    b'<s:Body><timg:SetImagingSettingsResponse xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl"/>'
    b'</s:Body></s:Envelope>'
)

# Real shape measured against the phone for the controls it refuses (task context):
# SetVideoEncoderConfiguration answers ActionNotSupported. set_autofocus must read this as failure.
SET_FAULT_RESPONSE = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    b'<s:Body><s:Fault><s:Code><s:Value>s:Receiver</s:Value>'
    b'<s:Subcode><s:Value>ter:ActionNotSupported</s:Value></s:Subcode></s:Code>'
    b'<s:Reason><s:Text xml:lang="en">Action Not Supported</s:Text></s:Reason></s:Fault>'
    b'</s:Body></s:Envelope>'
)


def _patch_urlopen(monkeypatch, response_bytes, captured):
    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResponse(response_bytes)
    monkeypatch.setattr(onvif.urllib.request, "urlopen", fake_urlopen)


def test_get_autofocus_parses_mode_from_response(monkeypatch):
    captured = {}
    _patch_urlopen(monkeypatch, GET_AUTO_RESPONSE, captured)
    client = OnvifClient("192.168.1.11", 8080, "root", "secret")

    assert client.get_autofocus() == "AUTO"
    body = captured["req"].data.decode()
    assert "GetImagingSettings" in body and onvif.TOKEN in body
    assert "UsernameToken" in body  # WS-Security header made it into the envelope


def test_get_autofocus_unknown_when_field_absent(monkeypatch):
    _patch_urlopen(monkeypatch, GET_NO_FOCUS_RESPONSE, {})
    client = OnvifClient("192.168.1.11", 8080, "root", "secret")
    assert client.get_autofocus() == "unknown"


def test_set_autofocus_true_on_success(monkeypatch):
    captured = {}
    _patch_urlopen(monkeypatch, SET_OK_RESPONSE, captured)
    client = OnvifClient("192.168.1.11", 8080, "root", "secret")

    assert client.set_autofocus("MANUAL") is True
    body = captured["req"].data.decode()
    assert "SetImagingSettings" in body and "MANUAL" in body and onvif.TOKEN in body


def test_set_autofocus_false_on_fault(monkeypatch):
    _patch_urlopen(monkeypatch, SET_FAULT_RESPONSE, {})
    client = OnvifClient("192.168.1.11", 8080, "root", "secret")
    assert client.set_autofocus("MANUAL") is False


def test_request_uses_a_finite_short_timeout(monkeypatch):
    """A hung ONVIF call must never be able to stall the supervisor's capture loop."""
    captured = {}
    _patch_urlopen(monkeypatch, SET_OK_RESPONSE, captured)
    client = OnvifClient("192.168.1.11", 8080, "root", "secret")
    client.set_autofocus("AUTO")
    assert captured["timeout"] is not None and 0 < captured["timeout"] <= 15


# Shapes as the phone answered them on 2026-09-28, trimmed to the parts read.
GET_STATUS_RESPONSE = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    b'<s:Body><timg:GetStatusResponse xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl">'
    b'<timg:Status><tt:FocusStatus20 xmlns:tt="http://www.onvif.org/ver10/schema">'
    b'<tt:Position>0.682352901</tt:Position><tt:MoveStatus>IDLE</tt:MoveStatus>'
    b'</tt:FocusStatus20></timg:Status></timg:GetStatusResponse></s:Body></s:Envelope>'
)

MOVE_OK_RESPONSE = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    b'<s:Body><timg:MoveResponse xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl"/>'
    b'</s:Body></s:Envelope>'
)


def test_focus_position_reads_the_lens_position(monkeypatch):
    captured = {}
    _patch_urlopen(monkeypatch, GET_STATUS_RESPONSE, captured)
    client = OnvifClient("192.168.1.11", 8080, "root", "secret")
    assert client.focus_position() == 0.682352901
    assert "GetStatus" in captured["req"].data.decode()


def test_focus_position_is_none_when_the_answer_has_none(monkeypatch):
    _patch_urlopen(monkeypatch, GET_NO_FOCUS_RESPONSE, {})
    client = OnvifClient("192.168.1.11", 8080, "root", "secret")
    assert client.focus_position() is None


def test_move_focus_sends_an_absolute_move(monkeypatch):
    captured = {}
    _patch_urlopen(monkeypatch, MOVE_OK_RESPONSE, captured)
    client = OnvifClient("192.168.1.11", 8080, "root", "secret")
    assert client.move_focus(0.8) is True
    body = captured["req"].data.decode()
    assert "<Move " in body and "Absolute" in body and "<Position>0.8</Position>" in body


def test_move_focus_false_on_fault(monkeypatch):
    _patch_urlopen(monkeypatch, SET_FAULT_RESPONSE, {})
    client = OnvifClient("192.168.1.11", 8080, "root", "secret")
    assert client.move_focus(0.8) is False
