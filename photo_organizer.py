import os
import sys
import json
import threading
import webview
from PIL import Image
from datetime import datetime

# EXIF tag IDs (see EXIF spec). DateTimeOriginal/Digitized live in the
# Exif sub-IFD (pointed to by tag 0x8769), NOT the base IFD — reading them
# off the base IFD is why so many photos used to fall back to "unnamed_".
_TAG_DATETIME           = 306      # base IFD
_TAG_DATETIME_ORIGINAL  = 36867    # Exif sub-IFD
_TAG_DATETIME_DIGITIZED = 36868    # Exif sub-IFD
_EXIF_IFD_POINTER       = 0x8769

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HEIC_SUPPORTED = True
except ImportError:
    _HEIC_SUPPORTED = False

_SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.webp'}
if _HEIC_SUPPORTED:
    _SUPPORTED_EXTENSIONS.add('.heic')

# Only this many operations are sent to the UI for the preview table; the
# full plan stays server-side (self._pending_ops) so it never crosses the
# JS<->Python bridge twice. Keep in sync with the frontend's render budget.
_PREVIEW_CAP = 800

# Machine-readable undo map written into every processed folder so a rename
# can be reverted even after the app is restarted (the in-memory stack is
# lost on exit). Not an image extension, so _collect_files ignores it.
_UNDO_SIDECAR = '.photo_renamer_undo.json'

