import os, time, shutil

class Archive:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def write_still(self, jpeg, ts):
        lt = time.localtime(ts)
        d = os.path.join(self.root, time.strftime("%Y/%m/%d", lt))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, time.strftime("%H%M%S", lt) + ".jpg")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(jpeg)
        os.replace(tmp, path)
        return path

    def free_bytes(self):
        return shutil.disk_usage(self.root).free
