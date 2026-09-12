import time, os
from ios2ha_camera.archive import Archive

def test_writes_dated_path(tmp_path):
    a = Archive(str(tmp_path))
    p = a.write_still(b"\xff\xd8\xff\xd9", ts=time.time())
    assert os.path.exists(p) and p.endswith(".jpg")
    parts = p.replace(str(tmp_path) + os.sep, "").split(os.sep)
    assert len(parts) == 4    # YYYY/MM/DD/HHMMSS.jpg

def test_free_bytes_positive(tmp_path):
    assert Archive(str(tmp_path)).free_bytes() > 0
