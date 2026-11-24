import io
import socket
import threading
import time
from typing import Self, override

import pytest

import disc_reader
from builders import pes, pmt_packet, pmt_section, source_packets, ts_packets
from disc_reader import (
    AudioTap,
    Remux2D,
    _crc32_mpeg2,
    _pmt_streams,
    filter_pmt,
    pes_pts,
    shift_timestamps,
)
from ssif_demux import BASE_PID, timestamp

AUDIO, OTHER_AUDIO, PGS = 0x1100, 0x1101, 0x1200
KEY_PTS = 900_000                        # the keyframe we start from (low 9 bits clear)


def test_crc32_mpeg2() -> None:
    assert _crc32_mpeg2(b"123456789") == 0x0376E6E7          # the standard check value
    section = pmt_section([(0x1B, BASE_PID)])
    assert _crc32_mpeg2(section) == 0                         # a section with its CRC


def test_filter_pmt() -> None:
    section = pmt_section([(0x1B, BASE_PID), (0x20, 0x1012), (0x81, AUDIO), (0x86, OTHER_AUDIO),
                           (0x90, PGS)])
    out = filter_pmt(section, {BASE_PID, AUDIO, PGS})
    assert [pid for _, _, pid in _pmt_streams(out)] == [BASE_PID, AUDIO, PGS]
    assert 3 + (((out[1] & 0x0F) << 8) | out[2]) == len(out)
    assert _crc32_mpeg2(out) == 0


def test_shift_timestamps() -> None:
    p = bytearray(ts_packets(AUDIO, pes(b"x", 1000, 900, stream_id=0xBD))[0])
    shift_timestamps(p, 500)
    assert pes_pts(p) == 1500
    hdr = 5 + p[4]                                              # after the stuffing
    assert timestamp(bytes(p[hdr + 14:hdr + 19])) == 1400      # DTS too
    shift_timestamps(p, -2000)                        # wraps around 2^33
    assert pes_pts(p) == (1 << 33) - 500
    assert pes_pts(ts_packets(AUDIO, b"\x00\x00\x02" + b"\x00" * 20)[0]) is None


def disc_stream(first_video_pts: int = KEY_PTS - 2 * 3754) -> list[bytes]:
    """PAT, PMT, then video frames every 3754 ticks and audio frames every 2880,
    audio PTS slightly behind the video in the stream (as in a real TS)."""
    packets = [ts_packets(0, b"\x00" + b"PAT" * 5)[0],
               pmt_packet(pmt_section([(0x1B, BASE_PID), (0x81, AUDIO), (0x86, OTHER_AUDIO),
                                       (0x90, PGS)]))]
    audio_pts = first_video_pts - 20_000
    for n in range(6):
        vpts = first_video_pts + n * 3754
        packets += ts_packets(BASE_PID, pes(bytes([n]) * 400, vpts, vpts - 3000))
        while audio_pts < vpts + 3754:
            packets += ts_packets(AUDIO, pes(b"A" + audio_pts.to_bytes(5, "big"), audio_pts,
                                             stream_id=0xBD))
            packets += ts_packets(OTHER_AUDIO, pes(b"B", audio_pts, stream_id=0xBD))
            audio_pts += 2880
    return packets


def pids_and_pts(ts: bytes) -> list[tuple[int, int | None]]:
    out: list[tuple[int, int | None]] = []
    for o in range(0, len(ts), 188):
        p = ts[o:o + 188]
        pid = ((p[1] & 0x1F) << 8) | p[2]
        out.append((pid, pes_pts(p) if p[1] & 0x40 else None))
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Receiver:
    """ffmpeg's side of the audio: listens on loopback, keeps every byte it gets."""

    def __init__(self, port: int = 0) -> None:
        self.server = socket.create_server(("127.0.0.1", port))
        self.port: int = self.server.getsockname()[1]
        self.data = bytearray()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        with self.server, self.server.accept()[0] as conn:
            while chunk := conn.recv(65536):
                self.data += chunk

    def received(self) -> bytes:
        self.thread.join(10)
        assert not self.thread.is_alive()
        return bytes(self.data)


def test_audio_tap() -> None:
    ffmpeg = Receiver()
    tap = AudioTap(ffmpeg.port, {AUDIO}, KEY_PTS + 77)
    data = source_packets(disc_stream())
    for i in range(0, len(data), 6144):
        tap.feed(data[i:i + 6144])
    tap.close()
    ts = ffmpeg.received()
    got = pids_and_pts(ts)
    assert tap.anchor_pts == KEY_PTS
    assert {pid for pid, _ in got} == {0, 0x100, BASE_PID, AUDIO}   # OTHER_AUDIO dropped
    video = [pts for pid, pts in got if pid == BASE_PID and pts is not None]
    assert video == [KEY_PTS]                                   # only the anchor frame
    audio = [pts for pid, pts in got if pid == AUDIO and pts is not None]
    assert audio and audio[0] >= KEY_PTS and audio[0] - KEY_PTS < 2880
    assert audio == sorted(audio)
    # the PMT announces only the forwarded streams
    pmt = next(ts[o:o + 188] for o in range(0, len(got) * 188, 188)
               if ts[o + 2] == 0x00 and ts[o + 1] & 0x1F == 1)
    assert [pid for _, _, pid in _pmt_streams(pmt[5:])] == [BASE_PID, AUDIO]


def test_audio_tap_clip_range_and_shift() -> None:
    ffmpeg = Receiver()
    tap = AudioTap(ffmpeg.port, {AUDIO}, KEY_PTS)
    tap.set_clip(1_000_000, KEY_PTS, KEY_PTS + 10_000)
    tap.feed(source_packets(disc_stream()))
    tap.close()
    audio = [pts for pid, pts in pids_and_pts(ffmpeg.received()) if pid == AUDIO and pts]
    # only the PES inside [in, out) of the clip, moved by delta
    assert audio and all(KEY_PTS + 1_000_000 <= a < KEY_PTS + 1_010_000 for a in audio)


