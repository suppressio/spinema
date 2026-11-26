"""osplatform.py: the Linux- and Windows-specific calls (drive status, library
loading, the default temp dir and log path) behind a platform-independent
interface. The Windows branches are exercised here regardless of the host OS,
by flipping the WINDOWS flag and replacing kernel32 with a fake, the same
trick used below for fcntl on Linux."""
import ctypes
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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
    monkeypatch.setattr(osplatform, "WINDOWS", False)
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
    monkeypatch.setattr(osplatform, "WINDOWS", False)
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
    monkeypatch.setattr(osplatform, "WINDOWS", False)
    monkeypatch.setattr(osplatform.sys, "platform", "linux")
    monkeypatch.setattr(osplatform.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(osplatform.ctypes, "CDLL", cdll)
    assert osplatform.load_library("bluray", "apt install libbluray2") is LIB
    assert tried == ["libbluray.so.4", "libbluray.so.2"]


def test_names_windows_dll_candidates_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osplatform, "MACOS", False)
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    # newest version first, then the unversioned fallback; MSYS2 builds have
    # the "lib" prefix (libbluray-4.dll), MSVC-style builds do not (#207)
    assert osplatform._names("bluray") == [
        "libbluray-4.dll", "bluray-4.dll", "libbluray-2.dll", "bluray-2.dll",
        "libbluray.dll", "bluray.dll"]
    assert osplatform._names("mmbd") == ["libmmbd-0.dll", "mmbd-0.dll", "libmmbd.dll", "mmbd.dll"]


