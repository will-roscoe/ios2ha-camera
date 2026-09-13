"""WS-Security SOAP client for the phone camera's ONVIF imaging service.

The camera app's /onvif/* endpoints need a WS-Security UsernameToken digest in
the SOAP header, not HTTP auth -- HTTP Basic only works on the media endpoints
(/snapshot.jpg, /mjpeg). Autofocus mode is the only imaging control exposed
here because it is the only one confirmed to change device behaviour:
brightness is accepted and does nothing, exposure is absent, and
SetVideoEncoderConfiguration answers ActionNotSupported. Measured against the
real phone, not assumed from the ONVIF spec.
"""

import base64
import datetime
import hashlib
import os
import re
import urllib.request

IMG_NS = "http://www.onvif.org/ver20/imaging/wsdl"
TOKEN = "video-source-1"
TIMEOUT_S = 10  # finite and short: a hung call must never stall the capture loop


def wsse_header(user, password):
    nonce = os.urandom(16)
    created = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        '<s:Header><wsse:Security s:mustUnderstand="1" '
        'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        '<wsse:UsernameToken><wsse:Username>%s</wsse:Username>'
        '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">%s</wsse:Password>'
        '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">%s</wsse:Nonce>'
        '<wsu:Created>%s</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>'
    ) % (user, digest, base64.b64encode(nonce).decode(), created)


class OnvifClient:
    def __init__(self, host, port, user, password):
        self.url = f"http://{host}:{port}/onvif/imaging_service"
        self.user, self.password = user, password

    def _soap(self, body):
        env = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
               '%s<s:Body>%s</s:Body></s:Envelope>') % (wsse_header(self.user, self.password), body)
        req = urllib.request.Request(self.url, data=env.encode(), method="POST")
        req.add_header("Content-Type", "application/soap+xml; charset=utf-8")
        return urllib.request.urlopen(req, timeout=TIMEOUT_S).read().decode("utf-8", "replace")

    def get_autofocus(self):
        r = self._soap('<GetImagingSettings xmlns="%s"><VideoSourceToken>%s</VideoSourceToken></GetImagingSettings>' % (IMG_NS, TOKEN))
        m = re.search(r"<\w*:?AutoFocusMode>([^<]+)<", r)
        return m.group(1) if m else "unknown"

    def set_autofocus(self, mode):
        body = ('<SetImagingSettings xmlns="%s"><VideoSourceToken>%s</VideoSourceToken>'
                '<ImagingSettings xmlns:tt="http://www.onvif.org/ver10/schema">'
                '<tt:Focus><tt:AutoFocusMode>%s</tt:AutoFocusMode></tt:Focus></ImagingSettings>'
                '<ForcePersistence>false</ForcePersistence></SetImagingSettings>') % (IMG_NS, TOKEN, mode)
        r = self._soap(body)
        return "Fault" not in r
