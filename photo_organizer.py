import os
import sys
import json
import webview
from PIL import Image
from datetime import datetime

_EXIF_DATE_TIME_ORIGINAL = 36867  # EXIF tag ID, see EXIF spec §4.6.5

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HEIC_SUPPORTED = True
except ImportError:
    _HEIC_SUPPORTED = False

_SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.webp'}
if _HEIC_SUPPORTED:
    _SUPPORTED_EXTENSIONS.add('.heic')


def resource_path(relative_path):
    """Get absolute path to resource – works for dev and PyInstaller .exe"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


class Api:
    """
    All public methods are callable from JavaScript via:
    window.pywebview.api.<method>()
    """

    def __init__(self):
        self._window = None  # injected after window creation

    # ── Folder picker ──────────────────────────────────────────────────────────

    def select_folder(self):
        """Open native OS folder dialog. Returns chosen path or None."""
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if result and len(result) > 0:
            return result[0]
        return None

    # ── Folder scanner ─────────────────────────────────────────────────────────

    def scan_folder(self, folder_path):
        """
        Recursively scan folder_path.
        Returns a nested dict tree:
          { name, path, images, children: [...] }
        """
        def build_node(path):
            name = os.path.basename(path) or path
            children = []
            image_count = 0
            try:
                entries = sorted(
                    os.scandir(path),
                    key=lambda e: (not e.is_dir(), e.name.lower())
                )
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        children.append(build_node(entry.path))
                    elif entry.is_file():
                        ext = os.path.splitext(entry.name)[1].lower()
                        if ext in _SUPPORTED_EXTENSIONS:
                            image_count += 1
            except PermissionError:
                pass
            return {'name': name, 'path': path, 'images': image_count, 'children': children}

        if not os.path.isdir(folder_path):
            return None
        return build_node(folder_path)

    # ── EXIF helper ────────────────────────────────────────────────────────────

    def _get_date_taken(self, path):
        try:
            with Image.open(path) as img:
                exif_data = img.getexif()
                date_str = exif_data.get(_EXIF_DATE_TIME_ORIGINAL)
                if date_str:
                    return datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
        except Exception:
            pass
        return None

    # ── Rename ─────────────────────────────────────────────────────────────────

    def start_renaming(self, folder_paths):
        """
        Rename all images in the given folder_paths list.
        Calls window._onProgress(progressObj) for each file so the UI
        can update the progress bar and log table in real time.
        Returns a summary dict when done.
        """
        renamed_count = 0
        unnamed_count = 1
        folder_log_map = {}

        # Collect all files first for accurate progress reporting
        all_files = []
        for folder in folder_paths:
            if not os.path.isdir(folder):
                continue
            for root, dirs, files in os.walk(folder):
                dirs.sort()
                for filename in sorted(files):
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in _SUPPORTED_EXTENSIONS:
                        all_files.append((root, filename))

        total = len(all_files)

        for idx, (folder, filename) in enumerate(all_files):
            old_path = os.path.join(folder, filename)
            date_obj = self._get_date_taken(old_path)

            if date_obj is None:
                new_base = f'unnamed_{unnamed_count}'
                reason = 'Kein EXIF'
                has_exif = False
                unnamed_count += 1
            else:
                new_base = date_obj.strftime('%Y_%m_%d_%H%M%S')
                reason = date_obj.strftime('%Y-%m-%d %H:%M:%S')
                has_exif = True

            ext = os.path.splitext(filename)[1].lower()
            new_name = f'{new_base}{ext}'
            new_path = os.path.join(folder, new_name)

            # Duplicate check
            counter = 1
            while os.path.exists(new_path) and new_path != old_path:
                new_name = f'{new_base}_{counter}{ext}'
                new_path = os.path.join(folder, new_name)
                counter += 1

            actually_renamed = False
            if old_path != new_path:
                try:
                    os.rename(old_path, new_path)
                    renamed_count += 1
                    actually_renamed = True
                except OSError as e:
                    reason = f'Fehler: {e}'

            folder_log_map.setdefault(folder, []).append(
                (filename, new_name, reason)
            )

            # Push live progress update to the JS frontend
            progress = {
                'done': idx + 1,
                'total': total,
                'entry': {
                    'oldName': filename,
                    'newName': new_name,
                    'reason': reason,
                    'hasExif': has_exif,
                    'renamed': actually_renamed,
                    'folder': folder,
                }
            }
            self._window.evaluate_js(
                f'window._onProgress({json.dumps(progress)})'
            )

        # Write rename_log.txt into each processed folder
        for folder, entries in folder_log_map.items():
            self._write_log_file(folder, entries)

        return {
            'renamed': renamed_count,
            'total': total,
            'unnamedCount': unnamed_count - 1,
        }

    def _write_log_file(self, folder, entries):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_path = os.path.join(folder, 'rename_log.txt')
        lines = [
            '=' * 70,
            '  Vibe Photo Renamer – Rename-Protokoll',
            f'  Erstellt am: {timestamp}',
            f'  Ordner:      {folder}',
            '=' * 70,
            '',
            f"{'Originalname':<40} {'Neuer Name':<40} {'Grund'}",
            '-' * 100,
        ]
        for old_name, new_name, reason in entries:
            lines.append(f'{old_name:<40} {new_name:<40} {reason}')
        lines += [
            '',
            '-' * 100,
            f"Gesamt umbenannt: {sum(1 for old, new, _ in entries if old != new)} Datei(en)",
            '=' * 70,
        ]
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    api = Api()

    window = webview.create_window(
        title='Vibe Photo Renamer 📸',
        url=resource_path('ui.html'),
        js_api=api,
        width=820,
        height=820,
        min_size=(700, 600),
        background_color='#0d0d0f',
    )

    api._window = window
    webview.start(debug=False)
