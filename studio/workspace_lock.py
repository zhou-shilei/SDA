"""Reentrant thread and process lock for the shared JSON artifact registry."""

import fcntl
import threading


class WorkspaceLock:
    def __init__(self, path):
        self.path = path
        self.thread_lock = threading.RLock()
        self.depth = 0
        self.file = None

    def __enter__(self):
        self.thread_lock.acquire()
        try:
            if not self.depth:
                self.file = self.path.open("a+")
                fcntl.flock(self.file, fcntl.LOCK_EX)
            self.depth += 1
            return self
        except BaseException:
            if self.file is not None:
                self.file.close()
                self.file = None
            self.thread_lock.release()
            raise

    def __exit__(self, *exc):
        self.depth -= 1
        if not self.depth:
            self.file.close()
            self.file = None
        self.thread_lock.release()
