"""Work out the public address of this installation.

The domain lives in the panel so the operator can change it without editing
files on the server, and falls back to the PUBLIC_BASE_URL written by the
installer. Everything that hands an address to an outside system — payment
callbacks, the SMS webhook — goes through here, so one change reaches them all.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime

from django.conf import settings as django_settings

from sales.models import SiteSetting


def public_base_url() -> str:
    site = SiteSetting.get_solo()
    domain = (site.public_domain or '').strip().strip('/')
    if not domain:
        return django_settings.PUBLIC_BASE_URL.rstrip('/')
    # Tolerate a full URL pasted into the domain field.
    if domain.startswith(('http://', 'https://')):
        return domain.rstrip('/')
    scheme = 'https' if site.force_https else 'http'
    return f'{scheme}://{domain}'


def admin_url() -> str:
    return f'{public_base_url()}/{django_settings.ADMIN_PATH}'


def oxapay_webhook_url() -> str:
    return f'{public_base_url()}/api/payments/oxapay/webhook/'


def sms_webhook_url() -> str:
    site = SiteSetting.get_solo()
    if not site.sms_webhook_secret:
        return ''
    return f'{public_base_url()}/api/payments/sms/webhook/?secret={site.sms_webhook_secret}'


def certificate_expiry(cert_path: str) -> datetime | None:
    """Read the certificate's expiry with openssl, if it is available."""
    try:
        out = subprocess.run(
            ['openssl', 'x509', '-enddate', '-noout', '-in', cert_path],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return None
    if '=' not in out:
        return None
    try:
        return datetime.strptime(out.split('=', 1)[1].strip(), '%b %d %H:%M:%S %Y %Z')
    except ValueError:
        return None


def domain_is_live(site: SiteSetting | None = None) -> bool:
    """Whether Django will actually answer for the configured domain.

    ALLOWED_HOSTS is read from the env file at startup, so a domain saved in
    the panel does nothing until `vpnshop domain` writes it there and restarts
    the web service. Without this check the panel would look configured while
    every request to the domain came back as a 400.
    """
    site = site or SiteSetting.get_solo()
    domain = (site.public_domain or '').strip().strip('/')
    if not domain:
        return True
    if domain.startswith(('http://', 'https://')):
        domain = domain.split('://', 1)[1].strip('/')
    allowed = django_settings.ALLOWED_HOSTS
    return '*' in allowed or domain in allowed


def certificate_status(site: SiteSetting | None = None) -> list[dict]:
    """Report on each configured certificate file, for display in the panel."""
    site = site or SiteSetting.get_solo()
    rows = []
    for label, path in (('گواهی', site.ssl_cert_path), ('کلید خصوصی', site.ssl_key_path)):
        path = (path or '').strip()
        if not path:
            rows.append({'label': label, 'path': '', 'ok': False, 'note': 'وارد نشده'})
            continue
        if not os.path.isfile(path):
            rows.append({'label': label, 'path': path, 'ok': False, 'note': 'فایل پیدا نشد'})
            continue
        if not os.access(path, os.R_OK):
            rows.append({'label': label, 'path': path, 'ok': False, 'note': 'فایل قابل خواندن نیست'})
            continue
        note = 'موجود و قابل خواندن'
        if label == 'گواهی':
            expiry = certificate_expiry(path)
            if expiry:
                days = (expiry - datetime.utcnow()).days
                note += f' — انقضا: {expiry:%Y-%m-%d}'
                note += f' ({days} روز مانده)' if days >= 0 else ' (منقضی شده)'
        rows.append({'label': label, 'path': path, 'ok': True, 'note': note})
    return rows