# Localized strings for the rename_log.txt file and reason labels.
# The UI is fully i18n'd; the log file follows the active UI language too.
_LOG_I18N = {
    'de': {
        'title': 'Photo Renamer – Rename-Protokoll', 'created': 'Erstellt am',
        'folder': 'Ordner', 'col_old': 'Originalname', 'col_new': 'Neuer Name',
        'col_reason': 'Grund', 'total': 'Gesamt umbenannt', 'files': 'Datei(en)',
        'no_exif': 'Kein EXIF/Datum', 'error': 'Fehler',
        'original': 'EXIF Aufnahme', 'digitized': 'EXIF Digitalisiert',
        'datetime': 'EXIF Datum', 'mtime': 'Datei-Datum',
    },
    'en': {
        'title': 'Photo Renamer – Rename log', 'created': 'Created',
        'folder': 'Folder', 'col_old': 'Original name', 'col_new': 'New name',
        'col_reason': 'Reason', 'total': 'Total renamed', 'files': 'file(s)',
        'no_exif': 'No EXIF/date', 'error': 'Error',
        'original': 'EXIF taken', 'digitized': 'EXIF digitized',
        'datetime': 'EXIF date', 'mtime': 'File date',
    },
    'fr': {
        'title': 'Photo Renamer – Journal de renommage', 'created': 'Créé le',
        'folder': 'Dossier', 'col_old': 'Nom original', 'col_new': 'Nouveau nom',
        'col_reason': 'Raison', 'total': 'Total renommé', 'files': 'fichier(s)',
        'no_exif': 'Sans EXIF/date', 'error': 'Erreur',
        'original': 'EXIF prise', 'digitized': 'EXIF numérisé',
        'datetime': 'EXIF date', 'mtime': 'Date fichier',
    },
    'es': {
        'title': 'Photo Renamer – Registro de renombrado', 'created': 'Creado',
        'folder': 'Carpeta', 'col_old': 'Nombre original', 'col_new': 'Nombre nuevo',
        'col_reason': 'Motivo', 'total': 'Total renombrado', 'files': 'archivo(s)',
        'no_exif': 'Sin EXIF/fecha', 'error': 'Error',
        'original': 'EXIF captura', 'digitized': 'EXIF digitalizado',
        'datetime': 'EXIF fecha', 'mtime': 'Fecha archivo',
    },
    'pl': {
        'title': 'Photo Renamer – Dziennik zmian nazw', 'created': 'Utworzono',
        'folder': 'Folder', 'col_old': 'Oryginalna nazwa', 'col_new': 'Nowa nazwa',
        'col_reason': 'Powód', 'total': 'Łącznie zmieniono', 'files': 'plik(ów)',
        'no_exif': 'Brak EXIF/daty', 'error': 'Błąd',
        'original': 'EXIF zdjęcie', 'digitized': 'EXIF cyfrowe',
        'datetime': 'EXIF data', 'mtime': 'Data pliku',
    },
}


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
        self._window = None       # injected after window creation
        self._undo_stack = []     # [(current_path, original_path), ...]
        self._pending_ops = []    # full plan from the last preview_renaming()
        self._busy = False        # True while a rename batch is running

    # ── Folder picker ──────────────────────────────────────────────────────────

    def select_folder(self):
        """Open native OS folder dialog. Returns chosen path or None."""
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if result and len(result) > 0:
            return result[0]
        return None

    def get_heic_status(self):
        """Return whether HEIC support is enabled."""
        return _HEIC_SUPPORTED

    # ── Folder scanner ─────────────────────────────────────────────────────────

    def scan_folder(self, folder_path):
        """
        Recursively scan folder_path.
        Returns a nested dict tree:
          { name, path, images, children: [...] }
        images = recursive count including children
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
            # Add children's image counts recursively
            total_images = image_count + sum(c['images'] for c in children)
            return {'name': name, 'path': path, 'images': total_images, 'children': children}

        if not os.path.isdir(folder_path):
            return None
        return build_node(folder_path)

    # ── EXIF helper ────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_exif_dt(value):
        """Parse an EXIF datetime value (bytes or str) into a datetime."""
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode(errors='ignore')
        value = str(value).strip().split('.')[0]  # drop sub-seconds
        if not value or value.startswith('0000'):
            return None
        for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        return None

    def _get_date_taken(self, path, allow_mtime=True):
        """
        Return (datetime, source_code). Tries, in order:
          1. EXIF DateTimeOriginal   -> 'original'
          2. EXIF DateTimeDigitized  -> 'digitized'
          3. EXIF DateTime           -> 'datetime'
          4. Filesystem mtime        -> 'mtime'   (only if allow_mtime)
        With allow_mtime=False, files without EXIF return (None, None) and
        become unnamed_ instead of getting a possibly-wrong filesystem date
        (mtime is often the download/copy time, not the capture time).
        """
        try:
            with Image.open(path) as img:
                exif = img.getexif()
                try:
                    sub = exif.get_ifd(_EXIF_IFD_POINTER)
                except Exception:
                    sub = {}
                for tag, src in ((_TAG_DATETIME_ORIGINAL, 'original'),
                                 (_TAG_DATETIME_DIGITIZED, 'digitized')):
                    dt = self._parse_exif_dt(sub.get(tag))
                    if dt:
                        return dt, src
                dt = self._parse_exif_dt(exif.get(_TAG_DATETIME))
                if dt:
                    return dt, 'datetime'
        except Exception:
            pass
        if not allow_mtime:
            return None, None
        try:
            return datetime.fromtimestamp(os.path.getmtime(path)), 'mtime'
        except Exception:
            return None, None

    # ── Rename ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _collect_files(folder_paths):
        """Walk all folders and return [(folder, filename), ...] sorted."""
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
        return all_files

    def preview_renaming(self, folder_paths, allow_mtime=True):
        """
        Compute the full rename plan WITHOUT touching any files.
        The UI shows this as a confirmable preview. Returns:
          { operations: [{oldPath,newPath,oldName,newName,
                           reasonCode,dateStr,hasExif,folder}],
            total, unnamedCount, folderCount }
        allow_mtime mirrors the UI's "use file date as fallback" setting.
        """
        operations = []
        unnamed_count = 1
        # Track names already planned per folder so two photos with the
        # same timestamp don't collide (os.path.exists alone can't see
        # files that haven't been renamed yet during preview).
        planned = {}

        for folder, filename in self._collect_files(folder_paths):
            old_path = os.path.join(folder, filename)
            dt, src = self._get_date_taken(old_path, allow_mtime=allow_mtime)
            ext = os.path.splitext(filename)[1].lower()

            if dt is None:
                base = f'unnamed_{unnamed_count}'
                unnamed_count += 1
                reason_code, date_str, has_exif = 'no_exif', '', False
            else:
                base = dt.strftime('%Y_%m_%d_%H%M%S')
                reason_code = src
                date_str = dt.strftime('%Y-%m-%d %H:%M:%S')
                has_exif = src in ('original', 'digitized', 'datetime')

            used = planned.setdefault(folder, set())
            candidate = f'{base}{ext}'
            counter = 1
            while ((os.path.exists(os.path.join(folder, candidate))
                    and os.path.join(folder, candidate) != old_path)
                   or candidate in used):
                candidate = f'{base}_{counter}{ext}'
                counter += 1
            used.add(candidate)

            operations.append({
                'oldPath': old_path,
                'newPath': os.path.join(folder, candidate),
                'oldName': filename,
                'newName': candidate,
                'reasonCode': reason_code,
                'dateStr': date_str,
                'hasExif': has_exif,
                'folder': folder,
            })

        # Keep the full plan server-side; only ship a capped slice to the UI.
        self._pending_ops = operations
        folders = {op['folder'] for op in operations}
        return {
            'operations': operations[:_PREVIEW_CAP],
            'total': len(operations),
            'unnamedCount': unnamed_count - 1,
            'folderCount': len(folders),
        }

    def start_renaming(self, lang='de'):
        """
        Execute the plan cached by the last preview_renaming() on a
        background thread so the GUI stays responsive. Progress is pushed
        in batches via window._onProgressBatch(...); window._onRenameDone(...)
        fires at the end. Returns immediately.
        """
        if self._busy:
            return {'started': False, 'busy': True}
        operations = self._pending_ops
        self._pending_ops = []
        if not operations:
            return {'started': False, 'busy': False, 'count': 0}
        self._busy = True
        thread = threading.Thread(
            target=self._run_rename, args=(operations, lang), daemon=True
        )
        thread.start()
        return {'started': True, 'count': len(operations)}

    def _safe_eval(self, js):
        """evaluate_js that tolerates the window going away (shutdown)."""
        try:
            self._window.evaluate_js(js)
        except Exception:
            pass

    def _run_rename(self, operations, lang):
        try:
            self._do_run_rename(operations, lang)
        finally:
            self._busy = False

    def _do_run_rename(self, operations, lang):
        renamed_count = 0
        undo_stack = []
        folder_log_map = {}
        folder_undo_map = {}   # folder -> [(new_name, old_name), ...]
        total = len(operations)

        # One IPC round-trip per batch instead of per file. Small runs flush
        # every file (smooth bar); large runs flush every 25 (fast).
        flush_every = 1 if total <= 50 else 25
        buffer = []

        def flush(done):
            if not buffer:
                return
            payload = {'done': done, 'total': total, 'entries': buffer[:]}
            self._safe_eval(f'window._onProgressBatch({json.dumps(payload)})')
            del buffer[:]

        for idx, op in enumerate(operations):
            old_path = op['oldPath']
            new_path = op['newPath']
            reason_code = op['reasonCode']
            date_str = op.get('dateStr', '')

            actually_renamed = False
            if old_path != new_path:
                try:
                    os.rename(old_path, new_path)
                    renamed_count += 1
                    actually_renamed = True
                    undo_stack.append((new_path, old_path))
                    folder_undo_map.setdefault(op['folder'], []).append(
                        (op['newName'], op['oldName'])
                    )
                except OSError as e:
                    reason_code, date_str = 'error', str(e)

            folder_log_map.setdefault(op['folder'], []).append(
                (op['oldName'], op['newName'], reason_code, date_str)
            )

            buffer.append({
                'oldName': op['oldName'],
                'newName': op['newName'],
                'reasonCode': reason_code,
                'dateStr': date_str,
                'hasExif': op['hasExif'],
                'renamed': actually_renamed,
            })
            if len(buffer) >= flush_every:
                flush(idx + 1)

        flush(total)

        for folder, entries in folder_log_map.items():
            self._write_log_file(folder, entries, lang)
        for folder, pairs in folder_undo_map.items():
            self._write_undo_sidecar(folder, pairs)

        self._undo_stack = undo_stack
        summary = {
            'renamed': renamed_count,
            'total': total,
            'unnamedCount': sum(
                1 for op in operations if op['reasonCode'] == 'no_exif'
            ),
            'canUndo': len(undo_stack) > 0,
        }
        self._safe_eval(f'window._onRenameDone({json.dumps(summary)})')

    @staticmethod
    def _write_undo_sidecar(folder, pairs):
        path = os.path.join(folder, _UNDO_SIDECAR)
        data = {
            'created': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'entries': [{'from': new, 'to': old} for new, old in pairs],
        }
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def check_undo(self, folder_paths):
        """
        Report whether any undo sidecar exists under the given folders
        (called when folders are added so the Undo button can reappear
        after a restart). Returns {'available': bool, 'count': n}.
        """
        count = 0
        for folder in folder_paths or []:
            if not os.path.isdir(folder):
                continue
            for root, dirs, files in os.walk(folder):
                dirs.sort()
                if _UNDO_SIDECAR in files:
                    try:
                        with open(os.path.join(root, _UNDO_SIDECAR),
                                  encoding='utf-8') as f:
                            count += len(json.load(f).get('entries', []))
                    except (OSError, ValueError):
                        pass
        return {'available': count > 0, 'count': count}

    def undo_renaming(self, folder_paths=None):
        """
        Reverse the most recent rename. Uses the in-memory stack when
        available (same session); otherwise falls back to the on-disk
        sidecars under folder_paths (survives an app restart).
        Returns {'restored': n}.
        """
        restored = 0
        touched_folders = set()

        if self._undo_stack:
            for current_path, original_path in reversed(self._undo_stack):
                try:
                    if (os.path.exists(current_path)
                            and not os.path.exists(original_path)):
                        os.rename(current_path, original_path)
                        restored += 1
                        touched_folders.add(os.path.dirname(current_path))
                except OSError:
                    pass
            self._undo_stack = []
        else:
            for folder in folder_paths or []:
                if not os.path.isdir(folder):
                    continue
                for root, dirs, files in os.walk(folder):
                    dirs.sort()
                    if _UNDO_SIDECAR not in files:
                        continue
                    sidecar = os.path.join(root, _UNDO_SIDECAR)
                    try:
                        with open(sidecar, encoding='utf-8') as f:
                            entries = json.load(f).get('entries', [])
                    except (OSError, ValueError):
                        continue
                    for e in reversed(entries):
                        cur = os.path.join(root, e['from'])
                        orig = os.path.join(root, e['to'])
                        try:
                            if (os.path.exists(cur)
                                    and not os.path.exists(orig)):
                                os.rename(cur, orig)
                                restored += 1
                        except OSError:
                            pass
                    touched_folders.add(root)

        for folder in touched_folders:
            try:
                os.remove(os.path.join(folder, _UNDO_SIDECAR))
            except OSError:
                pass
        return {'restored': restored}

    @staticmethod
    def _reason_text(loc, reason_code, date_str):
        if reason_code == 'no_exif':
            return loc['no_exif']
        if reason_code == 'error':
            return f"{loc['error']}: {date_str}" if date_str else loc['error']
        label = loc.get(reason_code, reason_code)
        return f'{date_str} · {label}' if date_str else label

    def _write_log_file(self, folder, entries, lang='de'):
        loc = _LOG_I18N.get(lang, _LOG_I18N['de'])
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_path = os.path.join(folder, 'rename_log.txt')
        lines = [
            '=' * 70,
            f"  {loc['title']}",
            f"  {loc['created']}: {timestamp}",
            f"  {loc['folder']}: {folder}",
            '=' * 70,
            '',
            f"{loc['col_old']:<40} {loc['col_new']:<40} {loc['col_reason']}",
            '-' * 100,
        ]
        for old_name, new_name, reason_code, date_str in entries:
            reason = self._reason_text(loc, reason_code, date_str)
            lines.append(f'{old_name:<40} {new_name:<40} {reason}')
        renamed = sum(1 for old, new, _, _ in entries if old != new)
        lines += [
            '',
            '-' * 100,
            f"{loc['total']}: {renamed} {loc['files']}",
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
