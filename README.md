<p align="center">
  <img src="logo.png" alt="swpt logo" width="96">
</p>

<h1 align="center">swpt</h1>

<p align="center">
  <b>Instant search for Windows — every file name on your PC, the text inside your documents, and even scanned paper.</b><br>
  Fast, keyboard-driven, fully local. No Everything, no Windows Search, no cloud.
</p>

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6?logo=windows">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="Qt" src="https://img.shields.io/badge/UI-PySide6%20(Qt%206)-41CD52?logo=qt&logoColor=white">
  <img alt="Local only" src="https://img.shields.io/badge/data-100%25%20local-success">
  <a href="https://github.com/YamYam-ee/swpt/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/YamYam-ee/swpt?color=00B8D4"></a>
</p>

---

## Why swpt?

Windows Search is slow and unpredictable. Third-party tools find file names but not what's *inside* your files — and almost nothing searches the scanned PDFs sitting in your project folders.

**swpt does all three, in one window, as fast as you can type:**

| | |
|---|---|
| ⚡ **This PC** | Searches the name of every file on all local drives — millions of entries, results while you type. |
| 📄 **File contents** | Full-text search inside PDF, Word, Excel, PowerPoint, OpenDocument, RTF, e-mail and plain-text/code files. |
| 🔍 **Scanned documents** | Built-in OCR (Windows' own engine) makes image-only PDFs and photos searchable. |
| 👁️ **Quick preview** | Press <kbd>Space</kbd> to preview — select and copy text, zoom, and search inside the document. |
| 🗂️ **Folder tabs** | Pin your important folders (OneDrive, SharePoint, network shares) as tabs with their own content index. |

## Features

### Whole-PC file name search
- swpt builds **its own index** of every file name on all fixed drives — no dependency on any other program.
- The index is built in a separate **low-priority background process** (below-normal CPU *and* disk priority), takes about a minute and you can keep working meanwhile.
- All names live in memory as one long lowercase string, so a search is a single native `str.find` sweep — **instant even across millions of files**.
- Changes are picked up **live** via `ReadDirectoryChangesW` and layered on top of the index, so new, renamed and deleted files show up immediately.

### Full-text content search
- Content is stored in **SQLite FTS5** with a **trigram tokenizer** and diacritic folding — substring matches anywhere in a word, accent-insensitive (`å`, `é`, `ü` …).
- Extraction runs in a **process pool** so the UI never stutters, and re-indexes only files whose modification time changed.
- Supported formats: `PDF`, `DOCX/DOCM/DOTX`, `XLSX/XLSM`, `PPTX`, `ODT/ODS/ODP`, `RTF`, `EML`, `TXT`, `MD`, `CSV`, `JSON`, `XML`, `HTML`, `YAML`, `SQL`, and most source-code files.
- OneDrive / SharePoint **online-only files are detected and skipped**, so swpt never triggers mass downloads.

### OCR for scanned documents
- Uses **Windows.Media.Ocr** — the OCR engine already built into Windows. Nothing to install, nothing leaves your machine.
- Smart per-page detection: pages that already contain text are used as-is; only image-only pages are rendered (300 DPI) and recognized.
- Works on PDFs and images (`PNG`, `JPG`, `TIFF`, `BMP`, `GIF`) — roughly half a second per page.
- Enable it per folder tab; new scans dropped into the folder are picked up automatically.

### Preview that actually works
- Press <kbd>Space</kbd> on any result.
- **PDFs and images** open in a custom viewer: scroll to page, <kbd>Ctrl</kbd>+scroll to zoom, drag to select text (including OCR'd text on scanned pages), <kbd>Ctrl</kbd>+<kbd>C</kbd> to copy, <kbd>Ctrl</kbd>+<kbd>F</kbd> to find with highlighted matches.
- **Word, Excel, PowerPoint, Outlook and more** are rendered with Windows' own preview handlers (`IPreviewHandler`), with thumbnails as fallback.

### Built for the keyboard
- Global hotkey to summon swpt from anywhere.
- Lives in the **system tray**, optional autostart with Windows.
- Single-instance: launching again just brings the existing window to front.
- Drag results straight into Explorer, Outlook, Teams…

### Always up to date
- swpt checks for a new version **once a day** and shows an **Update to x.y.z** button when one is out.
- One click downloads it, **verifies its SHA-256 checksum**, installs it and restarts swpt — no need to visit GitHub.
- Per-user install: no administrator rights, no UAC prompt. Turn the check off in *Settings → Updates*.

## Search syntax

| Query | Matches |
|---|---|
| `invoice 2024` | Items containing **both** `invoice` and `2024` anywhere (partial words OK) |
| `"135"` | `135`, `135-1`, `135.pdf` — but **not** `135a` or `1350` (whole-word match) |
| `"welding procedure"` | That exact phrase |

Content search requires at least 3 characters per term (trigram index).

## Keyboard shortcuts

| Key | Action |
|---|---|
| <kbd>Space</kbd> | Preview selected file |
| <kbd>Enter</kbd> | Open selected file |
| <kbd>↓</kbd> / <kbd>↑</kbd> | Move between search field and results |
| <kbd>Esc</kbd> | Clear search / close preview |
| <kbd>Ctrl</kbd>+<kbd>T</kbd> | New folder tab |
| <kbd>Ctrl</kbd>+<kbd>W</kbd> | Close tab |
| <kbd>Ctrl</kbd>+<kbd>Tab</kbd> / <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>Tab</kbd> | Next / previous tab |
| <kbd>F5</kbd> | Refresh current tab |
| <kbd>Ctrl</kbd>+<kbd>,</kbd> | Settings |
| <kbd>Ctrl</kbd>+<kbd>F</kbd> (in preview) | Find in document — <kbd>Enter</kbd> / <kbd>Shift</kbd>+<kbd>Enter</kbd> for next / previous |

## Download

<p>
  <a href="https://github.com/YamYam-ee/swpt/releases/latest/download/swpt-setup-x64.msi">
    <img alt="Download installer" src="https://img.shields.io/badge/Download-Installer%20(.msi)-0078D6?style=for-the-badge&logo=windows">
  </a>
</p>

**Installer (recommended):** download **[swpt-setup-x64.msi](https://github.com/YamYam-ee/swpt/releases/latest/download/swpt-setup-x64.msi)** and run it. swpt installs for your user account in `%LOCALAPPDATA%\Programs\swpt`, adds a Start menu shortcut and starts. No administrator rights needed, and future updates install from inside the app. Uninstall under *Settings → Apps → Installed apps*.

**Portable:** prefer no installation? Download **[swpt-windows-x64.zip](https://github.com/YamYam-ee/swpt/releases/latest/download/swpt-windows-x64.zip)**, extract it anywhere and run `swpt.exe`. Updates then go through the installer.

> Windows SmartScreen may warn because the app isn't code-signed. Click **More info → Run anyway**.

## Run from source

**Requirements:** Windows 10 or 11, Python 3.10+.

```bash
git clone https://github.com/YamYam-ee/swpt.git
cd swpt
pip install -r requirements.txt
pythonw swpt.pyw
```

On first launch swpt offers to index your PC. Everything after that is automatic.

> **OCR language pack:** OCR uses the Windows OCR language pack. If it's missing, add it under
> *Settings → Time & language → Language → (your language) → Language options → Optical character recognition*.

### Build the exe and installer yourself

```bash
pip install pyinstaller
dotnet tool install --global wix --version 5.0.2
powershell -ExecutionPolicy Bypass -File build.ps1
```

`dist/` then contains `swpt-windows-x64.zip` and `swpt-setup-x64.msi`.

### Releasing a new version

1. Bump `VERSION` in `version.py` and add a section to `CHANGELOG.md`.
2. Commit, then tag and push: `git tag v1.2.0 && git push origin main v1.2.0`.
3. GitHub Actions builds the exe, zip and MSI and publishes the release with the changelog as notes. Installed copies pick it up within a day.

## Privacy

swpt is **100% local**. Your files, the index (`index.db`) and settings (`config.json`) are stored in `%LOCALAPPDATA%\swpt` and never leave your computer. Turning off indexing deletes the index.

The only network request swpt makes is the once-a-day update check, which asks the GitHub API for the latest release. Nothing about your PC or your files is sent. You can turn it off in *Settings → Updates*.

## Languages

The UI is available in **English** and **Swedish** and follows your Windows display language automatically. Override in Settings or with the environment variable `SWPT_LANG=en` / `sv`.

## Project layout

| File | Purpose |
|---|---|
| `swpt.pyw` | Entry point (no console window) |
| `app.py` | Main window, tabs, result table, background workers, tray & hotkey |
| `diskindex.py` | Whole-PC file name index + live change watcher |
| `extract.py` | Text extraction from documents (runs in worker processes) |
| `ocr.py` | OCR via Windows.Media.Ocr |
| `preview.py` | Quick-preview window (PDF/image viewer + Windows preview handlers) |
| `query.py` | Search query parsing (quoted = exact whole-word) |
| `store.py` | Config and SQLite/FTS5 database |
| `settings.py` | Settings dialog: hotkey, autostart, tray, language, indexing, updates |
| `updater.py` | Checks GitHub for new releases, downloads and installs the MSI |
| `version.py` | The app version (must match the release tag) |
| `installer/swpt.wxs` | MSI installer definition (WiX 5) |
| `build.ps1` | Builds the exe, zip and MSI |
| `i18n.py` | Translations (Swedish ↔ English) |
| `make_icon.py` | Builds `swpt.ico` and `logo.png` from `logo.svg` |

## Built with

[PySide6 / Qt 6](https://doc.qt.io/qtforpython/) · [PyMuPDF](https://pymupdf.readthedocs.io/) · [SQLite FTS5](https://www.sqlite.org/fts5.html) · [Windows.Media.Ocr via PyWinRT](https://github.com/pywinrt/pywinrt) · Win32 APIs via `ctypes`
