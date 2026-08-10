"""Point Nginx at the domain and certificate configured in the panel.

The panel stores the values but cannot install them: writing under /etc/nginx
and reloading the service needs root, which the web process should not rely on.
This command is what `vpnshop domain` runs.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand, CommandError

from sales.models import SiteSetting
from sales.services.site_urls import certificate_status

NGINX_CONF = Path('/etc/nginx/conf.d/vpnshop.conf')
ENV_FILE = Path(os.getenv('VPNSHOP_ENV_FILE', '/etc/vpnshop/vpnshop.env'))


def update_env_file(path: Path, values: dict[str, str]) -> None:
    """Rewrite keys in the runtime env file, keeping everything else intact.

    Django reads ALLOWED_HOSTS and PUBLIC_BASE_URL from here at startup, so a
    domain that is only stored in the panel would still be rejected with a
    400 and the panel would stay unreachable under it.
    """
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    kept = [line for line in lines if line.split('=', 1)[0].strip() not in values]
    kept += [f'{key}={value}' for key, value in values.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(kept).strip() + '\n', encoding='utf-8')
    os.chmod(path, 0o600)

# Only emitted when the panel owns 443. On any other port the panel must not
# touch port 80 at all, because that port belongs to something else — usually
# x-ui, which also needs it free for its own certificate renewals.
REDIRECT_BLOCK = """server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

"""

TEMPLATE = """# ساخته‌شده توسط vpnshop domain — دستی ویرایش نکنید
{redirect}server {{
    listen {https_port} ssl;
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
    listen {https_port};
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


def port_owner(port: int) -> str:
    """Which program is listening on a port, as a short readable name.

    Nginx binding a port that x-ui already holds fails at reload with a message
    most operators never see, so the clash is reported before anything is
    written.
    """
    try:
        out = subprocess.run(
            ['ss', '-lptnH', f'sport = :{port}'], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:  # noqa: BLE001
        return ''
    names = []
    for match in re.finditer(r'users:\(\("([^"]+)"', out):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return '، '.join(names)


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

        https_port = int(site.panel_https_port or 443)
        template = TEMPLATE if use_ssl else PLAIN_TEMPLATE
        config = template.format(
            domain=domain,
            cert=site.ssl_cert_path.strip(),
            key=site.ssl_key_path.strip(),
            port=options['port'],
            https_port=https_port,
            # Grabbing port 80 for a redirect is only right when this panel is
            # also the thing answering on 443.
            redirect=REDIRECT_BLOCK.format(domain=domain) if (use_ssl and https_port == 443) else '',
        )

        if options['print_only']:
            self.stdout.write(config)
            return

        if os.geteuid() != 0:
            raise CommandError('این دستور باید با کاربر root اجرا شود.')

        # Refuse before writing anything, rather than leaving nginx unable to
        # start because the port belongs to someone else.
        owner = port_owner(https_port)
        if owner and 'nginx' not in owner:
            raise CommandError(
                f'پورت {https_port} همین حالا در اختیار «{owner}» است، پس Nginx نمی‌تواند آن را بگیرد. '
                'در پنل، «دامنه و SSL» → «پورت پنل روی اینترنت» را به پورت آزادی مثل ۸۴۴۳ تغییر دهید '
                'و دوباره این دستور را بزنید.'
            )
        if use_ssl and https_port == 443:
            eighty = port_owner(80)
            if eighty and 'nginx' not in eighty:
                raise CommandError(
                    f'پورت ۸۰ در اختیار «{eighty}» است و این تنظیم می‌خواهد آن را برای ریدایرکت بگیرد. '
                    'اگر پورت ۸۰ و ۴۴۳ را برای x-ui می‌خواهید، «پورت پنل روی اینترنت» را ۸۴۴۳ بگذارید.'
                )

        if not shutil_which('nginx'):
            self.stdout.write('Nginx نصب نیست؛ در حال نصب...')
            subprocess.run(['apt-get', 'update', '-qq'], capture_output=True)
            subprocess.run(['apt-get', 'install', '-y', '-qq', 'nginx'], capture_output=True)
            if not shutil_which('nginx'):
                raise CommandError('نصب خودکار Nginx ناموفق بود. دستی نصب کنید: apt install nginx')

        # Django must be told to answer for this host, or every request to the
        # new domain comes back as a 400 no matter how nginx is configured.
        scheme = 'https' if use_ssl else 'http'
        default_port = 443 if scheme == 'https' else 80
        shown_port = '' if https_port == default_port else f':{https_port}'
        hosts = [domain, '127.0.0.1', 'localhost']
        update_env_file(ENV_FILE, {
            'ALLOWED_HOSTS': ','.join(hosts),
            'PUBLIC_BASE_URL': f'{scheme}://{domain}{shown_port}',
        })
        self.stdout.write(f'فایل تنظیمات به‌روز شد: {ENV_FILE}')

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

        # Gunicorn only reads the env file at startup, so the new ALLOWED_HOSTS
        # takes effect on restart. Reload is not enough here.
        restart = subprocess.run(
            ['systemctl', 'restart', 'vpnshop-web'], capture_output=True, text=True
        )
        if restart.returncode == 0:
            self.stdout.write('سرویس پنل ری‌استارت شد.')
        else:
            self.stdout.write(self.style.WARNING(
                'ری‌استارت خودکار سرویس پنل انجام نشد. دستی بزنید: vpnshop restart'
            ))

        self.stdout.write(self.style.SUCCESS(f'دامنه {domain} اعمال شد.'))
        self.stdout.write(f'تنظیمات Nginx: {NGINX_CONF}')
        self.stdout.write(f'آدرس پنل: {scheme}://{domain}{shown_port}/{django_settings.ADMIN_PATH}')
        if https_port != 443:
            self.stdout.write(
                f'پنل روی پورت {https_port} است، پس پورت ۸۰ و ۴۴۳ آزاد ماندند و '
                'می‌توانید آن‌ها را به x-ui بدهید.'
            )
        if https_port not in (443, 80, 88, 8443):
            self.stdout.write(self.style.WARNING(
                f'تلگرام روی پورت {https_port} وبهوک نمی‌فرستد. فقط ۴۴۳، ۸۰، ۸۸ و ۸۴۴۳ را قبول می‌کند. '
                'یا پورت را عوض کنید یا ربات را روی حالت Polling بگذارید.'
            ))
        if not use_ssl:
            self.stdout.write(self.style.WARNING(
                'بدون SSL تنظیم شد. برای امنیت مسیر مخفی پنل، حتما گواهی را فعال کنید.'
            ))


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)
