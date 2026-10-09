#!/bin/sh
# ManageDisks installer for Arch, Debian/Ubuntu, Fedora, openSUSE, Void,
# Alpine, Gentoo, Solus, Mageia and their derivatives.
#
#   ./install.sh              install (dependencies + program, into /usr/local)
#   ./install.sh upgrade      update the source checkout and reinstall
#   ./install.sh uninstall    remove the program (add --purge to also remove settings)
#   ./install.sh deps         only install dependencies
#   ./install.sh status       show what is installed and what is missing
#
# Options: --prefix DIR (default /usr/local), --no-deps, --minimal (skip
# optional filesystem tools), --yes (non-interactive), --purge.
set -eu

ACTION=install
PREFIX=/usr/local
WITH_DEPS=1
OPTIONAL=1
ASSUME_YES=0
PURGE=0

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

ORIG_ARGS="$*"
while [ $# -gt 0 ]; do
    case "$1" in
        install|upgrade|update|uninstall|remove|deps|status) ACTION=$1 ;;
        --prefix) shift; [ $# -gt 0 ] || die "--prefix needs a directory"; PREFIX=$1 ;;
        --prefix=*) PREFIX=${1#--prefix=} ;;
        --no-deps) WITH_DEPS=0 ;;
        --minimal) OPTIONAL=0 ;;
        -y|--yes) ASSUME_YES=1 ;;
        --purge) PURGE=1 ;;
        -h|--help) usage ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
    shift
done
[ "$ACTION" = update ] && ACTION=upgrade
[ "$ACTION" = remove ] && ACTION=uninstall

SRC_DIR=$(cd "$(dirname "$0")" && pwd)

# ---------------------------------------------------------------- privileges
if [ "$ACTION" != status ] && [ "$(id -u)" -ne 0 ]; then
    say "Root privileges are needed; re-running with sudo"
    if command -v sudo >/dev/null 2>&1; then
        # shellcheck disable=SC2086
        exec sudo sh "$0" $ORIG_ARGS
    elif command -v doas >/dev/null 2>&1; then
        # shellcheck disable=SC2086
        exec doas sh "$0" $ORIG_ARGS
    else
        die "run this script as root"
    fi
fi

# ---------------------------------------------------------------- distro detection
FAMILY=unknown
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    IDS=$(. /etc/os-release; echo "${ID:-} ${ID_LIKE:-}")
    # shellcheck disable=SC1091
    PRETTY=$(. /etc/os-release; echo "${PRETTY_NAME:-Linux}")
else
    IDS=""
    PRETTY="Linux"
fi
for id in $IDS; do
    case "$id" in
        arch|archarm|manjaro*|endeavouros|garuda|cachyos|artix|arcolinux|steamos) FAMILY=arch ;;
        debian|ubuntu|linuxmint|pop|elementary|zorin|kali|raspbian|neon|deepin|mx|devuan|pureos) FAMILY=debian ;;
        fedora|rhel|centos|rocky|almalinux|nobara|ultramarine|ol|bazzite) FAMILY=fedora ;;
        opensuse*|suse|sles|sled|aeon|kalpa) FAMILY=suse ;;
        void) FAMILY=void ;;
        alpine|postmarketos) FAMILY=alpine ;;
        gentoo|funtoo|calculate) FAMILY=gentoo ;;
        solus) FAMILY=solus ;;
        mageia|openmandriva) FAMILY=mageia ;;
        nixos) FAMILY=nixos ;;
    esac
    [ "$FAMILY" != unknown ] && break
done

# ---------------------------------------------------------------- package helpers
pkg_available() {
    case "$FAMILY" in
        arch)   pacman -Si "$1" >/dev/null 2>&1 ;;
        debian) apt-cache policy "$1" 2>/dev/null | grep -q 'Candidate: [^(]' ;;
        fedora|mageia) dnf -q info "$1" >/dev/null 2>&1 ;;
        suse)   zypper --non-interactive --quiet search -x "$1" >/dev/null 2>&1 ;;
        void)   xbps-query -R "$1" >/dev/null 2>&1 ;;
        alpine) apk search -e "$1" 2>/dev/null | grep -q . ;;
        solus)  eopkg info "$1" >/dev/null 2>&1 ;;
        gentoo) [ -n "$(find /var/db/repos/*/"$1" -maxdepth 0 2>/dev/null)" ] || \
                [ -n "$(find /usr/portage/"$1" -maxdepth 0 2>/dev/null)" ] ;;
        *) return 1 ;;
    esac
}

# Print the first available package among the arguments.
first_available() {
    for p in "$@"; do
        if pkg_available "$p"; then echo "$p"; return 0; fi
    done
    return 1
}

