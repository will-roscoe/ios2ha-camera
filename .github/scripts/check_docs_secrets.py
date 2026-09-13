#!/usr/bin/env python3
"""Fail the docs build if a credential reached the rendered HTML.

The documentation site is published publicly from a private repository, so
anything Sphinx renders is world-readable. Documenting that a variable named
``MQTT_PASSWORD`` exists is fine and necessary; rendering its *value* is not.

This scans for a secret-shaped name being *assigned* something that is not
obviously a placeholder, plus a few formats that are secrets whatever they are
called (JWTs, PEM private keys). It deliberately does not try to be a general
secret scanner: a check that cries wolf on prose gets switched off.

Usage:  check_docs_secrets.py <built-html-dir>
Exit:   0 clean, 1 findings (printed with file and line).
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

# Names that make a value sensitive. Matched case-insensitively against the
# identifier immediately left of the assignment.
SECRET_NAME = (
    r"(?:[A-Za-z0-9_]*"
    r"(?:PASSWORD|PASSWD|SECRET|TOKEN|APIKEY|API_KEY|LLT|CREDENTIALS?"
    r"|PRIVATE_KEY|ACCESS_KEY)"
    r"[A-Za-z0-9_]*)"
)

# NAME=value or NAME: value. Applied to tag-stripped text, never to raw HTML:
# Sphinx renders source through Pygments, which wraps every token in its own
# <span>, so in the markup the name and the "=" are never adjacent. Matching the
# raw HTML silently scans nothing under _modules/ -- verified by planting a
# credential in a module and watching this check pass.
ASSIGNMENT = re.compile(rf"\b({SECRET_NAME})\s*[=:]\s*[\"'`]?([^\s\"'`|,)]{{4,}})", re.IGNORECASE)

TAG = re.compile(r"<[^>]+>")


def visible_text(line: str) -> str:
    """The text a reader sees: markup removed, entities decoded."""
    return html.unescape(TAG.sub("", line))

# Formats that are secrets regardless of the name attached to them.
ALWAYS = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
     "a JWT (Home Assistant long-lived token)"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "a PEM private key"),
]

# Values that document a variable rather than leak one. A placeholder is the
# normal case in docs, so this list has to be generous or the check is noise.
PLACEHOLDER = re.compile(
    r"^(?:"
    r"[.]{3}|[-–—]+|"                       # ... or a dash used as "none"
    r"[<${(\[].*|"                          # <your-password>, ${VAR}, (unset)
    r"(?:x{3,}|\*{3,}|\.{3,})|"             # xxxx, ****
    r"(?:your|my|some|the|a|an|no|not|if|is|it|to|for|and|or|see|use|any)\b.*|"
    r"(?:none|null|unset|empty|blank|changeme|placeholder|redacted|example|"
    r"todo|tbd|required|optional|string|value|password|secret|token|true|false)"
    r")$",
    re.IGNORECASE,
)


# Known-safe values whose *name* looks sensitive but whose meaning is not. Keyed
# on the value, not the name, so the same name carrying anything else still fires.
# Default-deny with explicit exceptions: a new legitimate hit should force someone
# to think about it, rather than being quietly excluded by a cleverer pattern.
ALLOWED_VALUES = {
    # ONVIF uses "token" to mean a resource handle. This is the imaging service's
    # video source identifier, fixed by the protocol, not a credential.
    "video-source-1",
}


def suspicious(name: str, value: str) -> bool:
    value = value.strip().strip("\"'`")
    if not value or value in ALLOWED_VALUES or PLACEHOLDER.match(value):
        return False
    # A bare word with no digits or punctuation is almost always prose or an
    # identifier being discussed, not a credential.
    if value.isalpha() and len(value) < 12:
        return False
    return True


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(argv[1])
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    findings: list[str] = []
    scanned = 0
    for path in sorted(root.rglob("*.html")):
        scanned += 1
        for lineno, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
            line = visible_text(raw)
            for pattern, what in ALWAYS:
                if pattern.search(line):
                    findings.append(f"{path.relative_to(root)}:{lineno}: {what}")
            for name, value in ASSIGNMENT.findall(line):
                if suspicious(name, value):
                    findings.append(
                        f"{path.relative_to(root)}:{lineno}: {name} assigned a value "
                        f"({value[:12]}...)"
                    )

    if findings:
        print(f"Credentials found in {len(findings)} place(s) in the rendered docs:\n")
        for f in findings:
            print(f"  {f}")
        print(
            "\nThe docs site is published publicly. Remove the value, or replace it "
            "with a placeholder such as <your-password>."
        )
        return 1

    print(f"No credentials in the rendered docs ({scanned} HTML files scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
