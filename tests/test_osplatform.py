"""osplatform.py: the Linux- and Windows-specific calls (drive status, library
loading, the default temp dir and log path) behind a platform-independent
interface. The Windows branches are exercised here regardless of the host OS,
by flipping the WINDOWS flag and replacing kernel32 with a fake, the same
trick used below for fcntl on Linux."""
import ctypes
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import osplatform
from osplatform import DriveStatus, default_program_log, default_temp_dir, drive_status


def test_drive_status_no_such_device() -> None:
    assert drive_status("/no/such/device") == DriveStatus.UNKNOWN


@pytest.mark.parametrize(("ioctl_result", "expected"), [
    (4, DriveStatus.DISC),       # CDS_DISC_OK
    (1, DriveStatus.EMPTY),      # CDS_NO_DISC
    (2, DriveStatus.EMPTY),      # CDS_TRAY_OPEN
    (3, DriveStatus.UNKNOWN),    # CDS_DRIVE_NOT_READY: busy, not an eject
    (0, DriveStatus.UNKNOWN),
])
@pytest.mark.skipif(sys.platform == "win32",
                    reason="fcntl is POSIX only; Windows gets its own drive check (#10)")
def test_drive_status_maps_the_ioctl_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                            ioctl_result: int, expected: DriveStatus) -> None:
    def ioctl(fd: int, request: int, arg: int) -> int:
        return ioctl_result

    device = tmp_path / "sr0"
    device.touch()
    # the ioctl mapping is Linux's own, tested regardless of where this runs
    monkeypatch.setattr(osplatform, "LINUX", True)
    monkeypatch.setattr(osplatform, "fcntl", SimpleNamespace(ioctl=ioctl))
    assert drive_status(str(device)) == expected


@pytest.mark.skipif(sys.platform == "win32",
                    reason="fcntl is POSIX only; Windows gets its own drive check (#10)")
