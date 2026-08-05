"""Receive Telegram updates over HTTPS instead of asking for them.

Polling needs its own long-running process that holds an open connection to
Telegram. Webhook mode drops that process: Telegram posts each update to the
web server that already serves the panel and the payment callbacks, so replies
are faster and the server carries one process less.

Switching is the operator's decision, made in the panel, because a webhook only
works on a domain with a valid certificate.
"""

from __future__ import annotations

import threading

from telebot import TeleBot, types

from sales.models import SiteSetting
from sales.services.botcore import build_bot
from sales.services.site_urls import public_base_url, telegram_webhook_url

_bot: TeleBot | None = None
_bot_token: str = ''
_lock = threading.Lock()


def get_webhook_bot() -> TeleBot | None:
    """The bot instance this web worker uses to answer updates.

    Built once per worker and rebuilt if the operator changes the token, since
    handlers are attached at construction time and are not cheap to re-register
    on every request.
    """
    global _bot, _bot_token

    token = (SiteSetting.get_solo().telegram_bot_token or '').strip()
    if not token:
        return None
    with _lock:
        if _bot is None or _bot_token != token:
            # Each gunicorn worker gets its own background watchers. They all
            # claim work through the database, so duplicated sends are already
            # prevented by notified_at and the broadcast status column.
            _bot = build_bot(token, threaded=False)
            _bot_token = token
        return _bot


def handle_update(payload: dict) -> None:
    """Feed one raw update dict to the handlers."""
    bot = get_webhook_bot()
    if bot is None:
        return
    bot.process_new_updates([types.Update.de_json(payload)])


def set_webhook() -> tuple[bool, str]:
    """Register the webhook with Telegram. Returns (ok, message in Persian)."""
    site = SiteSetting.get_solo()
    token = (site.telegram_bot_token or '').strip()
    if not token:
        return False, 'ابتدا توکن ربات را در پنل ثبت کنید.'

    url = telegram_webhook_url()
    if not url:
        return False, 'کلید مخفی وبهوک ساخته نشده است. یک بار تنظیمات را ذخیره کنید.'
    if not url.startswith('https://'):
        return False, (
            f'آدرس فعلی «{public_base_url()}» با https نیست. '
            'تلگرام وبهوک بدون HTTPS را نمی‌پذیرد. ابتدا دامنه و SSL را تنظیم کنید.'
        )

    bot = TeleBot(token)
    try:
        bot.remove_webhook()
        ok = bot.set_webhook(
            url=url,
            secret_token=site.telegram_webhook_secret,
            drop_pending_updates=True,
            max_connections=40,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f'ثبت وبهوک ناموفق بود: {exc}'
    if not ok:
        return False, 'تلگرام ثبت وبهوک را نپذیرفت.'
    return True, f'وبهوک روی این آدرس ثبت شد:\n{url}'


def delete_webhook() -> tuple[bool, str]:
    site = SiteSetting.get_solo()
    token = (site.telegram_bot_token or '').strip()
    if not token:
        return False, 'توکن ربات ثبت نشده است.'
    try:
        TeleBot(token).remove_webhook()
    except Exception as exc:  # noqa: BLE001
        return False, f'حذف وبهوک ناموفق بود: {exc}'
    return True, 'وبهوک حذف شد. حالا حالت Polling می‌تواند کار کند.'


def webhook_status() -> dict:
    """What Telegram currently thinks the webhook is, for showing in the panel."""
    site = SiteSetting.get_solo()
    token = (site.telegram_bot_token or '').strip()
    if not token:
        return {'ok': False, 'note': 'توکن ربات ثبت نشده است.'}
    try:
        info = TeleBot(token).get_webhook_info()
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'note': f'خواندن وضعیت از تلگرام ناموفق بود: {exc}'}

    expected = telegram_webhook_url()
    current = info.url or ''
    return {
        'ok': bool(current) and current == expected,
        'current_url': current,
        'expected_url': expected,
        'pending_update_count': getattr(info, 'pending_update_count', 0),
        'last_error_message': getattr(info, 'last_error_message', '') or '',
        'note': (
            'وبهوک درست ثبت شده است.' if current and current == expected
            else ('وبهوک ثبت نشده است.' if not current else 'وبهوک روی آدرس دیگری ثبت شده است.')
        ),
    }
