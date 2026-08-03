"""Attach a config the user already owns to their Telegram account.

The bot only sells subscriptions, but users often already hold configs bought
elsewhere on the same panels. Matching those by the identifier inside the share
link lets them watch remaining traffic and time here.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.utils import timezone

from sales.models import LinkedService, TelegramUser, XUIPanel
from sales.services.configlink import ConfigLinkError, extract_client_id, extract_label
from sales.services.formatting import fa_digits
from sales.services.xui import XUIClient, XUIError

GB = 1024 ** 3


class LinkingError(RuntimeError):
    pass


def _bytes_text(value: int) -> str:
    if value <= 0:
        return fa_digits('0') + ' گیگابایت'
    gb = value / GB
    if gb < 0.01:
        return fa_digits(f'{value / (1024 * 1024):.1f}') + ' مگابایت'
    return fa_digits(f'{gb:.2f}') + ' گیگابایت'


def _expiry_text(expiry_ms: int) -> str:
    """3x-ui stores expiry as epoch milliseconds; 0 means unlimited."""
    if not expiry_ms:
        return 'نامحدود'
    if expiry_ms < 0:
        # Negative values are 3x-ui's "start on first use" duration, in ms.
        return fa_digits(int(abs(expiry_ms) / 86_400_000)) + ' روز پس از اولین اتصال'
    moment = datetime.fromtimestamp(expiry_ms / 1000, tz=dt_timezone.utc)
    local = timezone.localtime(moment)
    remaining = moment - timezone.now()
    if remaining.total_seconds() <= 0:
        return f'منقضی شده ({fa_digits(local.strftime("%Y-%m-%d"))})'
    return f'{fa_digits(local.strftime("%Y-%m-%d"))} ({fa_digits(remaining.days)} روز باقی‌مانده)'


def usage_text(info: dict, title: str) -> str:
    used = int(info.get('up') or 0) + int(info.get('down') or 0)
    total = int(info.get('total') or 0)

    lines = [f'📊 <b>{title}</b>', '']
    if total > 0:
        remaining = max(total - used, 0)
        percent = min(int(used * 100 / total), 100) if total else 0
        lines.append(f'حجم کل: {_bytes_text(total)}')
        lines.append(f'مصرف‌شده: {_bytes_text(used)} ({fa_digits(percent)}٪)')
        lines.append(f'باقی‌مانده: {_bytes_text(remaining)}')
    else:
        lines.append('حجم: نامحدود')
        lines.append(f'مصرف‌شده: {_bytes_text(used)}')

    lines.append(f'انقضا: {_expiry_text(int(info.get("expiry_time") or 0))}')
    if not info.get('enable', True):
        lines.append('')
        lines.append('⛔️ این سرویس در پنل غیرفعال است.')
    return '\n'.join(lines)


def find_in_panels(identifier: str) -> tuple[XUIPanel, dict] | tuple[None, None]:
    """Search every active panel for the config, returning the first match."""
    errors: list[str] = []
    for panel in XUIPanel.objects.filter(is_active=True):
        try:
            info = XUIClient(panel).find_client_by_identifier(identifier)
        except XUIError as exc:
            errors.append(f'{panel.name}: {exc}')
            continue
        if info:
            return panel, info
    if errors and len(errors) == XUIPanel.objects.filter(is_active=True).count():
        # Every panel failed, so "not found" would be a lie.
        raise LinkingError('ارتباط با پنل برقرار نشد. کمی بعد دوباره تلاش کنید.')
    return None, None


def link_config(user: TelegramUser, raw_text: str) -> tuple[LinkedService, dict]:
    """Validate a pasted config link and attach it to the user."""
    try:
        identifier = extract_client_id(raw_text)
    except ConfigLinkError as exc:
        raise LinkingError(str(exc)) from exc

    existing = LinkedService.objects.filter(user=user, client_uuid=identifier).first()

    panel, info = find_in_panels(identifier)
    if not info:
        raise LinkingError(
            'این کانفیگ روی سرورهای ما پیدا نشد. '
            'مطمئن شوید لینک را کامل و بدون تغییر ارسال کرده‌اید.'
        )

    label = extract_label(raw_text) or info.get('inbound_remark') or ''
    if existing:
        existing.panel = panel
        existing.client_email = info.get('email') or ''
        existing.inbound_id = info.get('inbound_id') or 0
        existing.label = label
        existing.config_link = raw_text.strip()[:2000]
        existing.save()
        return existing, info

    linked = LinkedService.objects.create(
        user=user,
        panel=panel,
        client_uuid=identifier,
        client_email=info.get('email') or '',
        inbound_id=info.get('inbound_id') or 0,
        label=label,
        config_link=raw_text.strip()[:2000],
    )
    return linked, info


def refresh_usage(linked: LinkedService) -> dict:
    """Read live usage for an already-linked config."""
    try:
        info = XUIClient(linked.panel).find_client_by_identifier(linked.client_uuid)
    except XUIError as exc:
        raise LinkingError(f'ارتباط با پنل برقرار نشد: {exc}') from exc
    if not info:
        raise LinkingError('این کانفیگ دیگر روی پنل وجود ندارد. ممکن است حذف شده باشد.')
    return info
