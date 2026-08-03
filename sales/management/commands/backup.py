import sys
from pathlib import Path

from django.core.management.base import BaseCommand

from sales.services.backup import backup_filename, create_backup


class Command(BaseCommand):
    help = 'ساخت فایل پشتیبان از داده‌ها و فایل‌های آپلودشده.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--out',
            default='',
            help='مسیر فایل یا پوشه خروجی. پیش‌فرض: پوشه backups کنار داده‌های برنامه.',
        )

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

        from django.conf import settings

        target = Path(options['out']) if options['out'] else Path(settings.MEDIA_ROOT).parent / 'backups'
        if target.suffix.lower() != '.zip':
            target.mkdir(parents=True, exist_ok=True)
            target = target / backup_filename()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)

        target.write_bytes(create_backup())
        size_mb = target.stat().st_size / (1024 * 1024)
        self.stdout.write(self.style.SUCCESS(f'✅ فایل پشتیبان ساخته شد: {target}'))
        self.stdout.write(f'   حجم: {size_mb:.2f} مگابایت')
