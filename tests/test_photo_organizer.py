"""
Unit tests for the pure logic in photo_organizer.Api.

These exercise the parts that actually move files / decide names, since the
tool renames irreversibly. GUI/IPC is not covered (no window in tests;
_safe_eval swallows the missing-window error, so _do_run_rename runs fine).
"""
import os
import json
from datetime import datetime

import pytest
from PIL import Image

from photo_organizer import Api, _UNDO_SIDECAR


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_jpeg(path, dt_datetime=None):
    """Tiny JPEG; if dt_datetime given, write base EXIF DateTime (tag 306)."""
    img = Image.new('RGB', (4, 4), (120, 80, 40))
    if dt_datetime:
        exif = Image.Exif()
        exif[306] = dt_datetime  # 'YYYY:MM:DD HH:MM:SS'
        img.save(path, 'JPEG', exif=exif)
    else:
        img.save(path, 'JPEG')


def api():
    return Api()  # no window; _safe_eval tolerates that


# ── _parse_exif_dt ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('value,expected', [
    ('2021:07:15 09:30:00', datetime(2021, 7, 15, 9, 30, 0)),
    (b'2021:07:15 09:30:00', datetime(2021, 7, 15, 9, 30, 0)),
    ('2021:07:15 09:30:00.123', datetime(2021, 7, 15, 9, 30, 0)),
    ('2021-07-15 09:30:00', datetime(2021, 7, 15, 9, 30, 0)),
    ('0000:00:00 00:00:00', None),
    ('not a date', None),
    ('', None),
    (None, None),
])
def test_parse_exif_dt(value, expected):
    assert Api._parse_exif_dt(value) == expected


# ── _get_date_taken fallback chain ────────────────────────────────────────────

def test_get_date_taken_uses_exif(tmp_path):
    p = tmp_path / 'a.jpg'
    make_jpeg(str(p), '2019:03:04 12:00:00')
    dt, src = api()._get_date_taken(str(p))
    assert dt == datetime(2019, 3, 4, 12, 0, 0)
    assert src == 'datetime'


def test_get_date_taken_mtime_fallback(tmp_path):
    p = tmp_path / 'b.jpg'
    make_jpeg(str(p))  # no EXIF
    ts = datetime(2020, 1, 2, 3, 4, 5).timestamp()
    os.utime(str(p), (ts, ts))
    dt, src = api()._get_date_taken(str(p), allow_mtime=True)
    assert src == 'mtime'
    assert dt == datetime(2020, 1, 2, 3, 4, 5)


def test_get_date_taken_mtime_disabled(tmp_path):
    p = tmp_path / 'c.jpg'
    make_jpeg(str(p))  # no EXIF
    assert api()._get_date_taken(str(p), allow_mtime=False) == (None, None)


# ── scan_folder recursive count ───────────────────────────────────────────────

def test_scan_folder_recursive_count(tmp_path):
    sub = tmp_path / 'sub'
    sub.mkdir()
    make_jpeg(str(tmp_path / 'top.jpg'))
    make_jpeg(str(sub / 'x.jpg'))
    make_jpeg(str(sub / 'y.jpg'))
    (tmp_path / 'note.txt').write_text('ignore me')

    tree = api().scan_folder(str(tmp_path))
    assert tree['images'] == 3            # 1 top + 2 in sub (recursive)
    child = next(c for c in tree['children'] if c['name'] == 'sub')
    assert child['images'] == 2


# ── preview_renaming ──────────────────────────────────────────────────────────

def test_preview_collision_same_timestamp(tmp_path):
    make_jpeg(str(tmp_path / 'one.jpg'), '2021:05:05 10:10:10')
    make_jpeg(str(tmp_path / 'two.jpg'), '2021:05:05 10:10:10')
    res = api().preview_renaming([str(tmp_path)])
    new_names = sorted(op['newName'] for op in res['operations'])
    assert res['total'] == 2
    assert len(set(new_names)) == 2, 'same-timestamp files must not collide'
    assert new_names[0] == '2021_05_05_101010.jpg'
    assert new_names[1] == '2021_05_05_101010_1.jpg'


def test_preview_unnamed_when_mtime_off(tmp_path):
    make_jpeg(str(tmp_path / 'no_exif.jpg'))
    res = api().preview_renaming([str(tmp_path)], allow_mtime=False)
    op = res['operations'][0]
    assert op['reasonCode'] == 'no_exif'
    assert op['newName'].startswith('unnamed_')
    assert res['unnamedCount'] == 1
    assert res['folderCount'] == 1


# ── rename + durable undo roundtrip ───────────────────────────────────────────

def test_rename_then_undo_in_memory(tmp_path):
    p = tmp_path / 'pic.jpg'
    make_jpeg(str(p), '2018:11:23 08:09:10')
    a = api()
    plan = a.preview_renaming([str(tmp_path)])
    a._pending_ops = plan['operations']
    a._do_run_rename(plan['operations'], 'en')

    renamed = tmp_path / '2018_11_23_080910.jpg'
    assert renamed.exists() and not p.exists()
    assert (tmp_path / _UNDO_SIDECAR).exists()

    out = a.undo_renaming([str(tmp_path)])
    assert out['restored'] == 1
    assert p.exists() and not renamed.exists()
    assert not (tmp_path / _UNDO_SIDECAR).exists()


def test_undo_from_sidecar_after_restart(tmp_path):
    p = tmp_path / 'pic.jpg'
    make_jpeg(str(p), '2018:11:23 08:09:10')
    a = api()
    plan = a.preview_renaming([str(tmp_path)])
    a._do_run_rename(plan['operations'], 'en')

    # Simulate a fresh process: in-memory undo stack is gone.
    fresh = api()
    info = fresh.check_undo([str(tmp_path)])
    assert info['available'] and info['count'] == 1

    out = fresh.undo_renaming([str(tmp_path)])
    assert out['restored'] == 1
    assert p.exists()


# ── re-entrancy guard ─────────────────────────────────────────────────────────

def test_start_renaming_busy_guard():
    a = api()
    a._busy = True
    a._pending_ops = [{'oldPath': 'x', 'newPath': 'y'}]
    assert a.start_renaming('de') == {'started': False, 'busy': True}


def test_start_renaming_no_ops():
    a = api()
    a._pending_ops = []
    assert a.start_renaming('de') == {'started': False, 'busy': False, 'count': 0}


# ── _reason_text localisation ─────────────────────────────────────────────────

def test_reason_text():
    from photo_organizer import _LOG_I18N
    loc = _LOG_I18N['en']
    assert Api._reason_text(loc, 'no_exif', '') == loc['no_exif']
    assert Api._reason_text(loc, 'error', 'boom') == "%s: boom" % loc['error']
    assert Api._reason_text(loc, 'original', '2021-01-01 00:00:00') == \
        '2021-01-01 00:00:00 · ' + loc['original']
