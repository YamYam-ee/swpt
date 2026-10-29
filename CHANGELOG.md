# Changelog

## [1.2.0] – 2026-10-29

### Featherweight: ~90% less memory

swpt used to sit at around 800 MB of RAM (with peaks of 1.5 GB) on a PC with 2 million files. It now uses **about 90 MB while open and about 25 MB while waiting in the tray**.

- 🪶 **Memory-mapped index.** The file-name index is read straight from disk by Windows instead of being copied into the app's own memory. Windows keeps only the parts in use and can drop them at any time.
- 🔤 **Compact UTF-8 names.** A single emoji in one file name used to make Python store *all* 2 million names at 4 bytes per character (410 MB). Names are now stored as UTF-8.
- 📉 **No more daily 500 MB spike** when the index is refreshed: the new index is simply mapped next to the old one.
- 🧮 **Compact result lists** (~9× smaller) and folder lookups straight from the index file.
- 💤 **Tray mode gives memory back.** After a minute hidden, swpt releases its result list and returns its memory to Windows. Bringing it back and searching still takes well under 0.1 s.
- 🏗️ **Lighter index rebuilds:** the background rebuild now runs once a day (changes are tracked live anyway) and peaks at about half the memory it used to.
- 🧹 Content-indexing helper processes now exit when they're done instead of staying in memory.

Your existing index is converted automatically the first time 1.2.0 starts.

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