pm_refresh() {
    case "$FAMILY" in
        debian) apt-get update -qq || true ;;
        arch)   pacman -Sy --noconfirm >/dev/null 2>&1 || true ;;
        alpine) apk update -q || true ;;
        void)   xbps-install -S >/dev/null 2>&1 || true ;;
    esac
}

pm_install() {
    [ $# -gt 0 ] || return 0
    say "Installing packages: $*"
    if [ "$ASSUME_YES" = 1 ]; then Y=1; else Y=0; fi
    case "$FAMILY" in
        arch)   if [ $Y = 1 ]; then pacman -S --needed --noconfirm "$@"; else pacman -S --needed "$@"; fi ;;
        debian) if [ $Y = 1 ]; then DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"; else apt-get install "$@"; fi ;;
        fedora|mageia) if [ $Y = 1 ]; then dnf install -y "$@"; else dnf install "$@"; fi ;;
        suse)   if [ $Y = 1 ]; then zypper --non-interactive install "$@"; else zypper install "$@"; fi ;;
        void)   if [ $Y = 1 ]; then xbps-install -y "$@"; else xbps-install "$@"; fi ;;
        alpine) apk add "$@" ;;
        gentoo) if [ $Y = 1 ]; then emerge --noreplace "$@"; else emerge --ask --noreplace "$@"; fi ;;
        solus)  if [ $Y = 1 ]; then eopkg install -y "$@"; else eopkg install "$@"; fi ;;
        *) return 1 ;;
    esac
}

# want "description" candidate... -> appends the first available candidate to PKGS
PKGS=""
MISSING=""
want() {
    desc=$1; shift
    if p=$(first_available "$@"); then
        PKGS="$PKGS $p"
    else
        MISSING="$MISSING\n  - $desc (tried: $*)"
    fi
}

