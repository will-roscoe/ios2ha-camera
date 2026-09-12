"""WS-Security header construction and canned-response parsing.

No network is touched here: get_autofocus/set_autofocus are exercised against
fixed response strings by monkeypatching urllib.request.urlopen. Live calls
against the real phone are verified in Task 15 -- reaching it from a test
risks wedging the app.
"""

import base64
import hashlib
import re

from ios2ha_camera import onvif
from ios2ha_camera.onvif import OnvifClient, wsse_header


def test_wsse_digest_is_correct():
    h = wsse_header("root", "william2004")
    assert "UsernameToken" in h and "root" in h
    # extract nonce, created, digest and recompute
    nonce_b64 = re.search(r"Nonce[^>]*>([^<]+)<", h).group(1)
    created = re.search(r"Created>([^<]+)<", h).group(1)
    digest = re.search(r'PasswordDigest">([^<]+)<', h).group(1)
    expect = base64.b64encode(hashlib.sha1(
        base64.b64decode(nonce_b64) + created.encode() + b"william2004").digest()).decode()
    assert digest == expect


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


GET_AUTO_RESPONSE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    '<s:Body><timg:GetImagingSettingsResponse xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl">'
    '<timg:ImagingSettings><tt:Focus xmlns:tt="http://www.onvif.org/ver10/schema">'
    '<tt:AutoFocusMode>AUTO</tt:AutoFocusMode></tt:Focus></timg:ImagingSettings>'
    '</timg:GetImagingSettingsResponse></s:Body></s:Envelope>'
).encode()

GET_NO_FOCUS_RESPONSE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    '<s:Body><timg:GetImagingSettingsResponse xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl">'
    '<timg:ImagingSettings/></timg:GetImagingSettingsResponse></s:Body></s:Envelope>'
).encode()

SET_OK_RESPONSE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    '<s:Body><timg:SetImagingSettingsResponse xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl"/>'
    '</s:Body></s:Envelope>'
).encode()

# Real shape measured against the phone for the controls it refuses (task context):
# SetVideoEncoderConfiguration answers ActionNotSupported. set_autofocus must read this as failure.
SET_FAULT_RESPONSE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    '<s:Body><s:Fault><s:Code><s:Value>s:Receiver</s:Value>'
    '<s:Subcode><s:Value>ter:ActionNotSupported</s:Value></s:Subcode></s:Code>'
    '<s:Reason><s:Text xml:lang="en">Action Not Supported</s:Text></s:Reason></s:Fault>'
    '</s:Body></s:Envelope>'
).encode()


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
