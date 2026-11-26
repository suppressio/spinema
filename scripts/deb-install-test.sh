#!/usr/bin/env bash
# Installs the .deb in a clean, throwaway Debian/Ubuntu container (as root),
# checks it and removes it: run by the Debian package workflow.
#   scripts/deb-install-test.sh <package.deb> recommends      the default install
#   scripts/deb-install-test.sh <package.deb> no-recommends   DLNA only, no FUSE/Samba
# No /dev/fuse needed: FUSE is loaded, not mounted. Never run it on a real system.
set -euo pipefail

deb=$(realpath "$1")
mode=$2
app=/opt/spinema/app

control=$(dpkg-deb -f "$deb" Depends Recommends)
echo "$control"
if grep -E 'pyfuse3|trio' <<<"$control"; then echo "former FUSE packages in $deb" >&2; exit 1; fi
grep -qxF 'Recommends: fuse3, samba' <<<"$control"

installed() { [ "$(dpkg-query -W -f '${db:Status-Status}' "$1" 2>/dev/null)" = installed ]; }

export DEBIAN_FRONTEND=noninteractive
apt-get update
# an install from before the Disks/Spinema rename (#196) left this mount point
mkdir -p /srv/bd3d
case "$mode" in
    recommends) apt-get install -y "$deb" ;;
    no-recommends) apt-get install -y --no-install-recommends "$deb" ;;
    *) echo "unknown mode: $mode" >&2; exit 2 ;;
esac

# the FUSE adapter is the vendored mfusepy: the former Python packages stay out
for package in python3-pyfuse3 python3-trio; do
    if installed "$package"; then echo "$package was installed" >&2; exit 1; fi
done
grep -q '^# Permission to use, copy, modify, and distribute this software' "$app/mfusepy.py"

spinema --version
test ! -e /srv/bd3d
# the DLNA path imports no FUSE module
python3 -c "import sys; sys.path.insert(0, '$app'); import bd3d_fs"

# the Samba block of the release before #196: postinst must replace it, every time
old_share() {
    sed -i '/^# >>> spinema >>>$/,/^# <<< spinema <<<$/d' /etc/samba/smb.conf
    cat >> /etc/samba/smb.conf <<'OLD'

# >>> spinema >>>
[Disks]
   path = /srv/bd3d
   comment = Blu-ray and DVD, played on the fly
   read only = yes
   guest ok = yes
   browseable = yes
# <<< spinema <<<
OLD
    mkdir -p /srv/bd3d
}

if [ "$mode" = recommends ]; then
    testparm -s | grep -q '^\[Spinema\]$'
    old_share
    dpkg-reconfigure spinema
    dpkg-reconfigure spinema
    test ! -e /srv/bd3d
    testparm -s | grep -q '^\[Spinema\]$'
    if testparm -s | grep -q '^\[Disks\]$'; then echo "old [Disks] share left" >&2; exit 1; fi
    [ "$(grep -cxF '# >>> spinema >>>' /etc/samba/smb.conf)" = 1 ]
    testparm -s --section-name Spinema --parameter-name path | grep -qx /srv/spinema
    grep -qxF user_allow_other /etc/fuse.conf
    # loads libfuse3 through mfusepy and builds the adapter, without mounting
    python3 - "$app" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import fuse_fs
from tree import Tree
assert fuse_fs._FUSE_LIBRARY is not None and "fuse3" in fuse_fs._FUSE_LIBRARY, fuse_fs._FUSE_LIBRARY
libfuse = fuse_fs.mfusepy._libfuse
assert libfuse._name == fuse_fs._FUSE_LIBRARY, libfuse._name
assert libfuse.fuse_version() >= 300, libfuse.fuse_version()
fuse_fs.Bd3dFS(Tree())
print("FUSE adapter loaded:", fuse_fs._FUSE_LIBRARY, libfuse.fuse_version())
PY
else
    for package in fuse3 samba python3-pyfuse3 python3-trio; do
        if installed "$package"; then echo "$package was installed" >&2; exit 1; fi
    done
    test ! -e /etc/fuse.conf
    # without libfuse3 the FUSE module fails with ImportError, which bd3d_fs
    # turns into a clear message for --share samba/both
    python3 - "$app" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
try:
    import fuse_fs  # noqa: F401
except ImportError as e:
    print("FUSE not available, as expected:", e)
else:
    raise SystemExit("fuse_fs imported without fuse3")
PY
fi

apt-get remove -y spinema
# "! cmd" does not trip set -e: fail explicitly
if grep -qsF '# >>> spinema >>>' /etc/samba/smb.conf; then exit 1; fi
test ! -e /srv/spinema
test ! -e /srv/bd3d
if [ "$mode" = recommends ]; then
    grep -qxF user_allow_other /etc/fuse.conf
fi
apt-get purge -y spinema
if getent group spinema; then exit 1; fi
echo "install test passed: $mode"
