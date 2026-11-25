"""
osplatform.py — the handful of calls that differ by operating system: whether a
disc is in a drive, which drives to look at by default, how to load a shared
library, where temporary files, the program's log and the FUSE mount go by
default, and how FUSE differs (libfuse3 or WinFsp). Linux,
macOS and Windows are implemented; a further port adds branches here instead
of touching every module that assumes one platform today.

Named osplatform, not platform: the standard library already has a `platform`
module (imported by mfusepy), and a module of ours with that
name would shadow it for anything importing from src/.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import glob
import os
import string
import subprocess
import sys
from ctypes import wintypes
from enum import Enum, auto
from pathlib import Path
from typing import cast

LINUX = sys.platform == "linux"
MACOS = sys.platform == "darwin"
WINDOWS = sys.platform == "win32"

# fcntl is POSIX only: importing it unconditionally would crash this module (and
# everything that imports it, down to bd3d_fs.py) on Windows before drive_status()
# below even gets a chance to say "not Linux" (#10 gives Windows its own drive check)
if sys.platform != "win32":
    import fcntl

# ctypes.WinDLL only exists on Windows: guarded the same way as fcntl above, so this
# module still imports on Linux/macOS. kernel32 is the one indirection point the
# Windows-specific functions below call through, so tests can replace it with a
# fake regardless of the host OS (the same trick test_osplatform.py uses on fcntl).
if sys.platform == "win32":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.HANDLE)
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.DeviceIoControl.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                          wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    kernel32.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
else:
    kernel32 = None


class DriveStatus(Enum):
    EMPTY = auto()        # no disc, or the tray is open
    DISC = auto()         # a disc is in and ready
    UNKNOWN = auto()      # the drive is busy, or the call failed: not an eject


_CDROM_DRIVE_STATUS = 0x5326
_CDS_NO_DISC, _CDS_TRAY_OPEN, _CDS_DISC_OK = 1, 2, 4

# IOCTL_STORAGE_CHECK_VERIFY: CTL_CODE(FILE_DEVICE_MASS_STORAGE=0x2d, 0x200,
# METHOD_BUFFERED, FILE_READ_ACCESS) from winioctl.h, the Windows analog of the
# CDROM_DRIVE_STATUS ioctl above: asks the drive for media presence without
# reading it. GENERIC_READ/FILE_SHARE_*/OPEN_EXISTING are the usual CreateFile
# constants for opening a volume device by drive letter ("\\.\D:").
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE_VALUE = -1
_IOCTL_STORAGE_CHECK_VERIFY = 0x002D4800
_ERROR_NOT_READY = 21
_DRIVE_CDROM = 5


def _drive_status_windows(device: str) -> DriveStatus:
    k = cast(ctypes.CDLL, kernel32)
    handle = k.CreateFileW(f"\\\\.\\{device}", _GENERIC_READ,
                            _FILE_SHARE_READ | _FILE_SHARE_WRITE, None,
                            _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
    if handle in (0, _INVALID_HANDLE_VALUE):
        return DriveStatus.UNKNOWN
    try:
        returned = wintypes.DWORD()
        ok = k.DeviceIoControl(handle, _IOCTL_STORAGE_CHECK_VERIFY, None, 0,
                                None, 0, ctypes.byref(returned), None)
    finally:
        k.CloseHandle(handle)
    if ok:
        return DriveStatus.DISC
    return DriveStatus.EMPTY if k.GetLastError() == _ERROR_NOT_READY else DriveStatus.UNKNOWN


def _drive_status_linux(device: str) -> DriveStatus:
    # spelled as sys.platform, not WINDOWS: pyright narrows only on sys.platform, and
    # without it the Windows type check flags fcntl and os.O_NONBLOCK below
    if sys.platform == "win32":
        return DriveStatus.UNKNOWN
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return DriveStatus.UNKNOWN
    try:
        status = fcntl.ioctl(fd, _CDROM_DRIVE_STATUS, 0)
    except OSError:
        return DriveStatus.UNKNOWN
    finally:
        os.close(fd)
    if status == _CDS_DISC_OK:
        return DriveStatus.DISC
    if status in (_CDS_NO_DISC, _CDS_TRAY_OPEN):
        return DriveStatus.EMPTY
    return DriveStatus.UNKNOWN


def drive_status(device: str) -> DriveStatus:
    """Ask the drive whether a disc is in, without reading it. A busy drive can
    answer "not ready" or fail the call: that is not an eject (UNKNOWN)."""
    if WINDOWS:
        return _drive_status_windows(device)
    return _drive_status_linux(device) if LINUX else DriveStatus.UNKNOWN


def list_drives() -> list[str]:
    """Optical drives on this machine, for when none are named on the command
    line: by device path on Linux, by drive letter ("D:") on Windows."""
    if WINDOWS:
        k = cast(ctypes.CDLL, kernel32)
        return sorted(f"{letter}:" for letter in string.ascii_uppercase
                      if k.GetDriveTypeW(f"{letter}:\\") == _DRIVE_CDROM)
    return sorted(glob.glob("/dev/sr*")) if LINUX else []


_LIBRARY_VERSIONS: dict[str, tuple[int, ...]] = {
    "bluray": (4, 2),
    "dvdread": (8, 4),
    "mmbd": (0,),
}


def _names(name: str) -> list[str]:
    """Candidate file names for a shared library on this OS, newest first."""
    versions = _LIBRARY_VERSIONS.get(name, ())
    if MACOS:
        return [f"lib{name}.{v}.dylib" for v in versions] + [f"lib{name}.dylib"]
    if WINDOWS:
        return [f"{name}-{v}.dll" for v in versions] + [f"{name}.dll"]
    return [f"lib{name}.so.{v}" for v in versions]


def load_library(name: str, advice: str) -> ctypes.CDLL:
    """Load a shared library by its common name ("bluray", "dvdread"): the
    system's own lookup first, then the version suffixes Debian/Ubuntu ship."""
    for candidate in filter(None, [ctypes.util.find_library(name), *_names(name)]):
        try:
            return ctypes.CDLL(candidate)
        except OSError:
            pass
    raise OSError(f"lib{name} not found ({advice})")


