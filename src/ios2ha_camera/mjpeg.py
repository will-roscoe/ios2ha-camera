"""Parse a multipart/x-mixed-replace stream into JPEG payloads.

Guardian Eye sends a `guardianeyeframe` boundary with a Content-Length on every
part, so parts are cut on the declared length. The fallback marker scan is only
for a part that omits it: scanning is not safe in general, because a frame
carrying an EXIF thumbnail contains a second SOI/EOI pair and would be cut short.
"""

import logging

log = logging.getLogger(__name__)


class PartTooLarge(Exception):
    """Raised when the buffer grows past the cap without a part completing.

    The caller reconnects. Without a cap, a stream that never terminates a part
    grows the buffer until the process is killed.
    """


def iter_multipart(stream, boundary: bytes, chunk_size: int = 65536,
                   max_part_bytes: int = 16 << 20):
    """Yield JPEG payloads from a multipart/x-mixed-replace HTTP response."""
    marker = b"--" + boundary
    # read1 returns what is already buffered instead of waiting for a full
    # chunk_size, which keeps per-frame latency down on a chunked transfer.
    read = getattr(stream, "read1", None) or stream.read
    buf = bytearray()

    def fill():
        chunk = read(chunk_size)
        if not chunk:
            return False
        buf.extend(chunk)
        if len(buf) > max_part_bytes:
            raise PartTooLarge(f"no complete part within {max_part_bytes} bytes")
        return True

    def find_from(needle, start):
        """Index of needle at or after start, filling the buffer until found.
        None if the stream ends first."""
        while True:
            at = buf.find(needle, start)
            if at >= 0:
                return at
            if not fill():
                return None

    while True:
        start = find_from(marker, 0)
        if start is None:
            return
        head_end = find_from(b"\r\n\r\n", start)
        if head_end is None:
            return
        body = head_end + 4
        length = _content_length(buf[start + len(marker):head_end])

        if length is None:
            soi = find_from(b"\xff\xd8\xff", body)
            if soi is None:
                return
            eoi = find_from(b"\xff\xd9", soi + 3)
            if eoi is None:
                return
            end = eoi + 2
            payload = bytes(buf[soi:end])
        else:
            end = body + length
            while len(buf) < end:
                if not fill():
                    return
            payload = bytes(buf[body:end])
            if not _is_whole_jpeg(payload):
                # The declared length disagrees with the bytes that followed. A
                # short one truncates the frame, a long one swallows the next
                # part's boundary and loses a frame -- both silently, and a
                # corrupt frame would go straight into the stack. Drop this part
                # and resync on the next boundary rather than yield it.
                log.warning("part declared %d bytes but is not a whole JPEG; resyncing", length)
                del buf[:body]
                continue

        del buf[:end]
        yield payload


def _is_whole_jpeg(payload: bytes) -> bool:
    return payload.startswith(b"\xff\xd8\xff") and payload.endswith(b"\xff\xd9")


def _content_length(headers: bytes):
    for line in bytes(headers).split(b"\r\n"):
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None
