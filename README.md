# photoRenamer 📸

A **fully offline** desktop tool that renames photos based on their capture
date – scheme `YYYY_MM_DD_HHMMSS.ext` (e.g. `2021_07_15_093000.jpg`). There
is **no network connection**: all images, EXIF data and logs stay solely on
your machine.

Technically: a Python backend using
[pywebview](https://pywebview.flowlib.org/) (native window frame) + a single
`ui.html` as the interface.

---

## What the tool does

- **Recursive folder scan** – tree view with checkboxes; the image count per
  folder includes all subfolders.
- **Preview before renaming** – the full "old name → new name" list including
  the reason is shown in a dialog. **Not a single file is touched until you
  confirm.**
- **Date detection** with an EXIF metadata fallback chain:
  `DateTimeOriginal` → `DateTimeDigitized` → `DateTime` → (optional) file
  modified date. The file date can be turned off and is marked yellow,
  because it is often the download/copy time, not the capture time.
- **Undo** – reverts the last run. Works even after restarting the app,
  because a machine-readable `.photo_renamer_undo.json` is written per folder.
- **Log** – a `rename_log.txt` is written per folder in the UI language
  (DE / EN / FR / ES / PL).
- **HEIC support** when `pillow-heif` is installed (status shown in the app;
  without the library `.heic` files are skipped).
- Runs on a background thread with batched progress – the UI stays responsive
  even with thousands of images.
- Keyboard: `Enter` = start/confirm, `Ctrl/Cmd+K` = add folder,
  `Esc` = close dialog.

Supported formats: `.jpg .jpeg .png .tiff .tif .webp` (+ `.heic` with
pillow-heif). Video/RAW are intentionally **not** supported.

---

## Requirements

- **Python 3.8 or newer** (the tool uses f-strings and `Image.Exif` –
  Python 2 will not work).
- OS: Windows, macOS or Linux (Windows, of course, for the `.exe`).
- Dependencies in `requirements.txt`:
  - `pywebview` – native window
  - `pillow` – image/EXIF access
  - `pillow-heif` – optional, for `.heic`

---

## Option A: Run as Python code

1. Get the repository (clone, or download and unzip the ZIP).
2. (Recommended) create a virtual environment:

   **Windows (PowerShell):**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
   **macOS / Linux:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Run:
   ```bash
   python photo_organizer.py
   ```

The window opens; add folders on the left, review the preview, confirm.
Done.

> Note: `ui.html` must sit next to `photo_organizer.py` – the interface is
> loaded from there.

---

## Option B: Build your own Windows `.exe`

This uses [PyInstaller](https://pyinstaller.org/). The key point is that
`ui.html` must be **bundled into the EXE** – the code finds it at runtime via
`resource_path()` (which handles PyInstaller's `sys._MEIPASS` automatically).

1. On a **Windows machine**, install the dependencies + PyInstaller:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt pyinstaller
   ```

2. Build (a single EXE, no console window):
   ```powershell
   pyinstaller --onefile --windowed `
     --name photoRenamer `
     --add-data "ui.html;." `
     --collect-all pillow_heif `
     photo_organizer.py
   ```

   What the options mean:
   - `--onefile` – everything in **one** `photoRenamer.exe`.
   - `--windowed` – no black console window (pure GUI app).
   - `--name photoRenamer` – name of the output file.
   - `--add-data "ui.html;."` – bundles `ui.html` into the EXE.
     **Caution:** on Windows the separator is a **semicolon** (`;`), on
     macOS/Linux a **colon** (`ui.html:.`).
   - `--collect-all pillow_heif` – ensures the HEIC library is fully
     included. Omit it if you don't need HEIC / `pillow-heif` isn't installed.

3. The result is at `dist\photoRenamer.exe`. That file is standalone and can
   be shared without a Python installation.

Optional custom icon:
```powershell
pyinstaller --onefile --windowed --name photoRenamer `
  --add-data "ui.html;." --collect-all pillow_heif `
  --icon myicon.ico photo_organizer.py
```

### If the EXE won't start

- **Blank / white window:** `ui.html` was not embedded – check the
  `--add-data` argument and the semicolon on Windows.
- **HEIC files missing:** add `--collect-all pillow_heif`.
- **pywebview error on startup:** on Windows pywebview uses the Microsoft
  Edge WebView2 runtime. It is usually present on current Windows 10/11
  systems; otherwise run Microsoft's "Evergreen WebView2 Runtime" installer
  once.
- For debugging, drop `--windowed` so error messages appear in a console
  window.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite stubs `webview`, so it runs headless without pywebview. Covered:
EXIF parsing and the fallback chain, recursive image counting, the preview
collision logic, the re-entrancy guard, and the full rename + undo roundtrip
(including sidecar restore after a "restart").

---

## Project structure

```
photo_organizer.py        Backend: scan, EXIF, rename, undo, logging
ui.html                   Complete UI (HTML/CSS/JS, 5 languages)
requirements.txt          Runtime dependencies
requirements-dev.txt      + pytest for the tests
tests/                    pytest suite (headless, webview stubbed)
```

Everything offline, no telemetry, no external calls.
