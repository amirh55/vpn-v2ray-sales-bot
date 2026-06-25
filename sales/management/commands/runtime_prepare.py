from __future__ import annotations

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Prepare persistent runtime directory for server deployments.'

    def add_arguments(self, parser):
        parser.add_argument('--copy-existing', action='store_true', help='Copy .env, db.sqlite3 and media from project root if runtime copies do not exist.')

    def handle(self, *args, **options):
        base_dir = Path(settings.BASE_DIR)
        runtime_dir = Path(settings.RUNTIME_DIR)
        media_dir = Path(settings.MEDIA_ROOT)
        db_path = Path(settings.DATABASES['default']['NAME'])
        env_path = runtime_dir / '.env'

        runtime_dir.mkdir(parents=True, exist_ok=True)
        media_dir.mkdir(parents=True, exist_ok=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        if options['copy_existing']:
            src_env = base_dir / '.env'
            src_db = base_dir / 'db.sqlite3'
            src_media = base_dir / 'media'
            if src_env.exists() and not env_path.exists():
                shutil.copy2(src_env, env_path)
                self.stdout.write(self.style.SUCCESS(f'Copied .env to {env_path}'))
            if src_db.exists() and not db_path.exists():
                shutil.copy2(src_db, db_path)
                self.stdout.write(self.style.SUCCESS(f'Copied db.sqlite3 to {db_path}'))
            if src_media.exists() and src_media.is_dir():
                for item in src_media.iterdir():
                    dst = media_dir / item.name
                    if dst.exists():
                        continue
                    if item.is_dir():
                        shutil.copytree(item, dst)
                    else:
                        shutil.copy2(item, dst)
                self.stdout.write(self.style.SUCCESS(f'Copied media files to {media_dir}'))

        if not env_path.exists():
            env_path.write_text(
                '# تنظیمات پایدار سرور؛ این فایل بیرون از Git نگهداری می‌شود.\n'
                '# مقادیر واقعی را از .env قدیمی یا پنل تنظیم کنید.\n'
                'DEBUG=1\n'
                'ALLOWED_HOSTS=127.0.0.1,localhost\n'
                'PUBLIC_BASE_URL=http://127.0.0.1:8000\n'
                'TIME_ZONE=Asia/Tehran\n',
                encoding='utf-8',
            )
            self.stdout.write(self.style.WARNING(f'Created default runtime env: {env_path}'))

        self.stdout.write(self.style.SUCCESS('Runtime directory is ready.'))
        self.stdout.write(f'RUNTIME_DIR: {runtime_dir}')
        self.stdout.write(f'ENV: {env_path}')
        self.stdout.write(f'DB: {db_path}')
        self.stdout.write(f'MEDIA: {media_dir}')
