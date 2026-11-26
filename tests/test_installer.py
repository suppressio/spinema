"""Focused checks for the native and Docker dependency decisions."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INSTALL = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
UNINSTALL = (ROOT / "scripts/uninstall.sh").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _case_body(script: str, start: str, end: str) -> str:
    return script.split(start, 1)[1].split(end, 1)[0]


def test_native_installer_keeps_runtime_and_removes_python_fuse_packages() -> None:
    assert "packages=(ffmpeg python3 " in INSTALL
    assert "python3-trio" not in INSTALL
    assert "python3-pyfuse3" not in INSTALL
    assert 'case "$SHARE" in samba|both) packages+=(fuse3 samba) ;; esac' in INSTALL


def test_native_installer_dependency_modes() -> None:
    packages = INSTALL.split("packages=(", 1)[1].split(")", 1)[0]
    assert "fuse3" not in packages and "samba" not in packages
    optional = _case_body(INSTALL, 'case "$SHARE" in samba|both)', "esac")
    assert "fuse3 samba" in optional
    assert "python3-pyfuse3" not in optional
    assert "python3-trio" not in optional


def test_uninstaller_does_not_claim_removed_python_packages() -> None:
    assert "python3-pyfuse3" not in UNINSTALL
    assert "python3-trio" not in UNINSTALL
    assert "fuse3 samba" in UNINSTALL


def test_docker_uses_vendored_adapter_and_system_fuse() -> None:
    assert "python3-pyfuse3" not in DOCKERFILE
    assert "python3-trio" not in DOCKERFILE
    assert "ffmpeg python3 fuse3 samba tini" in DOCKERFILE
    assert "COPY src/ /app/" in DOCKERFILE
    assert "COPY --from=version /version /app/version" in DOCKERFILE


def test_docker_heavy_option_is_off_by_default_and_reaches_the_program() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert 'HEAVY: "${HEAVY:-off}"' in compose
    assert '[ "$HEAVY" = "on" ] && set -- "$@" --heavy' in entrypoint
    assert "HEAVY=off" in env.splitlines()      # splitlines: a Windows checkout may use CRLF


def test_dlna_compose_overlay_passes_the_drive_named_by_drive() -> None:
    dlna = (ROOT / "docker-compose.dlna.yml").read_text(encoding="utf-8")
    assert '"${DRIVE:-/dev/null}"' in dlna      # the drive reaches a DLNA-only container
    assert "devices: !reset" not in dlna        # which would drop it


def test_installer_drops_the_old_mount_point_for_every_share_kind() -> None:
    # /srv/bd3d (before #196) must go even with --share dlna, which skips the rest of step 4
    step4 = _case_body(INSTALL, 'step "4/5 FUSE mount point"\n', 'if [ "$SHARE" = dlna ]')
    assert step4.strip() == "drop_old_mount"
    drop = _case_body(INSTALL, "drop_old_mount() {", "\n}")
    assert "mountpoint" not in drop     # says no to a stale FUSE mount, which rmdir cannot remove
    assert "-z /srv/bd3d" in drop and "umount -l /srv/bd3d" in drop


def test_updater_migrates_only_while_the_share_is_on_the_old_mount_point(tmp_path: Path) -> None:
    # update.sh re-runs install.sh while our smb.conf block still shares /srv/bd3d;
    # a folder left behind, or someone else's share on it, must not re-run it on
    # every update (#196)
    update = (ROOT / "scripts/update.sh").read_text(encoding="utf-8")
    assert "[ -e /srv/bd3d ]" not in update
    assert '[ "$share" != dlna ] && old_share_layout' in update
    # no CR: a Windows checkout may use CRLF, which bash would keep in the patterns
    function = _case_body(update.replace("\r", ""), "old_share_layout() {", "\n}")
    # by full path: a bare "bash" can be WSL's on Windows (see test_deb_version)
    bash = shutil.which("bash") or "bash"
    ours = "# >>> spinema >>>\n[{}]\n   path = {}\n   read only = yes\n# <<< spinema <<<\n"
    cases = ((ours.format("Disks", "/srv/bd3d"), True),
             (ours.format("Spinema", "/srv/spinema"), False),
             ("[mine]\n   path = /srv/bd3d\n" + ours.format("Spinema", "/srv/spinema"), False))
    for conf, migrate in cases:
        smb = tmp_path / "smb.conf"
        smb.write_bytes(conf.encode())     # LF line ends on every platform
        script = f'old_share_layout() {{{function}\n}}\nold_share_layout "$1"'
        found = subprocess.run([bash, "-c", script, "test", smb.as_posix()],
                               check=False).returncode == 0
        assert found is migrate, conf


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="the native install is Linux only, and update.sh refuses root")
def test_updater_runs_the_new_update_sh_after_a_pull_that_changed_it(tmp_path: Path) -> None:
    # bash keeps reading the update.sh it started with, even after `git pull`
    # replaced the file: a migration added to update.sh (like #196's) would only
    # run on the next update. The old script must hand over to the new one.
    def git(cwd: Path, *args: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                       cwd=cwd, check=True, capture_output=True)

    origin, work, user = tmp_path / "origin.git", tmp_path / "work", tmp_path / "user"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "scripts").mkdir()
    (work / "scripts" / "update.sh").write_bytes((ROOT / "scripts/update.sh").read_bytes())
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "current")
    git(work, "push", "-q", "origin", "HEAD")
    git(tmp_path, "clone", "-q", str(origin), str(user))
    (work / "scripts" / "update.sh").write_bytes(
        b'#!/usr/bin/env bash\necho "new update.sh ran (rerun=$SPINEMA_UPDATE_RERUN)"\n')
    git(work, "commit", "-q", "-am", "update.sh changed")
    git(work, "push", "-q", "origin", "HEAD")

    result = subprocess.run(["bash", str(user / "scripts" / "update.sh")],
                            capture_output=True, text=True, check=False, timeout=60)
    assert "new update.sh ran (rerun=1)" in result.stdout, result.stdout + result.stderr


def test_every_build_uses_the_edge264_commit_pinned_in_install_sh() -> None:
    # one decoder version for the native install, the .deb and Windows (#105)
    pinned = INSTALL.split("EDGE264_COMMIT=", 1)[1].split("\n", 1)[0].strip()
    deb = (ROOT / "scripts/build-deb.sh").read_text(encoding="utf-8")
    assert f"edge264_commit={pinned}" in deb
    windows = (ROOT / "scripts/build-edge264-windows.sh").read_text(encoding="utf-8")
    assert "EDGE264_COMMIT" in windows and "scripts/install.sh" in windows
    assert pinned not in windows        # read from install.sh, not a third copy


def test_windows_edge264_build_is_portable_and_self_contained() -> None:
    windows = (ROOT / "scripts/build-edge264-windows.sh").read_text(encoding="utf-8")
    assert "OS=windows" in windows
    assert "-posix" in windows          # MinGW with POSIX threads: edge264 uses pthreads
    assert "CFLAGS=-march=x86-64 VARIANTS=x86-64-v2,x86-64-v3,logs" in windows  # as the .deb
    for name in ("edge264mvc.2.dll", "edge264mvc_test.exe", "edge264.commit", "LICENSE_BSD.txt"):
        assert name in windows
