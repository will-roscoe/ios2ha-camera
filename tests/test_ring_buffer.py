from ios2ha_camera.ring_buffer import RingBuffer, Frame


def test_evicts_oldest_over_byte_budget():
    rb = RingBuffer(max_bytes=300)
    for i in range(5):
        rb.append(Frame(b"x" * 100, ts=float(i)))
    assert rb.total_bytes <= 300
    tss = [f.ts for f in rb.recent(10)]
    assert tss == [2.0, 3.0, 4.0]           # oldest two evicted


def test_latest_and_recent_order():
    rb = RingBuffer(max_bytes=10_000)
    for i in range(4):
        rb.append(Frame(b"y" * 10, ts=float(i)))
    assert rb.latest().ts == 3.0
    assert [f.ts for f in rb.recent(2)] == [2.0, 3.0]


def test_empty():
    rb = RingBuffer(max_bytes=100)
    assert rb.latest() is None and rb.newest_ts() is None


def test_recent_zero_returns_nothing():
    """list(dq)[-0:] is the whole deque, so a naive slice returns everything."""
    rb = RingBuffer(max_bytes=10_000)
    for i in range(4):
        rb.append(Frame(b"y" * 10, ts=float(i)))
    assert rb.recent(0) == []


def test_recent_more_than_held_returns_all():
    rb = RingBuffer(max_bytes=10_000)
    for i in range(3):
        rb.append(Frame(b"y" * 10, ts=float(i)))
    assert [f.ts for f in rb.recent(99)] == [0.0, 1.0, 2.0]


def test_total_bytes_tracks_contents():
    rb = RingBuffer(max_bytes=10_000)
    rb.append(Frame(b"z" * 40, ts=0.0))
    rb.append(Frame(b"z" * 60, ts=1.0))
    assert rb.total_bytes == 100


def test_keeps_one_frame_larger_than_the_budget():
    """A single frame over budget must still be retrievable, not evicted to empty."""
    rb = RingBuffer(max_bytes=50)
    rb.append(Frame(b"w" * 500, ts=7.0))
    assert rb.latest().ts == 7.0
    assert rb.newest_ts() == 7.0


def test_concurrent_appends_keep_byte_count_consistent():
    import threading

    rb = RingBuffer(max_bytes=10_000_000)
    def worker(base):
        for i in range(200):
            rb.append(Frame(b"q" * 50, ts=float(base + i)))

    threads = [threading.Thread(target=worker, args=(t * 1000,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert rb.total_bytes == 4 * 200 * 50
