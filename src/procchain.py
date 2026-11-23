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
from collections.abc import Sequence
from typing import IO, Any

log = logging.getLogger("bd3d_fs")


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
        for argv in stages:
            proc = subprocess.Popen(
                list(argv), stdin=upstream if upstream is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=stderr, env=full_env,
            )
            if upstream is not None:
                upstream.close()  # the child inherited it already
            upstream = proc.stdout
            self.processes.append(proc)
        if upstream is None:
            raise RuntimeError("the chain has no output pipe")
        self.stdout = upstream

    def failed(self) -> bool:
        return any(p.poll() not in (None, 0) for p in self.processes)

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