install_deps() {
    case "$FAMILY" in
        unknown)
            warn "Unknown distribution ($PRETTY): install the dependencies yourself (see docs/INSTALL.md)."
            return 0 ;;
        nixos)
            warn "On NixOS add the dependencies to configuration.nix (see docs/INSTALL.md)."
            return 0 ;;
    esac
    say "Detected $PRETTY ($FAMILY family)"
    pm_refresh
    case "$FAMILY" in
        arch)
            want "Python 3" python
            want "Qt for Python" pyside6 python-pyqt6
            want "Qt SVG support" qt6-svg
            want "util-linux" util-linux
            want "e2fsprogs" e2fsprogs
            want "polkit" polkit
            want "make" make
            if [ $OPTIONAL = 1 ]; then
                want "FAT tools" dosfstools; want "exFAT tools" exfatprogs; want "NTFS tools" ntfs-3g
                want "Btrfs tools" btrfs-progs; want "XFS tools" xfsprogs; want "F2FS tools" f2fs-tools
                want "LUKS" cryptsetup; want "S.M.A.R.T." smartmontools; want "udisks" udisks2
            fi ;;
        debian)
            want "Python 3" python3
            want "Qt for Python" python3-pyqt6 python3-pyside6.qtwidgets
            want "Qt SVG support" qt6-svg-plugins libqt6svg6
            want "sfdisk" fdisk util-linux
            want "e2fsprogs" e2fsprogs
            want "polkit (pkexec)" pkexec policykit-1
            want "make" make
            if [ $OPTIONAL = 1 ]; then
                want "FAT tools" dosfstools; want "exFAT tools" exfatprogs exfat-utils; want "NTFS tools" ntfs-3g
                want "Btrfs tools" btrfs-progs; want "XFS tools" xfsprogs; want "F2FS tools" f2fs-tools
                want "LUKS" cryptsetup-bin cryptsetup; want "S.M.A.R.T." smartmontools; want "udisks" udisks2
            fi ;;
        fedora|mageia)
            want "Python 3" python3
            want "Qt for Python" python3-pyside6 python3-pyqt6
            want "Qt SVG support" qt6-qtsvg
            want "util-linux" util-linux
            want "e2fsprogs" e2fsprogs
            want "polkit" polkit
            want "make" make
            if [ $OPTIONAL = 1 ]; then
                want "FAT tools" dosfstools; want "exFAT tools" exfatprogs; want "NTFS tools" ntfsprogs ntfs-3g
                want "Btrfs tools" btrfs-progs; want "XFS tools" xfsprogs; want "F2FS tools" f2fs-tools
                want "LUKS" cryptsetup; want "S.M.A.R.T." smartmontools; want "udisks" udisks2
            fi ;;
        suse)
            want "Python 3" python3
            want "Qt for Python" python3-pyside6 python3-PyQt6
            want "Qt SVG support" qt6-svg qt6-svg-imports libQt6Svg6
            want "util-linux" util-linux
            want "e2fsprogs" e2fsprogs
            want "polkit" polkit
            want "make" make
            if [ $OPTIONAL = 1 ]; then
                want "FAT tools" dosfstools; want "exFAT tools" exfatprogs; want "NTFS tools" ntfsprogs ntfs-3g
                want "Btrfs tools" btrfsprogs; want "XFS tools" xfsprogs; want "F2FS tools" f2fs-tools
                want "LUKS" cryptsetup; want "S.M.A.R.T." smartmontools; want "udisks" udisks2
            fi ;;
        void)
            want "Python 3" python3
            want "Qt for Python" python3-pyqt6 python3-PyQt6 python3-pyside6
            want "Qt SVG support" qt6-svg
            want "util-linux" util-linux
            want "e2fsprogs" e2fsprogs
            want "polkit" polkit
            want "make" make
            if [ $OPTIONAL = 1 ]; then
                want "FAT tools" dosfstools; want "exFAT tools" exfatprogs; want "NTFS tools" ntfs-3g
                want "Btrfs tools" btrfs-progs; want "XFS tools" xfsprogs; want "F2FS tools" f2fs-tools
                want "LUKS" cryptsetup; want "S.M.A.R.T." smartmontools; want "udisks" udisks2
            fi ;;
        alpine)
            want "Python 3" python3
            want "Qt for Python" py3-pyside6 py3-pyqt6
            want "Qt SVG support" qt6-qtsvg
            want "sfdisk" sfdisk util-linux
            want "util-linux" util-linux
            want "e2fsprogs" e2fsprogs
            want "polkit" polkit
            want "make" make
            if [ $OPTIONAL = 1 ]; then
                want "FAT tools" dosfstools; want "exFAT tools" exfatprogs; want "NTFS tools" ntfs-3g-progs
                want "Btrfs tools" btrfs-progs; want "XFS tools" xfsprogs; want "F2FS tools" f2fs-tools
                want "LUKS" cryptsetup; want "S.M.A.R.T." smartmontools; want "udisks" udisks2
            fi ;;
        gentoo)
            want "Qt for Python" dev-python/pyside dev-python/PyQt6
            want "util-linux" sys-apps/util-linux
            want "e2fsprogs" sys-fs/e2fsprogs
            want "polkit" sys-auth/polkit
            if [ $OPTIONAL = 1 ]; then
                want "FAT tools" sys-fs/dosfstools; want "exFAT tools" sys-fs/exfatprogs
                want "NTFS tools" sys-fs/ntfs3g; want "Btrfs tools" sys-fs/btrfs-progs
                want "XFS tools" sys-fs/xfsprogs; want "F2FS tools" sys-fs/f2fs-tools
                want "LUKS" sys-fs/cryptsetup; want "S.M.A.R.T." sys-apps/smartmontools
            fi ;;
        solus)
            want "Python 3" python3
            want "Qt for Python" python-pyside6 python-pyqt6 pyqt6
            want "util-linux" util-linux
            want "e2fsprogs" e2fsprogs
            want "polkit" polkit
            want "make" make
            if [ $OPTIONAL = 1 ]; then
                want "FAT tools" dosfstools; want "exFAT tools" exfatprogs; want "NTFS tools" ntfs-3g
                want "Btrfs tools" btrfs-progs; want "XFS tools" xfsprogs; want "F2FS tools" f2fs-tools
                want "LUKS" cryptsetup; want "S.M.A.R.T." smartmontools
            fi ;;
    esac
    # shellcheck disable=SC2086
    if ! pm_install $PKGS; then
        warn "installing everything at once failed; trying the packages one by one"
        for p in $PKGS; do
            pm_install "$p" || warn "could not install $p"
        done
    fi
    if [ -n "$MISSING" ]; then
        warn "these could not be found in your repositories:"
        printf "%b\n" "$MISSING" >&2
    fi
}

# ---------------------------------------------------------------- checks
find_python() {
    for p in /usr/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
        if [ -n "$p" ] && [ -x "$p" ]; then echo "$p"; return 0; fi
    done
    return 1
}

check_runtime() {
    PY=$(find_python) || die "python3 not found"
    "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
        || die "Python 3.9 or newer is required"
    if ! "$PY" -c 'import PySide6.QtWidgets' 2>/dev/null && ! "$PY" -c 'import PyQt6.QtWidgets' 2>/dev/null; then
        warn "Neither PySide6 nor PyQt6 is importable by $PY: the command line tool works, the window will not."
        warn "Install your distribution's PySide6/PyQt6 package (see docs/INSTALL.md)."
    fi
    command -v pkexec >/dev/null 2>&1 || warn "pkexec (polkit) is missing: the window cannot request administrator rights"
    command -v sfdisk >/dev/null 2>&1 || [ -x /usr/sbin/sfdisk ] || [ -x /sbin/sfdisk ] \
        || warn "sfdisk is missing: partitions cannot be changed"
}

