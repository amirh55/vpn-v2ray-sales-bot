"""Point Nginx at the domain and certificate configured in the panel.

The panel stores the values but cannot install them: writing under /etc/nginx
and reloading the service needs root, which the web process should not rely on.
This command is what `vpnshop domain` runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from sales.models import SiteSetting
from sales.services.site_urls import admin_url, certificate_status

NGINX_CONF = Path('/etc/nginx/conf.d/vpnshop.conf')

TEMPLATE = """# ساخته‌شده توسط vpnshop domain — دستی ویرایش نکنید
server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    http2 on;
    server_name {domain};
    client_max_body_size 25m;

    ssl_certificate     {cert};
    ssl_certificate_key {key};

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""

PLAIN_TEMPLATE = """# ساخته‌شده توسط vpnshop domain — دستی ویرایش نکنید
server {{
    listen 80;
    server_name {domain};
    client_max_body_size 25m;

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""


class Command(BaseCommand):
    help = 'اعمال دامنه و گواهی SSL ثبت‌شده در پنل روی Nginx.'

    def add_arguments(self, parser):
        parser.add_argument('--port', default=os.getenv('PORT', '8000'), help='پورت داخلی پنل.')
        parser.add_argument('--print', action='store_true', dest='print_only',
                            help='فقط تنظیمات را نشان بده و چیزی را تغییر نده.')

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

        site = SiteSetting.get_solo()
        domain = (site.public_domain or '').strip().strip('/')
        if not domain:
            raise CommandError(
                'دامنه‌ای ثبت نشده است. در پنل، «تنظیمات اصلی ربات» → بخش «دامنه و SSL» '
                'دامنه را وارد و ذخیره کنید.'
            )
        if domain.startswith(('http://', 'https://')):
            domain = domain.split('://', 1)[1].strip('/')

        use_ssl = bool(site.force_https and site.ssl_cert_path and site.ssl_key_path)
        if site.force_https and not (site.ssl_cert_path and site.ssl_key_path):
            raise CommandError(
                'حالت https روشن است ولی مسیر گواهی یا کلید خصوصی وارد نشده. '
                'یا مسیرها را در پنل وارد کنید یا گزینه https را خاموش کنید.'
            )
        if use_ssl:
            broken = [r for r in certificate_status(site) if not r['ok']]
            if broken:
                for row in broken:
                    self.stdout.write(self.style.ERROR(f'  {row["label"]}: {row["note"]} ({row["path"]})'))
                raise CommandError('فایل‌های گواهی قابل استفاده نیستند؛ مسیرها را بررسی کنید.')

        template = TEMPLATE if use_ssl else PLAIN_TEMPLATE
        config = template.format(
            domain=domain,
            cert=site.ssl_cert_path.strip(),
            key=site.ssl_key_path.strip(),
            port=options['port'],
        )

        if options['print_only']:
            self.stdout.write(config)
            return

        if not shutil_which('nginx'):
            raise CommandError('Nginx روی این سرور نصب نیست. اول آن را نصب کنید: apt install nginx')
        if os.geteuid() != 0:
            raise CommandError('این دستور باید با کاربر root اجرا شود.')

        previous = NGINX_CONF.read_text(encoding='utf-8') if NGINX_CONF.exists() else None
        NGINX_CONF.parent.mkdir(parents=True, exist_ok=True)
        NGINX_CONF.write_text(config, encoding='utf-8')

        test = subprocess.run(['nginx', '-t'], capture_output=True, text=True)
        if test.returncode != 0:
            # Put the working config back rather than leave nginx unable to start.
            if previous is None:
                NGINX_CONF.unlink(missing_ok=True)
            else:
                NGINX_CONF.write_text(previous, encoding='utf-8')
            self.stdout.write(test.stderr.strip())
            raise CommandError('تنظیمات Nginx معتبر نبود؛ تغییرات برگردانده شد.')

        reload_result = subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True, text=True)
        if reload_result.returncode != 0:
            subprocess.run(['systemctl', 'restart', 'nginx'], capture_output=True, text=True)

        self.stdout.write(self.style.SUCCESS(f'دامنه {domain} روی Nginx اعمال شد.'))
        self.stdout.write(f'فایل تنظیمات: {NGINX_CONF}')
        self.stdout.write(f'آدرس پنل: {admin_url()}')
        if not use_ssl:
            self.stdout.write(self.style.WARNING(
                'بدون SSL تنظیم شد. برای امنیت مسیر مخفی پنل، حتما گواهی را فعال کنید.'
            ))


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)
