# Installing, upgrading and uninstalling ManageDisks

ManageDisks is written in Python with Qt 6 (PySide6 **or** PyQt6, whichever your
distribution ships). It needs no compilation.

* [What gets installed](#what-gets-installed)
* [Requirements](#requirements)
* [Option A: install.sh (all distributions)](#option-a-installsh-all-distributions)
* [Option B: native packages](#option-b-native-packages)
  * [Arch Linux and derivatives](#arch-linux-and-derivatives)
  * [Debian, Ubuntu and derivatives](#debian-ubuntu-and-derivatives)
  * [Fedora, RHEL clones, openSUSE, Mageia](#fedora-rhel-clones-opensuse-mageia)
* [Other distributions](#other-distributions) (Void, Alpine, Gentoo, Solus, NixOS)
* [Option C: make install](#option-c-make-install)
* [Running from source without installing](#running-from-source-without-installing)
* [Troubleshooting](#troubleshooting)

## What gets installed

| Path (prefix `/usr/local` or `/usr`) | Purpose |
|---|---|
| `bin/managedisks`, `bin/managedisks-cli` | the window and the command line tool |
| `lib/managedisks/managedisks-helper` | privileged helper started through pkexec |
| `share/managedisks/` | the Python code |
| `share/applications/io.github.oreo1298.ManageDisks.desktop` | menu entry |
| `share/icons/hicolor/…`, `share/metainfo/…`, `share/man/man1/…` | icon, AppStream data, man pages |
| `/usr/share/polkit-1/actions/io.github.oreo1298.ManageDisks.policy` | polkit rule (always in `/usr/share`, the only place polkit reads) |

Settings live in `~/.config/ManageDisks/`, the operations log in `/var/log/managedisks/`.

## Requirements

Required:

* Python ≥ 3.9
* PySide6 or PyQt6 (Qt ≥ 6.2)
* util-linux (`lsblk`, `sfdisk`, `partx`, `wipefs`, `mkswap`; on Debian/Ubuntu `sfdisk` is in the `fdisk` package)
* e2fsprogs
* polkit (`pkexec`) and a running polkit authentication agent (every desktop has one)

Optional (each enables more features; *Filesystem Support* in the app and
`managedisks-cli support` show what is missing and how to install it):

| Feature | Arch | Debian/Ubuntu | Fedora | openSUSE |
|---|---|---|---|---|
| FAT16/32 | dosfstools | dosfstools | dosfstools | dosfstools |
| exFAT | exfatprogs | exfatprogs | exfatprogs | exfatprogs |
| NTFS | ntfs-3g | ntfs-3g | ntfsprogs | ntfsprogs |
| Btrfs | btrfs-progs | btrfs-progs | btrfs-progs | btrfsprogs |
| XFS | xfsprogs | xfsprogs | xfsprogs | xfsprogs |
| F2FS | f2fs-tools | f2fs-tools | f2fs-tools | f2fs-tools |
| LUKS encryption | cryptsetup | cryptsetup-bin | cryptsetup | cryptsetup |
| Drive health | smartmontools | smartmontools | smartmontools | smartmontools |
| Mac HFS+ | hfsprogs (AUR) | hfsprogs | hfsplus-tools | not packaged¹ |
| Mac APFS (experimental) | build from source² | apfsprogs | build from source² | apfsprogs |
| Mount like the file manager | udisks2 | udisks2 | udisks2 | udisks2 |
| SVG icon themes (Adwaita) | qt6-svg | qt6-svg-plugins | qt6-qtsvg | qt6-svg |

¹ openSUSE has no HFS+ package in its main repositories as far as we know; check
`zypper search hfsplus` or the Open Build Service. ² apfsprogs is not in the
official Arch or Fedora repositories as far as we know: build it from
[github.com/linux-apfs/apfsprogs](https://github.com/linux-apfs/apfsprogs) (see its README)
or look for a community package. Mounting APFS additionally needs the
[linux-apfs-rw](https://github.com/linux-apfs/linux-apfs-rw) kernel module or apfs-fuse.

## Option A: install.sh (all distributions)

```sh
git clone https://github.com/oreo1298/ManageDisks.git
cd ManageDisks
./install.sh
```

The script re-runs itself with `sudo` (or `doas`), detects your distribution,
installs the required and optional dependencies with your package manager, and
installs ManageDisks into `/usr/local` so it never conflicts with distribution
packages.

| Command | What it does |
|---|---|
| `./install.sh` | install dependencies and ManageDisks |
| `./install.sh --minimal` | skip the optional filesystem tools |
| `./install.sh --no-deps` | do not touch the package manager |
| `./install.sh --prefix /usr` | install somewhere else than `/usr/local` |
| `./install.sh --yes` | non-interactive (CI, scripts) |
| `./install.sh deps` | only install dependencies |
| `./install.sh status` | show installed version and missing tools |
| `./install.sh upgrade` | `git pull` the checkout and reinstall into the same prefix |
| `./install.sh uninstall` | remove ManageDisks (`--purge` also removes settings and logs) |

**Upgrade:** `cd ManageDisks && ./install.sh upgrade`. If you downloaded a
tarball instead of cloning, extract the new version and run
`./install.sh upgrade` from it.

**Uninstall:** `./install.sh uninstall` (from any copy of the source; it finds
the prefix that was used). If ManageDisks was installed as a distribution
package, the script tells you to remove it with your package manager instead.

## Option B: native packages

### Arch Linux and derivatives

Manjaro, EndeavourOS, CachyOS, Garuda, Artix:

```sh
git clone https://github.com/oreo1298/ManageDisks.git
cd ManageDisks/packaging/arch
makepkg -si
```

`makepkg` builds the latest commit (`managedisks-git`) and pacman installs it with
all dependencies; optional ones are listed as `optdepends`:

```sh
sudo pacman -S --needed dosfstools exfatprogs ntfs-3g btrfs-progs xfsprogs f2fs-tools cryptsetup smartmontools udisks2
```

* **Upgrade:** `cd ManageDisks/packaging/arch && makepkg -si` (it fetches the newest commit and rebuilds).
* **Uninstall:** `sudo pacman -Rns managedisks-git`

### Debian, Ubuntu and derivatives

Linux Mint, Pop!_OS, elementary, Zorin, KDE neon. Needs Debian 12 / Ubuntu 22.04 or newer.

```sh
sudo apt install git make python3-pyqt6
git clone https://github.com/oreo1298/ManageDisks.git
cd ManageDisks
make deb
sudo apt install ./managedisks_*_all.deb
```

Use `apt install ./file.deb`, not `dpkg -i`, so that apt pulls in the dependencies.

* **Upgrade:** `git pull && make deb && sudo apt install ./managedisks_*_all.deb`
* **Uninstall:** `sudo apt remove managedisks` (`apt purge` also deletes the operations log)

### Fedora, RHEL clones, openSUSE, Mageia

```sh
sudo dnf install git make rpm-build            # openSUSE: sudo zypper install git make rpm-build
git clone https://github.com/oreo1298/ManageDisks.git
cd ManageDisks
make rpm
sudo dnf install ~/rpmbuild/RPMS/noarch/managedisks-*.noarch.rpm
```

On openSUSE install the RPM with `sudo zypper install ~/rpmbuild/RPMS/noarch/managedisks-*.rpm`.
On RHEL/AlmaLinux/Rocky enable EPEL first (`sudo dnf install epel-release`), which provides PySide6/PyQt6 and some filesystem tools.

* **Upgrade:** `git pull && make rpm && sudo dnf upgrade ~/rpmbuild/RPMS/noarch/managedisks-*.rpm`
* **Uninstall:** `sudo dnf remove managedisks` (openSUSE: `sudo zypper remove managedisks`)

## Other distributions

`install.sh` supports these directly; the manual package lists are below in
case you prefer to do it yourself.

| Distribution | Dependencies |
|---|---|
| Void | `sudo xbps-install -S python3 python3-pyqt6 qt6-svg util-linux e2fsprogs polkit make dosfstools exfatprogs ntfs-3g btrfs-progs xfsprogs cryptsetup smartmontools` |
| Alpine | `sudo apk add python3 py3-pyside6 sfdisk util-linux e2fsprogs polkit make dosfstools exfatprogs ntfs-3g-progs btrfs-progs xfsprogs cryptsetup smartmontools` |
| Gentoo | `sudo emerge --ask dev-python/pyside sys-apps/util-linux sys-fs/e2fsprogs sys-auth/polkit sys-fs/dosfstools sys-fs/exfatprogs sys-fs/ntfs3g sys-fs/btrfs-progs sys-fs/xfsprogs sys-fs/cryptsetup sys-apps/smartmontools` |
| Solus | `sudo eopkg install python3 python-pyside6 util-linux e2fsprogs polkit make dosfstools exfatprogs ntfs-3g btrfs-progs xfsprogs cryptsetup smartmontools` |

Then: `sudo make install` (see below). Package names change over time; if one
is not found, search your repositories for “pyside6” or “pyqt6”.

### NixOS

Software cannot be installed into `/usr` on NixOS. Add the dependencies to
`configuration.nix` and run ManageDisks from its checkout:

```nix
environment.systemPackages = with pkgs; [
  (python3.withPackages (ps: [ ps.pyside6 ]))
  util-linux e2fsprogs dosfstools exfatprogs ntfs3g btrfs-progs xfsprogs f2fs-tools
  cryptsetup smartmontools
];
security.polkit.enable = true;
```

```sh
cd ManageDisks && python3 -m managedisks
```

Without the polkit policy installed, the password prompt names `python3`
rather than ManageDisks; everything else works the same.

## Option C: make install

```sh
sudo make install                 # → /usr/local
sudo make install PREFIX=/usr     # → /usr (what packages use)
sudo make uninstall [PREFIX=...]
```

Packagers: `make install DESTDIR=$pkgdir PREFIX=/usr PYTHON=/usr/bin/python3`
stages everything; refresh the icon and desktop caches in post-install.

## Running from source without installing

```sh
python3 -m managedisks              # GUI
python3 -m managedisks.cli --help   # CLI
```

When the policy file is not installed, pkexec asks for permission to run
`python3` as root, which is expected in this mode. `pip install --user .` also
works for development (it provides `managedisks` and `managedisks-cli`
commands, but no menu entry or polkit policy).

## Troubleshooting

**“No polkit authentication agent is running”.** Desktops start one
automatically. On minimal window managers (i3, sway, Hyprland…) start one
yourself, e.g. `polkit-gnome-authentication-agent-1`, `lxqt-policykit-agent`,
`polkit-kde-authentication-agent-1` or `hyprpolkitagent`.

**The window does not follow my dark theme.** ManageDisks follows the color
scheme reported by your desktop (xdg-desktop-portal, GNOME/Cinnamon/MATE
settings, XFCE, or Qt itself). For full native styling on GNOME install a Qt
platform theme (`qgnomeplatform-qt6` or `adwaita-qt6`); on other desktops
`qt6ct` or Kvantum. You can also force it with **☰ → View → Color Scheme** or
`managedisks --scheme dark`.

**Icons are missing or blank.** Install Qt's SVG support (`qt6-svg`,
`qt6-svg-plugins` or `qt6-qtsvg`); ManageDisks falls back to its own icons
otherwise.

**“Neither PySide6 nor PyQt6 is importable”.** Install your distribution's
`pyside6`/`python3-pyside6`/`python3-pyqt6` package. As a last resort:
`python3 -m pip install --user PySide6` (may need `--break-system-packages` on
distributions that protect the system Python).

**A filesystem is greyed out.** Its tools are missing: open
**☰ → Filesystem Support** (or run `managedisks-cli support`) for the exact
install command.

**“The kernel could not be updated … Reboot”.** Some changes cannot be applied
to a disk whose partitions are in use (typically the running system disk). The
partition table on disk is correct; reboot, or make the change from a live USB.
