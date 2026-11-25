#!/usr/bin/env bash
# Undo scripts/install.sh. apt packages and the user_allow_other line in
# /etc/fuse.conf are left in place (other software may use them).
set -euo pipefail

MARK_BEGIN="# >>> spinema >>>"
MARK_END="# <<< spinema <<<"
BASE=/srv/spinema

# installs from before the rename (bluray3d-xr) leave the same things under the old name
OLD_BEGIN="# >>> bluray3d-xr >>>"
OLD_END="# <<< bluray3d-xr <<<"

# /srv/bd3d: the mount point used before the Disks/Spinema rename, if still left over
for mount in "$BASE/Disks" /srv/bd3d; do
    if mountpoint -q "$mount"; then
        fusermount3 -u "$mount" || sudo umount "$mount"
    fi
done
if grep -qF -e "$MARK_BEGIN" -e "$OLD_BEGIN" /etc/samba/smb.conf; then
    sudo sed -i -e "/^$MARK_BEGIN\$/,/^$MARK_END\$/d" -e "/^$OLD_BEGIN\$/,/^$OLD_END\$/d" /etc/samba/smb.conf
    sudo systemctl restart smbd 2>/dev/null || sudo service smbd restart
    echo "Samba share [Spinema] removed"
fi
sudo rm -rf /opt/spinema /usr/local/bin/spinema /opt/bluray3d-xr /usr/local/bin/bluray3d-xr
sudo rmdir "$BASE/Disks" "$BASE" /srv/bd3d 2>/dev/null || true
echo "Removed. Packages left installed: ffmpeg fuse3 samba libbluray libaacs0 libbdplus0 libdvdread"
echo "(remove them with apt if nothing else needs them)"
