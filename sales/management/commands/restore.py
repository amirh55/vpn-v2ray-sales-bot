import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from sales.services.backup import RestoreError, restore_backup


class Command(BaseCommand):
    help = 'بازگردانی داده‌ها از یک فایل پشتیبان.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='مسیر فایل zip پشتیبان.')
        parser.add_argument(
            '--yes',
            action='store_true',
            help='بدون پرسیدن تایید، بازگردانی را انجام بده.',
        )

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

        path = Path(options['path'])
        if not path.is_file():
            raise CommandError(f'فایل پیدا نشد: {path}')

        if not options['yes']:
            self.stdout.write(self.style.WARNING(
                'بازگردانی، رکوردهای فعلی با همان شناسه را بازنویسی می‌کند. '
                'برای ادامه دوباره با سوییچ --yes اجرا کنید.'
            ))
            return

        try:
            result = restore_backup(path.read_bytes())
        except RestoreError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS('✅ بازگردانی انجام شد.'))
        self.stdout.write(f'   فایل‌های بازگردانی‌شده: {result["media_files"]}')
        self.stdout.write('   سرویس‌ها را ری‌استارت کنید: vpnshop restart')
