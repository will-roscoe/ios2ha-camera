"""Byte-bounded FIFO of compressed frames.

Frames are kept compressed so several minutes of history fit in memory: a decoded
1080x1920 frame is about 6 MB against 140-450 KB compressed.
"""

import threading
from collections import deque
from dataclasses import dataclass
from itertools import islice


@dataclass
class Frame:
    data: bytes
    ts: float


class RingBuffer:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._dq: deque[Frame] = deque()
        self._bytes = 0
        self._lock = threading.Lock()

    def append(self, frame: Frame) -> None:
        with self._lock:
            self._dq.append(frame)
            self._bytes += len(frame.data)
            # Keep the newest frame even when it alone exceeds the budget,
            # so the buffer never reads as empty while the stream is healthy.
            while self._bytes > self.max_bytes and len(self._dq) > 1:
                old = self._dq.popleft()
                self._bytes -= len(old.data)

    def latest(self):
        with self._lock:
            return self._dq[-1] if self._dq else None

    def recent(self, n):
        """The newest n frames, oldest first. islice avoids copying the whole
        deque, and clamping the start keeps recent(0) empty -- list(dq)[-0:]
        would return everything."""
        with self._lock:
            start = max(0, len(self._dq) - max(0, n))
            return list(islice(self._dq, start, len(self._dq)))

    def newest_ts(self):
        f = self.latest()
        return f.ts if f else None

    @property
    def total_bytes(self):
        return self._bytes
