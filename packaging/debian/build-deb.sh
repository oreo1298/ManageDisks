#!/bin/sh
# Build a .deb for Debian, Ubuntu, Linux Mint, Pop!_OS and friends:
#
#   ./packaging/debian/build-deb.sh            -> ./managedisks_<version>_all.deb
#   sudo apt install ./managedisks_<version>_all.deb
#
# Upgrade: build the new version and install it the same way.
# Remove:  sudo apt remove managedisks
set -eu

cd "$(dirname "$0")/../.."
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' managedisks/__init__.py)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

make -s install DESTDIR="$STAGE/root" PREFIX=/usr PYTHON=/usr/bin/python3
install -Dm644 LICENSE "$STAGE/root/usr/share/doc/managedisks/copyright"
gzip -9n "$STAGE/root/usr/share/man/man1/"*.1

SIZE=$(du -sk "$STAGE/root" | cut -f1)
mkdir -p "$STAGE/root/DEBIAN"
cat > "$STAGE/root/DEBIAN/control" <<EOF
Package: managedisks
Version: $VERSION
Section: admin
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3 (>= 3.9), python3-pyqt6 | python3-pyside6.qtwidgets, fdisk | util-linux (<< 2.29.2-3~), util-linux, e2fsprogs, pkexec | policykit-1
Recommends: qt6-svg-plugins, dosfstools, exfatprogs, ntfs-3g, btrfs-progs, xfsprogs, cryptsetup-bin, smartmontools, udisks2
Suggests: f2fs-tools, hfsprogs, apfsprogs
Maintainer: ManageDisks contributors <noreply@github.com>
Homepage: https://github.com/oreo1298/ManageDisks
Description: modern disk and partition manager
 ManageDisks creates, deletes, formats, resizes, moves and copies
 partitions, converts between MBR and GPT, encrypts with LUKS, checks
 drive health, clones disks and writes disk images. It has a Qt
 interface that follows the desktop theme and a diskpart-style command
 line.
EOF
cat > "$STAGE/root/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
    python3 -m compileall -q /usr/share/managedisks >/dev/null 2>&1 || true
fi
EOF
cat > "$STAGE/root/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
find /usr/share/managedisks -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
EOF
cat > "$STAGE/root/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true
if [ "$1" = purge ]; then rm -rf /var/log/managedisks; fi
EOF
chmod 0755 "$STAGE/root/DEBIAN/postinst" "$STAGE/root/DEBIAN/prerm" "$STAGE/root/DEBIAN/postrm"
OUT="managedisks_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$STAGE/root" "$OUT" >/dev/null
echo "$OUT"
