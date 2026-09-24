"""Run independent measurements in supervised processes, without worker threads."""

from __future__ import annotations

import multiprocessing
from multiprocessing.connection import wait
import os
import signal
import tempfile
from pathlib import Path


class ParallelExecutionError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _worker(connection, function, directory):
    def interrupt(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    tempfile.tempdir = directory
    os.environ["TMPDIR"] = directory
    try:
        connection.send((True, function()))
    except BaseException as error:
        connection.send((False, error))
    finally:
        connection.close()


def _stop(processes):
    for process in processes:
        if process.is_alive():
            process.terminate()
    for process in processes:
        process.join(10)
    failed = False
    for process in processes:
        if process.is_alive():
            process.kill()
            process.join()
            failed = True
    if failed:
        raise ParallelExecutionError("parallelCleanupFailed")


def _receive(connections):
    pending = dict(enumerate(connections))
    results = [None, None]
    while pending:
        for connection in wait(list(pending.values())):
            index = next(key for key, value in pending.items() if value is connection)
            try:
                succeeded, result = connection.recv()
            except EOFError as error:
                raise ParallelExecutionError("parallelWorkerFailed") from error
            del pending[index]
            if not succeeded:
                raise result
            results[index] = result
    return tuple(results)


def run_pair(first, second, execution_mode="parallel"):
    """Keep result order stable and stop the sibling on an execution failure."""
    if execution_mode == "sequential":
        return first(), second()
    if execution_mode != "parallel":
        raise ValueError("invalidExecutionMode")
    context = multiprocessing.get_context("fork")
    processes, connections = [], []
    with tempfile.TemporaryDirectory(prefix="sentinel-parallel-") as directory:
        try:
            for index, function in enumerate((first, second)):
                work = Path(directory) / str(index)
                work.mkdir(mode=0o700)
                parent, child = context.Pipe(duplex=False)
                process = context.Process(target=_worker, args=(child, function, str(work)))
                process.start()
                child.close()
                processes.append(process)
                connections.append(parent)
            results = _receive(connections)
            for process in processes:
                process.join()
                if process.exitcode != 0:
                    raise ParallelExecutionError("parallelWorkerFailed")
            return results
        finally:
            _stop(processes)
            for connection in connections:
                connection.close()