def test_load_library_tries_windows_candidates_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    tried: list[str] = []

    def nothing(name: str) -> str | None:
        return None

    def cdll(name: str) -> ctypes.CDLL:
        tried.append(name)
        if name != "bluray-4.dll":
            raise OSError("not found")
        return LIB

    monkeypatch.setattr(osplatform, "MACOS", False)
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    def no_folder(name: str) -> None:
        return None

    monkeypatch.setattr(osplatform, "_prepare_library_dir", no_folder)
    monkeypatch.setattr(osplatform.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(osplatform.ctypes, "CDLL", cdll)
    assert osplatform.load_library("bluray", "install libbluray") is LIB
    assert tried == ["libbluray-4.dll", "bluray-4.dll"]


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


def _no_library(name: str) -> str | None:
    return None


def _windows_library_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                         files: list[str]) -> tuple[Path, list[str], list[str]]:
    """A fake <Program Files>\\Spinema\\dll with these files; records what is
    added to the DLL search path and what is loaded."""
    dll = tmp_path / "Spinema" / "dll"
    dll.mkdir(parents=True)
    for f in files:
        (dll / f).touch()
    added: list[str] = []
    loaded: list[str] = []

    def add_dll_directory(path: str) -> object:
        added.append(path)
        return object()

    def cdll(name: str) -> ctypes.CDLL:
        loaded.append(name)
        return LIB

    monkeypatch.setattr(osplatform, "MACOS", False)
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setattr(osplatform, "_dll_directories", {})
    monkeypatch.setenv("PROGRAMW6432", str(tmp_path))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "no-x86"))
    monkeypatch.setattr(osplatform.os, "add_dll_directory", add_dll_directory, raising=False)
    def nothing(name: str) -> str | None:
        return None

    monkeypatch.setattr(osplatform.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(osplatform.ctypes, "CDLL", cdll)
    return dll, added, loaded


def test_library_dir_on_windows_is_in_the_install_dir(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setenv("PROGRAMW6432", str(tmp_path))
    assert osplatform.library_dir() == tmp_path / "Spinema" / "dll"
    monkeypatch.setattr(osplatform, "WINDOWS", False)
    assert osplatform.library_dir() is None


def test_load_library_on_windows_preloads_what_libbluray_loads_by_name(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Python ignores PATH for DLLs (only add_dll_directory), and libbluray loads
    # libaacs/libbdplus/libmmbd64 itself only from python.exe's folder or System32:
    # loaded first from our folder, libbluray gets them by module name (#207)
    dll, added, loaded = _windows_library_dir(
        monkeypatch, tmp_path, ["libaacs.dll", "libbdplus.dll", "libbluray-4.dll"])
    assert osplatform.load_library("bluray", "install libbluray") is LIB
    assert added == [str(dll)]
    assert loaded == [str(dll / "libaacs.dll"), str(dll / "libbdplus.dll"), "libbluray-4.dll"]


def test_load_library_on_windows_preloads_libdvdcss_for_libdvdread(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dll, _, loaded = _windows_library_dir(
        monkeypatch, tmp_path, ["libdvdcss-2.dll", "libdvdread-8.dll"])
    osplatform.load_library("dvdread", "install libdvdread")
    assert loaded == [str(dll / "libdvdcss-2.dll"), "libdvdread-8.dll"]


def test_load_library_on_windows_adds_the_folder_once(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # bluray.py and dvd.py both load: one search-path entry, kept alive for the process
    dll, added, _ = _windows_library_dir(monkeypatch, tmp_path, ["libbluray-4.dll"])
    osplatform.load_library("bluray", "install libbluray")
    osplatform.load_library("dvdread", "install libdvdread")
    assert added == [str(dll)]


def test_load_library_on_windows_without_the_folder_uses_the_system_lookup(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # a developer's own DLLs (e.g. next to python.exe) still work without an install
    _, added, loaded = _windows_library_dir(monkeypatch, tmp_path, [])
    monkeypatch.setenv("PROGRAMW6432", str(tmp_path / "elsewhere"))
    osplatform.load_library("bluray", "install libbluray")
    assert added == []
    assert loaded == ["libbluray-4.dll"]


def test_load_library_on_windows_a_broken_companion_says_which(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # libaacs present but its libgcrypt missing: say so, not "the disc is encrypted" later
    _windows_library_dir(monkeypatch, tmp_path, ["libaacs.dll"])

    def cdll(name: str) -> ctypes.CDLL:
        raise OSError("[WinError 126] The specified module could not be found")

    monkeypatch.setattr(osplatform.ctypes, "CDLL", cdll)
    with pytest.raises(OSError, match=r"libaacs\.dll cannot be loaded: \[WinError 126\]"):
        osplatform.load_library("bluray", "install libbluray")


def test_load_library_on_windows_a_broken_optional_companion_only_warns(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        caplog: pytest.LogCaptureFixture) -> None:
    # a broken libbdplus or MakeMKV must not take AACS-only discs down with it
    dll, _, loaded = _windows_library_dir(
        monkeypatch, tmp_path, ["libaacs.dll", "libbdplus.dll", "libbluray-4.dll"])

    def cdll(name: str) -> ctypes.CDLL:
        if name.endswith("libbdplus.dll"):
            raise OSError("[WinError 193] %1 is not a valid Win32 application")
        loaded.append(name)
        return LIB

    monkeypatch.setattr(osplatform.ctypes, "CDLL", cdll)
    assert osplatform.load_library("bluray", "install libbluray") is LIB
    assert loaded == [str(dll / "libaacs.dll"), "libbluray-4.dll"]
    assert "libbdplus.dll cannot be loaded, going on without it" in caplog.text


def _makemkv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    makemkv = tmp_path / "x86" / "MakeMKV"
    makemkv.mkdir(parents=True)
    (makemkv / "libmmbd64.dll").touch()
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "x86"))
    monkeypatch.setattr(osplatform, "_makemkv_loaded", set[Path]())
    return makemkv / "libmmbd64.dll"


def test_load_library_on_windows_leaves_makemkv_alone(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # not loaded with libbluray: libbluray would fall back to it while libaacs
    # is tried, and a never-activated MakeMKV hangs there (#207)
    dll, _, loaded = _windows_library_dir(
        monkeypatch, tmp_path, ["libaacs.dll", "libbluray-4.dll"])
    _makemkv(monkeypatch, tmp_path)
    monkeypatch.delenv("LIBAACS_PATH", raising=False)
    osplatform.load_library("bluray", "install libbluray")
    assert loaded == [str(dll / "libaacs.dll"), "libbluray-4.dll"]


def test_load_library_on_windows_loads_makemkv_in_its_subprocess(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # a pipeline subprocess for a disc MakeMKV opened gets LIBAACS_PATH=libmmbd
    dll, _, loaded = _windows_library_dir(
        monkeypatch, tmp_path, ["libaacs.dll", "libbluray-4.dll"])
    mmbd = _makemkv(monkeypatch, tmp_path)
    monkeypatch.setenv("LIBAACS_PATH", "libmmbd")
    osplatform.load_library("bluray", "install libbluray")
    assert loaded == [str(dll / "libaacs.dll"), str(mmbd), "libbluray-4.dll"]


def test_load_makemkv_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, _, loaded = _windows_library_dir(monkeypatch, tmp_path, [])
    mmbd = _makemkv(monkeypatch, tmp_path)
    osplatform.load_makemkv()
    osplatform.load_makemkv()
    assert loaded == [str(mmbd)]


def test_load_makemkv_not_installed_does_nothing(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, _, loaded = _windows_library_dir(monkeypatch, tmp_path, [])
    osplatform.load_makemkv()
    monkeypatch.setattr(osplatform, "WINDOWS", False)
    osplatform.load_makemkv()
    assert loaded == []


def test_has_library_mmbd_on_windows_looks_in_makemkv_folder(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setattr(osplatform, "LINUX", False)
    monkeypatch.setenv("PROGRAMW6432", str(tmp_path / "pf"))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "x86"))
    monkeypatch.setattr(osplatform.ctypes.util, "find_library", _no_library)
    assert osplatform.has_library("mmbd") is False
    (tmp_path / "x86" / "MakeMKV").mkdir(parents=True)
    (tmp_path / "x86" / "MakeMKV" / "libmmbd64.dll").touch()
    assert osplatform.has_library("mmbd") is True


def test_has_library_found_by_the_system_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def found(name: str) -> str | None:
        return "libmmbd.so.0"

    # Linux's lookup, tested wherever this runs (Windows looks in MakeMKV's folder)
    monkeypatch.setattr(osplatform, "WINDOWS", False)
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
    monkeypatch.setattr(osplatform, "WINDOWS", False)
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
    monkeypatch.setattr(osplatform, "WINDOWS", False)
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
    monkeypatch.setattr(osplatform, "WINDOWS", False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_program_log() == tmp_path / "spinema" / "spinema.log"


def test_default_program_log_falls_back_to_dot_local_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osplatform, "MACOS", False)
    monkeypatch.setattr(osplatform, "WINDOWS", False)
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


def test_default_mount_point_on_linux_is_inside_the_installed_share(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # the .deb and install.sh create /srv/spinema: spinema mounts Disks inside it (#196)
    monkeypatch.setattr(osplatform, "WINDOWS", False)
    assert osplatform.default_mount_point() == "/srv/spinema/Disks"


def test_default_mount_point_on_windows_is_below_the_shared_folder(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # the installer shares <install dir>\share as "Spinema" and lets the user
    # write there: the mount folder goes below it, never shared itself (#102)
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.delenv("PROGRAMW6432", raising=False)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    assert osplatform.default_mount_point() == str(tmp_path / "Spinema" / "share" / "Disks")


def test_default_mount_point_on_windows_without_programfiles(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.delenv("PROGRAMW6432", raising=False)
    monkeypatch.delenv("PROGRAMFILES", raising=False)
    assert osplatform.default_mount_point() == str(
        Path("C:\\Program Files") / "Spinema" / "share" / "Disks")


def test_default_mount_point_on_windows_ignores_a_32_bit_python(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # a 32-bit Python on 64-bit Windows sees "Program Files (x86)" in PROGRAMFILES;
    # PROGRAMW6432 is the 64-bit folder the installer shares, for every bitness (#209)
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setenv("PROGRAMW6432", str(tmp_path / "Program Files"))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "Program Files (x86)"))
    assert osplatform.default_mount_point() == str(
        tmp_path / "Program Files" / "Spinema" / "share" / "Disks")


def test_fuse_creates_the_mount_point_only_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    # WinFsp makes the mount folder itself (a junction) and deletes it on
    # unmount; libfuse mounts on a folder that exists already
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    assert osplatform.fuse_creates_mount_point()
    monkeypatch.setattr(osplatform, "WINDOWS", False)
    assert not osplatform.fuse_creates_mount_point()


def test_fuse_library_on_linux_is_libfuse3(monkeypatch: pytest.MonkeyPatch) -> None:
    # mfusepy prefers libfuse 2 when both are installed: libfuse3 is named
    def find(name: str) -> str:
        return "libfuse3.so.4" if name == "fuse3" else "libfuse.so.2"

    monkeypatch.setattr(osplatform, "WINDOWS", False)
    monkeypatch.setattr(osplatform.ctypes.util, "find_library", find)
    assert osplatform.fuse_library() == "libfuse3.so.4"


def test_fuse_library_missing_on_linux_is_an_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osplatform, "WINDOWS", False)
    monkeypatch.setattr(osplatform.ctypes.util, "find_library", Mock(return_value=None))
    with pytest.raises(ImportError, match="libfuse3 not found"):
        osplatform.fuse_library()


def test_fuse_library_on_windows_is_left_to_mfusepy(monkeypatch: pytest.MonkeyPatch) -> None:
    # mfusepy finds WinFsp's DLL itself, from the registry
    find = Mock()
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setattr(osplatform.ctypes.util, "find_library", find)
    assert osplatform.fuse_library() is None
    find.assert_not_called()


def test_fuse_owner_on_windows_is_the_mounting_user(monkeypatch: pytest.MonkeyPatch) -> None:
    # WinFsp: -1 is the user who mounts (any other uid maps to some SID, 0 to
    # S-1-5-0 and -1 in getattr to "nobody", checked on Windows 11)
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    assert osplatform.fuse_owner() == (-1, -1)


def test_fuse_owner_on_linux_is_this_user(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":    # in the body: pyright narrows on it, not on skipif
        pytest.skip("os.getuid is POSIX only")
    monkeypatch.setattr(osplatform, "WINDOWS", False)
    assert osplatform.fuse_owner() == (os.getuid(), os.getgid())


def test_force_unmount_on_linux_is_a_lazy_fusermount3(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock()
    monkeypatch.setattr(osplatform, "WINDOWS", False)
    monkeypatch.setattr(osplatform.subprocess, "run", run)
    osplatform.force_unmount("/srv/spinema/Disks")
    run.assert_called_once_with(["fusermount3", "-u", "-z", "/srv/spinema/Disks"], check=False)


def test_force_unmount_on_windows_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # WinFsp has no fusermount3: its volume goes when the process ends
    run = Mock()
    monkeypatch.setattr(osplatform, "WINDOWS", True)
    monkeypatch.setattr(osplatform.subprocess, "run", run)
    osplatform.force_unmount("C:\\Program Files\\Spinema\\share\\Disks")
    run.assert_not_called()


@pytest.mark.parametrize(("windows", "expected", "unexpected"), [
    (True, "install WinFsp", "fuse3"),
    (False, "install fuse3", "WinFsp"),
])
def test_fuse_install_hint(monkeypatch: pytest.MonkeyPatch, windows: bool,
                           expected: str, unexpected: str) -> None:
    monkeypatch.setattr(osplatform, "WINDOWS", windows)
    hint = osplatform.fuse_install_hint("samba")
    assert expected in hint and unexpected not in hint
    assert hint.endswith("or use --share dlna")
    assert ("scripts/install.sh --share samba" in hint) is not windows


@pytest.mark.parametrize(("windows", "expected"), [(False, "239.255.255.250"),
                                                   (True, "0.0.0.0")])  # noqa: S104
def test_multicast_bind_address(monkeypatch: pytest.MonkeyPatch, windows: bool,
                                expected: str) -> None:
    # Linux: bound to the group, the socket gets only multicast. Windows cannot
    # bind a multicast address (WinError 10049, checked on Windows 11)
    monkeypatch.setattr(osplatform, "WINDOWS", windows)
    assert osplatform.multicast_bind_address("239.255.255.250") == expected
