# Changelog

## [1.1.0] – 2026-10-23

### Installer and automatic updates 🎉

- 📦 **Windows installer (MSI).** Installs swpt for your user account in `%LOCALAPPDATA%\Programs\swpt`, adds a Start menu shortcut and shows up under *Installed apps*. No administrator rights needed.
- 🔄 **Update from inside the app.** swpt checks GitHub for a new version once a day. When there is one, an **Update to x.y.z** button appears in the top-right corner. One click downloads it, verifies its SHA-256 checksum, installs it and restarts swpt. No need to visit GitHub.
- ⚙️ **Settings → Updates.** See your version, check for updates now, or turn the automatic check off.

**Upgrading from 1.0.x:** download and run **swpt-setup-x64.msi** below. From now on, updates arrive inside the app. If you ran the zip version, you can delete that folder afterwards (your index and settings are kept).

## [1.0.1] – 2026-10-22

### Performance fix

- 💤 Zero work while hidden in the tray. Live updates only run while the window is visible.
- ⚡ Live updates are throttled so they never take more than ~10% of one core.
- 🚀 Large result lists are up to 8× faster.
- 🗑️ Deleted folders are handled instantly.

## [1.0.0] – 2026-10-15

First release.
