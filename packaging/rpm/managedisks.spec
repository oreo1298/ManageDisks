# RPM spec for Fedora, RHEL/Alma/Rocky (with EPEL), openSUSE and Mageia.
#
#   make rpm                      # builds ~/rpmbuild/RPMS/noarch/managedisks-*.rpm
#   sudo dnf install ~/rpmbuild/RPMS/noarch/managedisks-*.rpm      (zypper install on openSUSE)
# Upgrade: build the new version and install it the same way.
# Remove:  sudo dnf remove managedisks   (zypper remove managedisks)

Name:           managedisks
Version:        1.0.0
Release:        1%{?dist}
Summary:        Modern disk and partition manager
License:        MIT
URL:            https://github.com/oreo1298/ManageDisks
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  make
Requires:       python3 >= 3.9
Requires:       (python3-pyside6 or python3-pyqt6 or python3-PyQt6)
Requires:       util-linux
Requires:       e2fsprogs
Requires:       polkit
Recommends:     dosfstools
Recommends:     exfatprogs
Recommends:     (ntfsprogs or ntfs-3g)
Recommends:     btrfs-progs
Recommends:     xfsprogs
Recommends:     cryptsetup
Recommends:     smartmontools
Recommends:     udisks2
Suggests:       f2fs-tools

%description
ManageDisks creates, deletes, formats, resizes, moves and copies partitions,
converts between MBR and GPT, encrypts with LUKS, checks drive health, clones
disks and writes disk images. It has a Qt interface that follows the desktop
theme and a diskpart-style command line.

%prep
%autosetup -n %{name}-%{version}

%build
# Pure Python: nothing to build.

%install
make install DESTDIR=%{buildroot} PREFIX=%{_prefix} PYTHON=%{__python3} LIBDIR=%{_prefix}/lib

%post
gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor &>/dev/null || :
update-desktop-database -q %{_datadir}/applications &>/dev/null || :

%postun
gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor &>/dev/null || :
update-desktop-database -q %{_datadir}/applications &>/dev/null || :

%files
%license LICENSE
%doc README.md
%{_bindir}/managedisks
%{_bindir}/managedisks-cli
%{_prefix}/lib/managedisks/
%{_datadir}/managedisks/
%{_datadir}/applications/io.github.oreo1298.ManageDisks.desktop
%{_datadir}/metainfo/io.github.oreo1298.ManageDisks.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/io.github.oreo1298.ManageDisks.svg
%{_datadir}/icons/hicolor/symbolic/apps/io.github.oreo1298.ManageDisks-symbolic.svg
%{_datadir}/polkit-1/actions/io.github.oreo1298.ManageDisks.policy
%{_mandir}/man1/managedisks.1*
%{_mandir}/man1/managedisks-cli.1*

%changelog
* Fri Oct 09 2026 ManageDisks contributors <noreply@github.com> - 1.0.0-1
- First release
