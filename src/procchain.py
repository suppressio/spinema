"""procchain.py — chain subprocess stages like a shell pipe (`a | b | c`), but
from Python, with no shell and no process group: one subprocess.Popen per
stage, each stage's stdout wired into the next one's stdin. The same on Linux,
Windows and macOS (no os.killpg, no job object): stopping kills every process
in the list this module keeps, straight from Python.

`failed()` mirrors bash's `pipefail`: a stage that exits nonzero fails the
whole chain even once a later stage has gone on to read its (truncated)
output and exited 0 itself, which naive piping would otherwise hide.
"""
import contextlib
import logging
import os
import subprocess
import time
from collections.abc import Sequence
from typing import IO, Any

log = logging.getLogger("bd3d_fs")
KILL_WAIT = 5      # seconds killed stages get to exit (library.py has its own, for Generator)


class ProcessChain:
    """`stages`: one argv per stage, stage 0 first. `env`: extra variables over
    `os.environ`, for every stage (only disc_reader.py reads them; the rest
    ignore what they don't know). `stderr`: where every stage's standard error
    goes, shared, as when bash's own pipe stages all inherit one fd."""

    def __init__(self, stages: Sequence[Sequence[str]], *, env: dict[str, str] | None = None,
                 stderr: IO[Any] | int | None = None) -> None:
        if not stages:
            raise ValueError("a chain needs at least one stage")
        full_env = {**os.environ, **env} if env else None
        self.processes: list[subprocess.Popen[bytes]] = []
        upstream: IO[bytes] | None = None
        try:
            for argv in stages:
                # start_new_session: a Ctrl+C in the terminal goes to the program,
                # which stops its pipelines itself (POSIX; ignored on Windows)
                proc = subprocess.Popen(
                    list(argv), stdin=upstream if upstream is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=stderr, env=full_env,
                    start_new_session=True,
                )
                self.processes.append(proc)  # first: whatever fails next, stop() reaches it
                if upstream is not None:
                    upstream.close()  # the child inherited it already
                upstream = proc.stdout
        except BaseException:
            # a stage that cannot start (its program missing) must not leave the
            # earlier ones running, with nothing left to stop them
            if upstream is not None:
                upstream.close()
            self.stop(KILL_WAIT)
            raise
        if upstream is None:
            raise RuntimeError("the chain has no output pipe")
        self.stdout = upstream

    def failed(self) -> bool:
        return any(p.poll() not in (None, 0) for p in self.processes)

    def finish(self, timeout: float) -> bool:
        """Once the output reached EOF: wait up to `timeout` in all for every stage
        to exit, then say whether one failed (pipefail). An earlier stage can close
        its output before it exits with an error, so polling it right after the last
        stage exits could miss that. A stage still running at the deadline is not
        counted as failed, and is left to stop()."""
        deadline = time.monotonic() + timeout
        for p in self.processes:
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(max(0.0, deadline - time.monotonic()))
        return self.failed()

    def stop(self, timeout: float) -> None:
        """Kill every process still running, then wait up to `timeout` for each:
        SIGKILL (or, on Windows, TerminateProcess) waits for a process stuck in
        the kernel (a drive that does not answer a read), so this must not hang
        an eject or the program's exit on it."""
        for p in self.processes:
            if p.poll() is None:
                with contextlib.suppress(OSError):
                    p.kill()
        for p in self.processes:
            try:
                p.wait(timeout)
            except subprocess.TimeoutExpired:
                log.warning("%s: did not exit %ds after being killed: left behind",
                            p.args, timeout)