def test_audio_tap_connects_before_ffmpeg_listens() -> None:
    """procchain starts disc_reader before ffmpeg: the first attempts are refused,
    the tap tries again and every byte still arrives (#192)."""
    port = free_port()
    tap = AudioTap(port, {AUDIO}, KEY_PTS)
    data = source_packets(disc_stream())
    tap.feed(data)                       # queued while nobody listens
    time.sleep(0.5)                      # a few refused attempts
    ffmpeg = Receiver(port)
    tap.feed(data)
    tap.close()
    ts = ffmpeg.received()
    assert ts and AUDIO in {pid for pid, _ in pids_and_pts(ts)}


def test_audio_tap_gives_up_after_the_deadline(capfd: pytest.CaptureFixture[str]) -> None:
    """Nobody ever listens: past the deadline the tap logs it and drops the audio,
    and feeding it never blocks (the video must go on) (#192)."""
    tap = AudioTap(free_port(), {AUDIO}, KEY_PTS, connect_timeout=0.3)
    time.sleep(0.6)
    data = source_packets(disc_stream())
    for _ in range(5000):                # more than the queue holds
        tap.feed(data)
    tap.close()
    assert "no audio reader" in capfd.readouterr().err


def test_audio_tap_abort_stops_waiting_for_ffmpeg() -> None:
    """The video consumer is gone before ffmpeg listens: close(abort=True) does not
    wait for the whole deadline."""
    tap = AudioTap(free_port(), {AUDIO}, KEY_PTS, connect_timeout=60)
    began = time.monotonic()
    tap.close(abort=True)
    assert time.monotonic() - began < 5


def test_audio_tap_ffmpeg_goes_away() -> None:
    """ffmpeg closes its end (seek, stop): the tap keeps draining, close() returns."""
    server = socket.create_server(("127.0.0.1", 0))
    tap = AudioTap(server.getsockname()[1], {AUDIO}, KEY_PTS)
    server.settimeout(10)
    conn = server.accept()[0]
    conn.close()
    server.close()
    data = source_packets(disc_stream())
    for _ in range(5000):
        tap.feed(data)
    tap.close()


class FakeSocket:
    """A connected socket for AudioTap: its own address and peer, what sendall does."""

    def __init__(self, me: tuple[str, int], peer: tuple[str, int] | None,
                 fail: OSError | None = None) -> None:
        self.me, self.peer, self.fail, self.closed = me, peer, fail, False

    def getsockname(self) -> tuple[str, int]:
        return self.me

    def getpeername(self) -> tuple[str, int]:
        if self.peer is None:            # reset by the other side as soon as accepted
            raise OSError(107, "Transport endpoint is not connected")
        return self.peer

    def settimeout(self, value: float | None) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        if self.fail:
            raise self.fail

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.mark.parametrize("case", ["itself", "reset"])
def test_audio_tap_drops_an_unusable_socket(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    """TCP simultaneous open (source port == target port) connects a socket to itself,
    holding the port ffmpeg needs; or the connection is reset as soon as accepted:
    the tap closes it and tries again."""
    ffmpeg = Receiver()
    me = ("127.0.0.1", ffmpeg.port)
    itself = FakeSocket(me, me if case == "itself" else None)
    real = socket.create_connection
    attempts: list[object] = []

    def connect(address: tuple[str, int], timeout: float | None = None) -> object:
        attempts.append(address)
        return itself if len(attempts) == 1 else real(address, timeout=timeout)

    monkeypatch.setattr(disc_reader.socket, "create_connection", connect)
    tap = AudioTap(ffmpeg.port, {AUDIO}, KEY_PTS)
    tap.feed(source_packets(disc_stream()))
    tap.close()
    assert itself.closed and len(attempts) == 2
    assert AUDIO in {pid for pid, _ in pids_and_pts(ffmpeg.received())}


def test_audio_tap_survives_any_send_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not only reset/broken pipe: any OSError while sending (a timeout, Windows'
    WSAENETRESET) leaves the tap draining, so the video never stalls."""
    failing = FakeSocket(("127.0.0.1", 1), ("127.0.0.1", 2), fail=TimeoutError("timed out"))

    def connect(address: tuple[str, int], timeout: float | None = None) -> object:
        return failing

    monkeypatch.setattr(disc_reader.socket, "create_connection", connect)
    tap = AudioTap(2, {AUDIO}, KEY_PTS)
    data = source_packets(disc_stream())
    for _ in range(5000):                # more than the queue holds
        tap.feed(data)
    tap.close()
    assert failing.closed


def test_remux_2d() -> None:
    buf = io.BytesIO()
    remux = Remux2D(buf, {AUDIO}, KEY_PTS)
    data = source_packets(disc_stream())
    for i in range(0, len(data), 6144):
        remux.feed(data[i:i + 6144])
    got = pids_and_pts(buf.getvalue())
    video = [pts for pid, pts in got if pid == BASE_PID and pts is not None]
    assert video == [KEY_PTS + n * 3754 for n in range(4)]     # from the keyframe on
    audio = [pts for pid, pts in got if pid == AUDIO and pts is not None]
    assert audio[0] >= KEY_PTS
    assert OTHER_AUDIO not in {pid for pid, _ in got}


def test_remux_2d_broken_pipe() -> None:
    class Closed(io.BytesIO):
        @override
        def write(self, b: object, /) -> int:
            raise BrokenPipeError

    remux = Remux2D(Closed(), {AUDIO}, KEY_PTS)
    remux.feed(source_packets(disc_stream()))
    assert remux.broken
