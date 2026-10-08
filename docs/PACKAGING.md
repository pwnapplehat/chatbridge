# Packaging and supported systems

## Ubuntu / Debian: `.deb`

```bash
packaging/deb/build-deb.sh          # -> dist/chatbridge_<version>_all.deb  (no root, no debhelper; byte-reproducible)
sudo apt install ./dist/chatbridge_*_all.deb
```

Installs the Python package to `/usr/lib/python3/dist-packages/chatbridge`, the `chatbridge` and `chatbridge-gui` commands, the desktop entry and icon, AppStream metadata (software centers), man pages, and an **optional** systemd user unit (`systemctl --user enable --now chatbridge-sync`). Dependencies (`python3 >= 3.11`, `python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1 >= 1.5`) are resolved by apt.

Supported: Ubuntu 24.04 LTS and newer, Debian 13 and newer (libadwaita 1.5 is required for the GUI). Ubuntu 22.04 ships libadwaita 1.1 and Python 3.10, so it is not supported.

Uninstall: `sudo apt remove chatbridge`. Your data (`~/.local/share/chatbridge`, `~/.config/chatbridge`) is left alone.

CI builds the package on every push, installs it on a clean Ubuntu 24.04 runner, smoke-tests the CLI and GUI, checks that removal leaves nothing behind, and uploads the `.deb` as a workflow artifact.

## Any other Linux distribution

```bash
packaging/build-tarball.sh && tar xf dist/chatbridge-*.tar.gz && cd chatbridge-*/ && ./install.sh
```

`install.sh` does a user-level install (no root) and tells you which packages to add if the GTK4/libadwaita bindings are missing:

| Distribution | Dependencies |
|---|---|
| Ubuntu / Debian | `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-venv` |
| Fedora / RHEL | `sudo dnf install python3-gobject gtk4 libadwaita` |
| Arch | `sudo pacman -S python-gobject gtk4 libadwaita` |
| openSUSE | `sudo zypper install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1` |

Only the Ubuntu `.deb` and the generic installer are tested. Native Fedora/Arch packages and a Flatpak are not provided yet; a Flatpak would need broad access to `~/.config/Cursor`, `~/.claude` and the host process list (to detect a running Cursor), which defeats most of the sandbox.

## Versioning

The single source of truth is `chatbridge/__init__.py` (`__version__`); the wheel, the `.deb` and the About dialog all read it.
