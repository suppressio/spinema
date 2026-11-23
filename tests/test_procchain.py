"""procchain.ProcessChain: chaining stages without a shell, pipefail semantics
(failed()) and stopping every process in the chain without a process group."""
import logging
import subprocess
import sys
import time
from typing import Any, cast
from unittest.mock import Mock

import pytest

import procchain
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


def test_a_stage_that_cannot_start_leaves_no_process_behind(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A stage whose program is missing raises from Popen: the stages already
    started must not be left running (stage 0 holding the drive, forever)."""
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:  # noqa: ANN401
        proc = cast("subprocess.Popen[bytes]", real_popen(*args, **kwargs))
        started.append(proc)
        return proc

    monkeypatch.setattr(procchain.subprocess, "Popen", recording_popen)
    sleepy = [sys.executable, "-c", "import time; time.sleep(60)"]
    with pytest.raises(FileNotFoundError):
        ProcessChain([sleepy, ["/no/such/program-spinema-test"]])
    assert len(started) == 1
    assert started[0].poll() is not None                  # killed and reaped


def test_finish_sees_an_early_stage_failing_after_closing_its_output() -> None:
    """Stage 0 closes its stdout (the next stage sees EOF and exits 0) and only
    then exits 1: waiting for the last stage alone and polling the rest would
    miss that failure."""
    late_failure = [sys.executable, "-c",
                    "import os, sys, time; os.close(1); time.sleep(0.5); sys.exit(1)"]
    chain = ProcessChain([late_failure, CAT])
    try:
        assert chain.stdout.read() == b""
        assert chain.finish(5) is True
    finally:
        chain.stop(5)


def test_finish_is_bounded_when_a_stage_closes_its_output_but_does_not_exit() -> None:
    lingering = [sys.executable, "-c", "import os, time; os.close(1); time.sleep(60)"]
    chain = ProcessChain([lingering])
    try:
        assert chain.stdout.read() == b""
        started = time.monotonic()
        assert chain.finish(0.5) is False                 # still running: not a failure
        assert time.monotonic() - started < 3
    finally:
        chain.stop(5)


def test_finish_of_a_successful_chain() -> None:
    chain = ProcessChain([CAT, CAT])
    try:
        chain.stdout.read()
        assert chain.finish(5) is False
    finally:
        chain.stop(5)


def test_stages_run_in_their_own_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """As before the chain left bash: a Ctrl+C in the terminal reaches the program,
    which stops its pipelines itself, not every ffmpeg straight away."""
    kwargs_seen: list[dict[str, Any]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:  # noqa: ANN401
        kwargs_seen.append(kwargs)
        return cast("subprocess.Popen[bytes]", real_popen(*args, **kwargs))

    monkeypatch.setattr(procchain.subprocess, "Popen", recording_popen)
    chain = ProcessChain([CAT])
    chain.stop(5)
    assert kwargs_seen[0]["start_new_session"] is True