installed_prefix() {
    for p in "$PREFIX" /usr/local /usr; do
        if [ -f "$p/share/managedisks/install-info" ]; then echo "$p"; return 0; fi
    done
    return 1
}

owned_by_package() {
    f="$1"
    if command -v pacman >/dev/null 2>&1 && pacman -Qo "$f" >/dev/null 2>&1; then return 0; fi
    if command -v dpkg >/dev/null 2>&1 && dpkg -S "$f" >/dev/null 2>&1; then return 0; fi
    if command -v rpm >/dev/null 2>&1 && rpm -qf "$f" >/dev/null 2>&1; then return 0; fi
    return 1
}

do_install() {
    [ -f "$SRC_DIR/Makefile" ] || die "run this script from the ManageDisks source directory"
    if [ $WITH_DEPS = 1 ]; then install_deps; fi
    command -v make >/dev/null 2>&1 || die "'make' is required (install it with your package manager)"
    check_runtime
    if old=$(installed_prefix) && [ "$old" != "$PREFIX" ]; then
        say "Removing the previous installation in $old"
        make -C "$SRC_DIR" -s uninstall PREFIX="$old" >/dev/null
    fi
    say "Installing ManageDisks into $PREFIX"
    make -C "$SRC_DIR" -s install PREFIX="$PREFIX" PYTHON="$PY"
    find "$SRC_DIR/managedisks" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
    say "Done. Start \"ManageDisks\" from your application menu, or run: managedisks"
    say "Command line: managedisks-cli --help   (diskpart-style console: sudo managedisks-cli shell)"
}

do_uninstall() {
    if [ -x /usr/bin/managedisks ] && owned_by_package /usr/bin/managedisks; then
        die "ManageDisks was installed with your package manager; remove it with that (e.g. pacman -R managedisks, apt remove managedisks, dnf remove managedisks)"
    fi
    if p=$(installed_prefix); then
        say "Removing ManageDisks from $p"
        make -C "$SRC_DIR" -s uninstall PREFIX="$p"
    else
        say "No installation made by this script was found; removing from $PREFIX anyway"
        make -C "$SRC_DIR" -s uninstall PREFIX="$PREFIX"
    fi
    if [ $PURGE = 1 ]; then
        say "Removing settings and logs"
        rm -rf /var/log/managedisks
        for home in /root /home/*; do
            rm -rf "$home/.config/ManageDisks" 2>/dev/null || true
        done
    fi
    say "ManageDisks has been removed."
}

do_upgrade() {
    if [ -d "$SRC_DIR/.git" ] && command -v git >/dev/null 2>&1; then
        say "Updating the source checkout"
        owner=$(stat -c %U "$SRC_DIR" 2>/dev/null || echo root)
        if [ "$owner" != root ] && command -v sudo >/dev/null 2>&1; then
            sudo -u "$owner" git -C "$SRC_DIR" pull --ff-only || \
                die "git pull failed (no network, or local changes?). Fix that and run ./install.sh upgrade again."
        else
            git -C "$SRC_DIR" pull --ff-only || \
                die "git pull failed (no network, or local changes?). Fix that and run ./install.sh upgrade again."
        fi
    else
        warn "not a git checkout: download the new version, then run ./install.sh upgrade from it"
    fi
    if p=$(installed_prefix); then PREFIX=$p; fi
    do_install
}

do_status() {
    if p=$(installed_prefix); then
        printf 'Installed: %s\n' "$(grep VERSION "$p/share/managedisks/install-info" | cut -d= -f2) in $p"
    elif command -v managedisks >/dev/null 2>&1; then
        printf 'Installed: %s (package manager)\n' "$(command -v managedisks)"
    else
        printf 'Installed: no\n'
    fi
    printf 'Source:    %s (version %s)\n' "$SRC_DIR" \
        "$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC_DIR/managedisks/__init__.py")"
    printf 'System:    %s (%s family)\n\n' "$PRETTY" "$FAMILY"
    PY=$(find_python) || die "python3 not found"
    PYTHONPATH="$SRC_DIR" "$PY" -m managedisks.cli support
}

case "$ACTION" in
    install)   do_install ;;
    upgrade)   do_upgrade ;;
    uninstall) do_uninstall ;;
    deps)      install_deps; check_runtime ;;
    status)    do_status ;;
esac