def has_library(name: str) -> bool:
    """Whether a shared library is installed, without loading it: libmmbd
    (MakeMKV) is picked as a decryption backend only when present, since it is
    optional and proprietary."""
    if ctypes.util.find_library(name):
        return True
    return LINUX and any(Path("/usr/lib", n).exists() for n in _names(name))


def default_temp_dir() -> str:
    """Where this run's temporary files (the pipeline log, IFO
    copies) go by default."""
    if WINDOWS:
        return os.environ.get("TEMP") or str(Path.home() / "AppData" / "Local" / "Temp")
    if MACOS:
        return os.environ.get("TMPDIR", "/tmp")  # noqa: S108  the documented default
    return "/tmp"  # noqa: S108  the documented default, not a secret


def default_program_log() -> Path:
    """Where the program's own log is saved by default (--log to change it)."""
    if WINDOWS:
        local_appdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local_appdata) / "spinema" / "spinema.log"
    if MACOS:
        return Path.home() / "Library" / "Logs" / "spinema" / "spinema.log"
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "spinema" / "spinema.log"


def default_mount_point() -> str:
    """Where the files are mounted by default (--mount to change it): Disks
    inside the folder the installer shares, never the shared folder itself.
    On Windows a mount folder cannot be shared, its parent can (#102)."""
    if WINDOWS:
        program_files = os.environ.get("PROGRAMFILES") or "C:\\Program Files"
        return str(Path(program_files) / "Spinema" / "share" / "Disks")
    return "/srv/spinema/Disks"


def fuse_creates_mount_point() -> bool:
    """Whether FUSE makes the mount folder itself: WinFsp creates it (a junction)
    and deletes it on unmount, and fails if it exists; libfuse mounts on a
    folder that exists already."""
    return WINDOWS


def fuse_library() -> str | None:
    """The FUSE library for mfusepy (FUSE_LIBRARY_PATH): libfuse3 by name, as
    mfusepy picks libfuse 2 when both are installed. None on Windows, where
    mfusepy finds WinFsp's DLL from the registry."""
    if WINDOWS:
        return None
    path = ctypes.util.find_library("fuse3")
    if path is None:
        raise ImportError("libfuse3 not found")
    return path


def fuse_owner() -> tuple[int, int]:
    """Who owns the files (uid, gid), as FUSE mount options: the user who mounts. WinFsp
    reads -1 as that user; any other number maps to some SID of its own."""
    # sys.platform too: pyright knows os.getuid is POSIX only from that alone
    if WINDOWS or sys.platform == "win32":
        return -1, -1
    return os.getuid(), os.getgid()


def force_unmount(mount_point: str) -> None:
    """Unmount a FUSE mount whose loop did not stop. Lazily on Linux (Samba may
    hold files open). Nothing on Windows: WinFsp has no such command, and its
    volume goes with the process."""
    if not WINDOWS:
        subprocess.run(["fusermount3", "-u", "-z", mount_point], check=False)


def fuse_install_hint(share: str) -> str:
    """How to get FUSE, for the error when it is missing."""
    if WINDOWS:
        return "install WinFsp (https://winfsp.dev), or use --share dlna"
    return f"run scripts/install.sh --share {share}, install fuse3, or use --share dlna"


def multicast_bind_address(group: str) -> str:
    """Where a socket joining a multicast group (SSDP) is bound. Linux: to the
    group itself, so it gets only that multicast and never datagrams sent to
    this host. Windows cannot bind a multicast address: to every interface."""
    return "0.0.0.0" if WINDOWS else group  # noqa: S104  joins the group, see above
