# Packaging and supported systems

## Windows 10 / 11

### Release builds

Pushing a tag `vX.Y.Z` (equal to `chatbridge.__version__`, with a `## X.Y.Z` section in `CHANGELOG.md`) runs `.github/workflows/release.yml`: it tests and builds on Ubuntu, Windows x64 and Windows ARM64, smoke-tests every package (the `.deb` is installed and removed; each Windows installer is installed silently, run, and uninstalled), and publishes one GitHub Release with `chatbridge_X.Y.Z_all.deb`, the source `.tar.gz`, `ChatBridge-X.Y.Z-{x64,arm64}-setup.exe`, `ChatBridge-X.Y.Z-{x64,arm64}-portable.zip` and `SHA256SUMS.txt`. Pushing the branch `release-check` does the same without publishing. The Windows programs are frozen with PyInstaller (`packaging/windows/chatbridge.spec`) and wrapped with Inno Setup (`packaging/windows/chatbridge.iss`); they are not code-signed.

### From source

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1             # per-user install (no administrator)
powershell -ExecutionPolicy Bypass -File install.ps1 -Service    # + hidden auto-sync at logon
powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
```

The installer finds the newest Python 3.11+ that has Tk (`py -3.x`, then `python`; pass `-Python C:\path\python.exe` to choose), creates a virtual environment under `%LOCALAPPDATA%\Programs\ChatBridge`, installs the package into it (the only dependency is `psutil`, used to detect a running Cursor) and adds:

- `bin\chatbridge.cmd` and `bin\chatbridge-gui.cmd`, with that folder added to your user `PATH` (`-NoPath` skips it);
- a **ChatBridge** Start Menu shortcut (`-NoShortcuts` skips it);
- with `-Service`, a scheduled task *ChatBridge Auto-Sync* that runs `pythonw -m chatbridge watch --interval 20 --log-file %LOCALAPPDATA%\ChatBridge\watch.log` hidden at every logon (one instance only, restarted on failure). If Task Scheduler refuses, a Startup-folder shortcut is used instead.

`-Prefix`, `-StartMenuDir` and `-TaskName` relocate everything (the CI smoke test uses them). Uninstall removes only those items; your chats, Claude data, settings, link database and undo journals are never touched. `pip install .` also works and provides the same `chatbridge` and `chatbridge-gui` commands (the GUI command is a windowed launcher, so no console opens). CI installs and uninstalls with the script on a clean Windows runner.

Supported: Windows 10 and 11 with Python 3.11 or newer from python.org (Tk is included). Not provided yet: an MSI / winget package or a signed executable.

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
