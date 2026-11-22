"""Own a Linux command tree until its last process has been reaped."""

import ctypes
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path


class Tree:
    def __init__(self, args):
        # Orphaned grandchildren become ours, including children that leave the group.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "Cannot supervise command descendants")
        self.children = Path(f"/proc/self/task/{os.getpid()}/children")
        self.process = subprocess.Popen(args, start_new_session=True)

    def pids(self):
        return [int(pid) for pid in self.children.read_text().split()]

    def reap(self):
        self.process.poll()
        for pid in self.pids():
            if pid != self.process.pid:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
        return self.pids()

    def signal(self, sig, children):
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass
        self.signal_adopted(sig, children)

    def signal_adopted(self, sig, children):
        for pid in children:
            try:
                if os.getpgid(pid) != self.process.pid:
                    os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def close(self, children):
        self.signal(signal.SIGTERM, children)
        signalled = set(children)
        deadline = time.monotonic() + 2
        while children := self.reap():
            if time.monotonic() >= deadline:
                self.signal(signal.SIGKILL, children)
            else:
                self.signal_adopted(signal.SIGTERM, set(children) - signalled)
                signalled.update(children)
            time.sleep(0.02)


def main():
    lifetime = int(sys.argv[1])
    # The remaining inherited descriptors are ownership locks. Popen closes them
    # in the managed command; this supervisor's copies live until it exits.
    if select.select([lifetime], [], [], 0)[0]:
        return 1
    tree = Tree(sys.argv[2:])
    try:
        while children := tree.reap():
            if tree.process.returncode is not None or select.select([lifetime], [], [], 0.04)[0]:
                break
    finally:
        tree.close(tree.pids())
    code = tree.process.returncode
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OSError as exc:
        sys.exit(str(exc))
