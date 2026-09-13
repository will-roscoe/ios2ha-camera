"""The vocabulary of `stream_state`, shared by everything that reports one.

This lives on its own rather than in `upstream` or `ingest` because both of them
report states and `upstream` already imports `ingest`; putting it in either would
make that a cycle. It is shared vocabulary, owned by neither.

Keeping it in one place is not tidiness. Home Assistant rejects a state that is
not among an enum sensor's declared options, so a value invented in one module
and not declared in the discovery payload does not fail loudly -- the entity just
goes unavailable and stays that way.
"""

# Frames are arriving. The three differ only in who they are arriving for.
ACTIVE_STATES = ("streaming", "capturing", "in_use")

# Every value `stream_state` can take, ordered as a story: unreachable, then
# reachable but not sending, then sending.
#
#   unavailable    nothing answers: off the network, or the connection times out
#   backgrounded   the port refuses, so the phone answers but the app is not
#                  listening -- iOS suspends a backgrounded app's sockets while
#                  leaving the process alive
#   streaming_off  the app answered 503: it is open, but Start streaming has not
#                  been tapped, and only a person can fix that
#   ready          reachable and willing, just not connected to at the moment
#   streaming      frames flowing with nobody holding the connection, which
#                  happens when `on_demand` is off and it is simply always open
#   capturing      frames flowing for a still
#   in_use         frames flowing for a viewer; wins over capturing when both
#                  apply, because a viewer is a person waiting on the picture
STREAM_STATES = ("unavailable", "backgrounded", "streaming_off", "ready") + ACTIVE_STATES


def classify_probe_failure(exc):
    """Why a connection to the camera app failed, as far as one TCP connect tells.

    Measured on the device on 2026-09-13 while the app sat backgrounded: the phone
    answered ICMP at 70-149 ms -- elevated, because backgrounding drops the WiFi
    radio into power save -- while the app's port gave an immediate ECONNREFUSED.
    iOS suspends a backgrounded app's listening sockets but leaves the process
    running, so the IP stack is up with nothing bound to that port.

    A phone genuinely off the network cannot refuse anything: it times out, or the
    route fails. The distinction is therefore free, since the connection is being
    made anyway; it only has to stop flattening both outcomes into one state.

    A refusal cannot prove the app is suspended rather than killed -- both look
    identical over WiFi. The `app_running` boolean, read over the cable, is what
    separates those two, and it is why that sensor exists.
    """
    if isinstance(exc, ConnectionRefusedError):
        return "backgrounded"
    return "unavailable"
