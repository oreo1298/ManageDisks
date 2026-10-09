# ManageDisks – install / uninstall without a package manager.
#
#   sudo make install                 # installs to /usr/local
#   sudo make install PREFIX=/usr     # what distribution packages use
#   sudo make uninstall [PREFIX=...]
#
# Packagers: use DESTDIR=... and run the cache updates in your post-install.

PREFIX     ?= /usr/local
DESTDIR    ?=
PYTHON     ?= /usr/bin/python3
BINDIR     ?= $(PREFIX)/bin
DATADIR    ?= $(PREFIX)/share
LIBDIR     ?= $(PREFIX)/lib
PKGDATADIR ?= $(DATADIR)/managedisks
HELPERDIR  ?= $(LIBDIR)/managedisks
MANDIR     ?= $(DATADIR)/man
# polkit only reads actions from its own directory, whatever PREFIX is.
POLKITDIR  ?= /usr/share/polkit-1/actions

APPID   := io.github.oreo1298.ManageDisks
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' managedisks/__init__.py)
HELPER  := $(HELPERDIR)/managedisks-helper
SOURCES := $(shell find managedisks -name '*.py' -not -path '*/__pycache__/*')

SUBST = sed -e 's|@PYTHON@|$(PYTHON)|g' -e 's|@PKGDATADIR@|$(PKGDATADIR)|g' -e 's|@HELPER@|$(HELPER)|g'

.PHONY: all install uninstall check test lint deb rpm dist clean help

all: help

help:
	@echo "ManageDisks $(VERSION)"
	@echo "  make install    install (PREFIX=$(PREFIX))"
	@echo "  make uninstall  remove an installation made with 'make install'"
	@echo "  make check      run the test suite"
	@echo "  make deb        build a .deb package (Debian/Ubuntu)"
	@echo "  make rpm        build an .rpm package (Fedora/openSUSE)"
	@echo "  make dist       create a source tarball"

install:
	@test -n "$(VERSION)" || { echo "cannot determine version"; exit 1; }
	install -d $(DESTDIR)$(PKGDATADIR)
	@for f in $(SOURCES); do \
		install -D -m 0644 "$$f" "$(DESTDIR)$(PKGDATADIR)/$$f" || exit 1; \
	done
	install -d $(DESTDIR)$(BINDIR) $(DESTDIR)$(HELPERDIR)
	$(SUBST) data/bin/managedisks.in > $(DESTDIR)$(BINDIR)/managedisks
	$(SUBST) data/bin/managedisks-cli.in > $(DESTDIR)$(BINDIR)/managedisks-cli
	$(SUBST) data/bin/managedisks-helper.in > $(DESTDIR)$(HELPER)
	chmod 0755 $(DESTDIR)$(BINDIR)/managedisks $(DESTDIR)$(BINDIR)/managedisks-cli $(DESTDIR)$(HELPER)
	install -D -m 0644 data/$(APPID).desktop $(DESTDIR)$(DATADIR)/applications/$(APPID).desktop
	install -D -m 0644 data/$(APPID).metainfo.xml $(DESTDIR)$(DATADIR)/metainfo/$(APPID).metainfo.xml
	install -D -m 0644 data/icons/hicolor/scalable/apps/$(APPID).svg \
		$(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/$(APPID).svg
	install -D -m 0644 data/icons/hicolor/symbolic/apps/$(APPID)-symbolic.svg \
		$(DESTDIR)$(DATADIR)/icons/hicolor/symbolic/apps/$(APPID)-symbolic.svg
	install -d $(DESTDIR)$(POLKITDIR)
	$(SUBST) data/$(APPID).policy.in > $(DESTDIR)$(POLKITDIR)/$(APPID).policy
	chmod 0644 $(DESTDIR)$(POLKITDIR)/$(APPID).policy
	install -D -m 0644 data/man/managedisks.1 $(DESTDIR)$(MANDIR)/man1/managedisks.1
	install -D -m 0644 data/man/managedisks-cli.1 $(DESTDIR)$(MANDIR)/man1/managedisks-cli.1
	@if [ -z "$(DESTDIR)" ]; then \
		$(PYTHON) -m compileall -q $(PKGDATADIR)/managedisks >/dev/null 2>&1 || true; \
		gtk-update-icon-cache -q -t -f $(DATADIR)/icons/hicolor >/dev/null 2>&1 || true; \
		update-desktop-database -q $(DATADIR)/applications >/dev/null 2>&1 || true; \
		echo "PREFIX=$(PREFIX)" > $(PKGDATADIR)/install-info; \
		echo "VERSION=$(VERSION)" >> $(PKGDATADIR)/install-info; \
		echo "Installed ManageDisks $(VERSION) to $(PREFIX)"; \
	fi

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/managedisks $(DESTDIR)$(BINDIR)/managedisks-cli
	rm -rf $(DESTDIR)$(HELPERDIR)
	rm -rf $(DESTDIR)$(PKGDATADIR)
	rm -f $(DESTDIR)$(DATADIR)/applications/$(APPID).desktop
	rm -f $(DESTDIR)$(DATADIR)/metainfo/$(APPID).metainfo.xml
	rm -f $(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/$(APPID).svg
	rm -f $(DESTDIR)$(DATADIR)/icons/hicolor/symbolic/apps/$(APPID)-symbolic.svg
	rm -f $(DESTDIR)$(MANDIR)/man1/managedisks.1 $(DESTDIR)$(MANDIR)/man1/managedisks-cli.1
	@# Only remove the polkit policy if it points at this installation's helper.
	@if grep -qs "$(HELPER)" $(DESTDIR)$(POLKITDIR)/$(APPID).policy; then \
		rm -f $(DESTDIR)$(POLKITDIR)/$(APPID).policy; fi
	@if [ -z "$(DESTDIR)" ]; then \
		gtk-update-icon-cache -q -t -f $(DATADIR)/icons/hicolor >/dev/null 2>&1 || true; \
		update-desktop-database -q $(DATADIR)/applications >/dev/null 2>&1 || true; \
		echo "Removed ManageDisks from $(PREFIX)"; \
	fi

check test:
	$(PYTHON) -m pytest -q tests

lint:
	$(PYTHON) -m pyflakes managedisks tests scripts || true

dist:
	git archive --format=tar.gz --prefix=managedisks-$(VERSION)/ -o managedisks-$(VERSION).tar.gz HEAD
	@echo "managedisks-$(VERSION).tar.gz"

deb:
	./packaging/debian/build-deb.sh

rpm: dist
	mkdir -p $(HOME)/rpmbuild/SOURCES
	cp managedisks-$(VERSION).tar.gz $(HOME)/rpmbuild/SOURCES/
	rpmbuild -ba packaging/rpm/managedisks.spec

clean:
	rm -rf build dist *.tar.gz *.deb
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
