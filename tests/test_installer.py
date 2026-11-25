"""Focused checks for the native and Docker dependency decisions."""
import subprocess
from pathlib import Path

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
    # update.sh re-runs install.sh while smb.conf still shares /srv/bd3d; a folder
    # left behind (not empty) must not re-run it on every update (#196)
    update = (ROOT / "scripts/update.sh").read_text(encoding="utf-8")
    assert "[ -e /srv/bd3d ]" not in update
    pattern = update.split("grep -qsE '", 1)[1].split("'", 1)[0]
    old = "[Disks]\n   path = /srv/bd3d\n   read only = yes\n"
    new = "[Spinema]\n   path = /srv/spinema\n   read only = yes\n"
    for conf, migrate in ((old, True), (new, False)):
        smb = tmp_path / "smb.conf"
        smb.write_text(conf, encoding="utf-8")
        found = subprocess.run(["grep", "-qsE", pattern, str(smb)], check=False).returncode == 0
        assert found is migrate
