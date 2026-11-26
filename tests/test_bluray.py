import ctypes
import subprocess

import pytest

import bluray

FOUND = ("internal.c:158: [bdplus] Found cached conversion table at "
         "/home/u/.config/bdplus/convtab/839F01323FC980EF69E9EF047CDA0EAC_PREDATOR_2_F4.bin\n")
MISSING = "internal.c:160: [bdplus] No cached conversion table found\n"


@pytest.mark.parametrize(("stderr", "found"), [(FOUND, True), (MISSING, False), ("", False)])
def test_bdplus_table_found_reads_the_libbdplus_log(
        monkeypatch: pytest.MonkeyPatch, stderr: str, found: bool) -> None:
    calls: list[dict[str, object]] = []

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        assert cmd[-1] == "/dev/sr0"
        calls.append(kw)
        return subprocess.CompletedProcess(cmd, 0, "", stderr)

    monkeypatch.setattr(bluray.subprocess, "run", run)
    monkeypatch.setenv("BD_DEBUG_MASK", "0")             # would hide libbdplus' line
    assert bluray.bdplus_table_found("/dev/sr0") is found
    env = calls[0]["env"]
    assert isinstance(env, dict) and "BD_DEBUG_MASK" not in env



class LogLib:
    """libbluray's log functions, for fakes that open a disc."""
    mask = 0xFFFFFFFF
    handler: int | None = None

    def bd_get_debug_mask(self) -> int:
        return type(self).mask

    def bd_set_debug_mask(self, mask: int) -> None:
        type(self).mask = mask

    def bd_set_debug_handler(self, handler: int | None) -> None:
        type(self).handler = handler

    def log(self, message: bytes) -> None:
        """What libbluray's bd_debug() does with a handler set."""
        handler = type(self).handler
        if handler is not None:
            ctypes.cast(handler, bluray._LOG_FUNC)(message)

def test_bdplus_table_found_gives_up_on_a_stuck_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 120)

    monkeypatch.setattr(bluray.subprocess, "run", run)
    assert not bluray.bdplus_table_found("/dev/sr0")


@pytest.mark.parametrize("bdplus", [True, False])
def test_disc_starts_libbdplus_on_bdplus_discs(monkeypatch: pytest.MonkeyPatch,
                                               bdplus: bool) -> None:
    # libbluray loads the cached conversion table only on bd_get_titles():
    # without it BD+ clips are read unfixed (#87)
    titles: list[object] = []

    class Info:
        bdplus_detected = bdplus

    class Ptr:
        contents = Info()

    class Lib(LogLib):
        def bd_open(self, path: bytes, key: None) -> int:
            return 1

        def bd_get_disc_info(self, bd: int) -> Ptr:
            return Ptr()

        def bd_get_titles(self, bd: int, flags: int, min_seconds: int) -> int:
            titles.append(bd)
            return 0

        def bd_close(self, bd: int) -> None:
            pass

    monkeypatch.setattr(bluray, "_libbluray", Lib)
    disc = bluray.Disc("/dev/sr0")
    assert titles == ([1] if bdplus else [])
    disc.close()


@pytest.mark.parametrize(("sep", "expected"), [("/", b"BDMV/STREAM/SSIF/00098.ssif"),
                                               ("\\", b"BDMV\\STREAM\\SSIF\\00098.ssif")])
def test_streams_are_opened_with_the_native_separator(
        monkeypatch: pytest.MonkeyPatch, sep: str, expected: bytes) -> None:
    # libbluray decrypts a file only when its path starts with "BDMV" DIR_SEP
    # "STREAM", and DIR_SEP is "\\" on Windows: with "/" it hands back the
    # encrypted bytes, and every pipeline fails (#104, seen on Windows 11)
    opened: list[bytes] = []

    class Info:
        bdplus_detected = False

    class Ptr:
        contents = Info()

    class Lib(LogLib):
        def bd_open(self, path: bytes, key: None) -> int:
            return 1

        def bd_get_disc_info(self, bd: int) -> Ptr:
            return Ptr()

        def bd_open_file_dec(self, bd: int, path: bytes) -> int:
            opened.append(path)
            return 1

        def bd_close(self, bd: int) -> None:
            pass

    monkeypatch.setattr(bluray, "_libbluray", Lib)
    monkeypatch.setattr(bluray.os, "sep", sep)
    disc = bluray.Disc("/dev/sr0")
    disc.open("BDMV/STREAM/SSIF/00098.ssif")
    assert opened == [expected]
    disc.close()


@pytest.mark.parametrize(("lines", "aacs", "bdplus"), [
    ([], "", ""),
    (["../src/libbluray/disc/aacs.c:115: Using libaacs for AACS\n"], "libaacs", ""),
    # libaacs could not decrypt: libbluray loaded the next library (aacs.c), the last wins
    (["aacs.c:115: Using libaacs for AACS\n", "aacs.c:115: Using libmmbd for AACS\n"],
     "libmmbd", ""),
    (["bdplus.c:119: Using libbdplus for BD+\n", "aacs.c:115: Using libaacs for AACS\n"],
     "libaacs", "libbdplus"),
    (["bdplus.c:119: Using libbdplus for BD+\n", "bdplus.c:119: Using libmmbd for BD+\n"],
     "", "libmmbd"),
    # the first candidate is LIBAACS_PATH / LIBBDPLUS_PATH as the user set it
    (["aacs.c:115: Using C:\\Program Files (x86)\\MakeMKV\\libmmbd64 for AACS\n"],
     "C:\\Program Files (x86)\\MakeMKV\\libmmbd64", ""),
])
def test_libraries_used(lines: list[str], aacs: str, bdplus: str) -> None:
    assert bluray._libraries_used(lines) == (aacs, bdplus)


