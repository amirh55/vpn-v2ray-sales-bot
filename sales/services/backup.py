"""Back the shop up to a single file, and put it back.

The archive holds a database-agnostic JSON dump rather than a copy of the
SQLite file, so a backup taken on SQLite can be restored onto PostgreSQL when
the shop outgrows it.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

BACKUP_FORMAT = 1
DATA_NAME = 'data.json'
META_NAME = 'meta.json'
MEDIA_PREFIX = 'media/'

# contenttypes and permissions are rebuilt from the code on migrate, and
# reloading them clashes with the rows already there. Sessions and admin log
# entries are noise that would only leak activity.
EXCLUDED = [
    'contenttypes',
    'auth.Permission',
    'admin.logentry',
    'sessions',
]


def _dump_data() -> str:
    buffer = io.StringIO()
    call_command(
        'dumpdata',
        *[f'--exclude={label}' for label in EXCLUDED],
        natural_foreign=True,
        natural_primary=True,
        indent=2,
        stdout=buffer,
    )
    return buffer.getvalue()


def _media_files() -> list[Path]:
    root = Path(settings.MEDIA_ROOT)
    if not root.exists():
        return []
    return [path for path in root.rglob('*') if path.is_file()]


def backup_filename() -> str:
    stamp = timezone.localtime().strftime('%Y%m%d-%H%M%S')
    return f'vpnshop-backup-{stamp}.zip'


def create_backup() -> bytes:
    """Return a zip archive of the database contents and uploaded media."""
    media = _media_files()
    meta = {
        'format': BACKUP_FORMAT,
        'created_at': timezone.now().isoformat(),
        'media_files': len(media),
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(META_NAME, json.dumps(meta, ensure_ascii=False, indent=2))
        archive.writestr(DATA_NAME, _dump_data())
        root = Path(settings.MEDIA_ROOT)
        for path in media:
            archive.write(path, MEDIA_PREFIX + str(path.relative_to(root)).replace('\\', '/'))
    return buffer.getvalue()


class RestoreError(RuntimeError):
    pass


def restore_backup(raw: bytes) -> dict:
    """Load a backup archive back into the database and media directory.

    Rows are matched by primary key, so restoring onto an empty database gives
    an exact copy, and restoring onto a live one refreshes the rows it holds
    without deleting anything that is missing from the archive.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise RestoreError('فایل پشتیبان معتبر نیست یا خراب شده است.') from exc

    names = archive.namelist()
    if DATA_NAME not in names:
        raise RestoreError('فایل پشتیبان ناقص است: data.json در آن نیست.')

    meta = {}
    if META_NAME in names:
        try:
            meta = json.loads(archive.read(META_NAME).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            meta = {}
    if meta.get('format', BACKUP_FORMAT) > BACKUP_FORMAT:
        raise RestoreError('این فایل پشتیبان مربوط به نسخه جدیدتری از ربات است.')

    # loaddata only reads from disk, so the dump is staged in a temp file.
    tmp_dir = Path(settings.MEDIA_ROOT).parent / 'restore-tmp'
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f'restore-{datetime.now():%Y%m%d%H%M%S}.json'
    try:
        tmp_path.write_bytes(archive.read(DATA_NAME))
        try:
            call_command('loaddata', str(tmp_path), verbosity=0)
        except Exception as exc:  # noqa: BLE001
            raise RestoreError(f'بازگردانی داده‌ها ناموفق بود: {exc}') from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    media_root = Path(settings.MEDIA_ROOT)
    restored_media = 0
    for name in names:
        if not name.startswith(MEDIA_PREFIX) or name.endswith('/'):
            continue
        relative = name[len(MEDIA_PREFIX):]
        # Never let a crafted archive write outside the media directory.
        target = (media_root / relative).resolve()
        if not str(target).startswith(str(media_root.resolve())):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(name))
        restored_media += 1

    return {'media_files': restored_media, 'meta': meta}
