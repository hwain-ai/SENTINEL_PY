"""Bind unlimited Linux test processes to their launching checker."""

import ctypes
import os
import signal
import sys


def arm_parent_death(expected_parent):
    if sys.platform != "linux":
        return
    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "parent death signal unavailable")
    if os.getppid() != expected_parent:
        os.kill(os.getpid(), signal.SIGKILL)


def bind_forked_children():
    expected = [os.getpid()]

    def before_fork():
        expected[0] = os.getpid()

    def after_fork():
        arm_parent_death(expected[0])

    os.register_at_fork(before=before_fork, after_in_child=after_fork)