@pytest.mark.parametrize(("line", "debug_mask", "shown"), [
    # only the messages libbluray would have shown anyway reach stderr
    ("aacs.c:115: Using libaacs for AACS\n", bluray.DBG_CRIT, False),
    ("bluray.c:1200: libbluray version 1.5.0\n", bluray.DBG_CRIT, False),
    ("dec.c:197: aacs_open() failed: -3!\n", bluray.DBG_CRIT, True),
    ("aacs.c:121: No usable AACS libraries found!\n", bluray.DBG_CRIT, True),
    ("aacs.c:255: Unable decrypt unit (AACS)!\n", bluray.DBG_CRIT, True),
    ("bluray.c:900: index.bdmv not found!\n", bluray.DBG_CRIT, True),
    ("disc.c:300: could not open the disc\n", bluray.DBG_CRIT, True),
    # what libbluray finds on every disc, said with DBG_BLURAY only
    ("aacs.c:83: AACS/Unit_Key_RO.inf not found. No AACS protection.\n", bluray.DBG_CRIT, False),
    ("bdplus.c:87: BDSVM/00000.svm not found. No BD+ protection.\n", bluray.DBG_CRIT, False),
    # the user asked for libbluray's own debug output (BD_DEBUG_MASK): all of it
    ("aacs.c:115: Using libaacs for AACS\n", bluray.DBG_CRIT | bluray.DBG_BLURAY, True),
])
def test_shown_while_capturing(line: str, debug_mask: int, shown: bool) -> None:
    assert bluray._shown_while_capturing(line, debug_mask) is shown


@pytest.mark.parametrize(("current", "env", "expected"), [
    (0x840, None, 0x840),
    (0xFFFFFFFF, None, bluray.DBG_CRIT),          # libbluray has not logged yet
    (0xFFFFFFFF, "0x40", 0x40),                   # it will read BD_DEBUG_MASK then
    (0xFFFFFFFF, "nonsense", bluray.DBG_CRIT),
])
def test_effective_debug_mask(monkeypatch: pytest.MonkeyPatch, current: int, env: str | None,
                              expected: int) -> None:
    if env is None:
        monkeypatch.delenv("BD_DEBUG_MASK", raising=False)
    else:
        monkeypatch.setenv("BD_DEBUG_MASK", env)
    assert bluray._effective_debug_mask(current) == expected


def test_disc_records_the_library_libbluray_used(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # libaacs had no key, libbluray fell back to libmmbd: info() says so, only the
    # critical message reaches stderr, and libbluray's log is left as it was
    class Info:
        bdplus_detected = False
        aacs_detected = True
        aacs_handled = True
        aacs_error_code = 0
        disc_name = None
        udf_volume_id = None
        content_exist_3D = False  # noqa: N815  libbluray's field name

    class Ptr:
        contents = Info()

    class Lib(LogLib):
        def bd_open(self, path: bytes, key: None) -> int:
            assert type(self).mask & bluray.DBG_BLURAY
            self.log(b"aacs.c:115: Using libaacs for AACS\n")
            self.log(b"dec.c:197: aacs_open() failed: -3!\n")
            self.log(b"aacs.c:115: Using libmmbd for AACS\n")
            return 1

        def bd_get_disc_info(self, bd: int) -> Ptr:
            return Ptr()

        def bd_close(self, bd: int) -> None:
            pass

    monkeypatch.setattr(bluray, "_libbluray", Lib)
    monkeypatch.delenv("BD_DEBUG_MASK", raising=False)
    Lib.mask, Lib.handler = 0xFFFFFFFF, None
    disc = bluray.Disc("/dev/sr0")
    info = disc.info()
    assert (info.aacs_library, info.bdplus_library) == ("libmmbd", "")
    assert capsys.readouterr().err == "dec.c:197: aacs_open() failed: -3!\n"
    assert Lib.handler is None and Lib.mask == bluray.DBG_CRIT
    disc.close()


def test_disc_restores_libbluray_log_when_the_open_fails(
        monkeypatch: pytest.MonkeyPatch) -> None:
    class Lib(LogLib):
        def bd_open(self, path: bytes, key: None) -> int:
            return 0

    monkeypatch.setattr(bluray, "_libbluray", Lib)
    monkeypatch.delenv("BD_DEBUG_MASK", raising=False)
    Lib.mask, Lib.handler = 0x840, None
    with pytest.raises(OSError, match="cannot open Blu-ray"):
        bluray.Disc("/dev/sr0")
    assert Lib.handler is None and Lib.mask == 0x840