def test_drive_status_ioctl_failure_is_unknown(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    def fail(fd: int, request: int, arg: int) -> int:
        raise OSError("drive busy")

    device = tmp_path / "sr0"
    device.touch()
    monkeypatch.setattr(osplatform, "LINUX", True)
    monkeypatch.setattr(osplatform, "fcntl", SimpleNamespace(ioctl=fail))
    assert drive_status(str(device)) == DriveStatus.UNKNOWN


def test_drive_status_not_linux_is_unknown(monkeypatch: pytest.MonkeyPatch,
                                           tmp_path: Path) -> None:
    device = tmp_path / "sr0"
    device.touch()
    monkeypatch.setattr(osplatform, "LINUX", False)
    assert drive_status(str(device)) == DriveStatus.UNKNOWN


def _fake_kernel32(*, handle: int = 1, ok: bool = True, last_error: int = 0) -> SimpleNamespace:
    calls = SimpleNamespace(opened=[], closed=[])

    def create_file_w(path: str, *_a: object) -> int:
        calls.opened.append(path)
        return handle

    def device_io_control(*_a: object) -> bool:
        return ok

    def close_handle(h: int) -> None:
        calls.closed.append(h)

    return SimpleNamespace(CreateFileW=create_file_w, DeviceIoControl=device_io_control,
                           CloseHandle=close_handle, GetLastError=lambda: last_error,
                           calls=calls)


def test_drive_status_windows_disc_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_kernel32(ok=True)
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setattr(osplatform, "kernel32", fake)
    assert drive_status("D:") == DriveStatus.DISC
    assert fake.calls.opened == ["\\\\.\\D:"]
    assert fake.calls.closed == [1]                       # the handle is always closed


def test_drive_status_windows_no_disc(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_kernel32(ok=False, last_error=21)         # ERROR_NOT_READY
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setattr(osplatform, "kernel32", fake)
    assert drive_status("D:") == DriveStatus.EMPTY


def test_drive_status_windows_other_error_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_kernel32(ok=False, last_error=5)          # ERROR_ACCESS_DENIED: busy, not an eject
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setattr(osplatform, "kernel32", fake)
    assert drive_status("D:") == DriveStatus.UNKNOWN


def test_drive_status_windows_cannot_open_drive_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_kernel32(handle=-1)                       # INVALID_HANDLE_VALUE
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setattr(osplatform, "kernel32", fake)
    assert drive_status("D:") == DriveStatus.UNKNOWN
    assert fake.calls.closed == []                         # nothing to close


def test_list_drives_windows_filters_by_drive_type(monkeypatch: pytest.MonkeyPatch) -> None:
    def get_drive_type_w(path: str) -> int:
        return 5 if path in ("D:\\", "E:\\") else 3        # DRIVE_CDROM vs DRIVE_FIXED

    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setattr(osplatform, "kernel32", SimpleNamespace(GetDriveTypeW=get_drive_type_w))
    assert osplatform.list_drives() == ["D:", "E:"]


def test_list_drives(monkeypatch: pytest.MonkeyPatch) -> None:
    def glob(pattern: str) -> list[str]:
        return ["/dev/sr1", "/dev/sr0"]

    monkeypatch.setattr(osplatform.glob, "glob", glob)
    monkeypatch.setattr(osplatform, "LINUX", True)
    assert osplatform.list_drives() == ["/dev/sr0", "/dev/sr1"]
    monkeypatch.setattr(osplatform, "LINUX", False)
    assert osplatform.list_drives() == []


# what the mocked ctypes.CDLL hands back: no real library is opened, so the
# test means the same on every OS (ctypes.CDLL(None) is POSIX only)
LIB = ctypes.CDLL.__new__(ctypes.CDLL)


def test_load_library_tries_candidates_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    tried: list[str] = []

    def nothing(name: str) -> str | None:
        return None

    def cdll(name: str) -> ctypes.CDLL:
        tried.append(name)
        if name != "libbluray.so.2":
            raise OSError("not found")
        return LIB

    # the .so candidate names are Linux's own, tested regardless of where this runs
    monkeypatch.setattr(osplatform, "MACOS", False)
    monkeypatch.setattr(osplatform.sys, "platform", "linux")
    monkeypatch.setattr(osplatform.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(osplatform.ctypes, "CDLL", cdll)
    assert osplatform.load_library("bluray", "apt install libbluray2") is LIB
    assert tried == ["libbluray.so.4", "libbluray.so.2"]


def test_names_windows_dll_candidates_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osplatform, "MACOS", False)
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    # newest version first, then the unversioned fallback, no "lib" prefix
    assert osplatform._names("bluray") == ["bluray-4.dll", "bluray-2.dll", "bluray.dll"]
    assert osplatform._names("mmbd") == ["mmbd-0.dll", "mmbd.dll"]


def test_load_library_tries_windows_candidates_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    tried: list[str] = []

    def nothing(name: str) -> str | None:
        return None

    def cdll(name: str) -> ctypes.CDLL:
        tried.append(name)
        if name != "bluray-2.dll":
            raise OSError("not found")
        return LIB

    monkeypatch.setattr(osplatform, "MACOS", False)
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setattr(osplatform.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(osplatform.ctypes, "CDLL", cdll)
    assert osplatform.load_library("bluray", "install libbluray") is LIB
    assert tried == ["bluray-4.dll", "bluray-2.dll"]


def test_load_library_prefers_the_system_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    tried: list[str] = []

    def found(name: str) -> str | None:
        return "libbluray.so.4.1.0"

    def cdll(name: str) -> ctypes.CDLL:
        tried.append(name)
        return LIB

    monkeypatch.setattr(osplatform.ctypes.util, "find_library", found)
    monkeypatch.setattr(osplatform.ctypes, "CDLL", cdll)
    assert osplatform.load_library("bluray", "apt install libbluray2") is LIB
    assert tried == ["libbluray.so.4.1.0"]


def test_load_library_raises_with_the_advice_when_nothing_works(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def nothing(name: str) -> str | None:
        return None

    def cdll(name: str) -> ctypes.CDLL:
        raise OSError("not found")

    monkeypatch.setattr(osplatform.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(osplatform.ctypes, "CDLL", cdll)
    with pytest.raises(OSError, match=r"libbluray not found .*apt install libbluray2"):
        osplatform.load_library("bluray", "apt install libbluray2")


def test_has_library_found_by_the_system_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def found(name: str) -> str | None:
        return "libmmbd.so.0"

    monkeypatch.setattr(osplatform.ctypes.util, "find_library", found)
    assert osplatform.has_library("mmbd") is True


def test_has_library_falls_back_to_the_known_path(monkeypatch: pytest.MonkeyPatch) -> None:
    known = "/usr/lib/libmmbd.so.0"

    def nothing(name: str) -> str | None:
        return None

    def exists(self: Path) -> bool:
        return self == Path(known)

    # the /usr/lib fallback and its .so name are Linux's own, tested regardless
    # of where this runs
    monkeypatch.setattr(osplatform, "LINUX", True)
    monkeypatch.setattr(osplatform, "MACOS", False)
    monkeypatch.setattr(osplatform.sys, "platform", "linux")
    monkeypatch.setattr(osplatform.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(osplatform.Path, "exists", exists)
    assert osplatform.has_library("mmbd") is True


def test_has_library_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def nothing(name: str) -> str | None:
        return None

    def missing(self: Path) -> bool:
        return False

    monkeypatch.setattr(osplatform.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(osplatform.Path, "exists", missing)
    assert osplatform.has_library("mmbd") is False


def test_default_temp_dir_is_slash_tmp_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    # macOS sets TMPDIR itself; drop it so the plain default is what is checked here
    monkeypatch.delenv("TMPDIR", raising=False)
    assert default_temp_dir() == "/tmp"                       # noqa: S108


def test_default_temp_dir_on_windows_uses_temp_env(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path) -> None:
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setenv("TEMP", str(tmp_path))
    assert default_temp_dir() == str(tmp_path)


def test_default_temp_dir_on_windows_falls_back_to_appdata_local_temp(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.delenv("TEMP", raising=False)
    assert default_temp_dir() == str(Path.home() / "AppData" / "Local" / "Temp")


def test_default_program_log_under_xdg_state_home(monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    # the XDG path is Linux's own (macOS always takes ~/Library/Logs), tested
    # regardless of where this runs
    monkeypatch.setattr(osplatform, "MACOS", False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_program_log() == tmp_path / "spinema" / "spinema.log"


def test_default_program_log_falls_back_to_dot_local_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osplatform, "MACOS", False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert default_program_log() == Path.home() / ".local" / "state" / "spinema" \
        / "spinema.log"


def test_default_program_log_on_windows_under_localappdata(monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path) -> None:
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_program_log() == tmp_path / "spinema" / "spinema.log"


def test_default_program_log_on_windows_falls_back_to_appdata_local(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert default_program_log() == Path.home() / "AppData" / "Local" / "spinema" \
        / "spinema.log"
