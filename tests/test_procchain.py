"""procchain.ProcessChain: chaining stages without a shell, pipefail semantics
(failed()) and stopping every process in the chain without a process group."""
import logging
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from procchain import ProcessChain

CAT = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]


def test_stdout_flows_through_every_stage() -> None:
    chain = ProcessChain([
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'hello')"],
        CAT,
        CAT,
    ])
    try:
        assert chain.stdout.read() == b"hello"
        assert chain.processes[-1].wait(5) == 0
    finally:
        chain.stop(5)


def test_single_stage() -> None:
    chain = ProcessChain([[sys.executable, "-c", "print('ok')"]])
    try:
        assert chain.stdout.read().strip() == b"ok"
    finally:
        chain.stop(5)


def test_empty_chain_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one stage"):
        ProcessChain([])


def test_env_reaches_every_stage() -> None:
    code = "import os, sys; sys.stdout.write(os.environ.get('SPINEMA_TEST', '?'))"
    chain = ProcessChain([[sys.executable, "-c", code]], env={"SPINEMA_TEST": "yes"})
    try:
        assert chain.stdout.read() == b"yes"
    finally:
        chain.stop(5)


def test_a_stage_failing_fails_the_chain() -> None:
    """pipefail: stage 0 fails; stage 1 still reads its (empty) output to a clean
    exit, which naive piping would read as the whole chain having succeeded."""
    chain = ProcessChain([
        [sys.executable, "-c", "import sys; sys.exit(1)"],
        CAT,
    ])
    try:
        assert chain.stdout.read() == b""
        chain.processes[-1].wait(5)
        assert chain.processes[-1].returncode == 0        # the last stage itself is fine
        assert chain.failed()
    finally:
        chain.stop(5)


def test_a_successful_chain_has_not_failed() -> None:
    chain = ProcessChain([CAT, CAT])
    try:
        chain.stdout.read()
        for p in chain.processes:
            p.wait(5)
        assert not chain.failed()
    finally:
        chain.stop(5)


def test_stop_kills_every_process() -> None:
    sleepy = [sys.executable, "-c", "import time; time.sleep(60)"]
    chain = ProcessChain([sleepy, sleepy])
    started = time.monotonic()
    chain.stop(5)
    assert time.monotonic() - started < 5                 # killed, not waited out
    assert all(p.poll() is not None for p in chain.processes)


def test_stop_does_not_hang_on_a_stuck_process(caplog: pytest.LogCaptureFixture) -> None:
    """A process stuck on the drive (uninterruptible sleep) survives SIGKILL:
    stop() still returns, and logs that one was left behind."""
    chain = object.__new__(ProcessChain)
    proc = Mock(pid=4242, args=["ffmpeg"])
    proc.poll.return_value = None
    proc.wait.side_effect = subprocess.TimeoutExpired(["ffmpeg"], 5)
    chain.processes = [proc]
    caplog.set_level(logging.WARNING, logger="bd3d_fs")
    chain.stop(5)
    proc.kill.assert_called_once()
    proc.wait.assert_called_once_with(5)
    assert "did not exit 5s after being killed" in caplog.text
