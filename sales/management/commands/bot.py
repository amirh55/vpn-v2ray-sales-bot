"""Run the Telegram bot in polling mode.

The bot's actual behaviour lives in sales/services/botcore.py, shared with the
webhook view, so this command only deals with getting a valid token and keeping
the polling loop alive.
"""

from __future__ import annotations

import sys
import time

from django.core.management.base import BaseCommand
from telebot import TeleBot

from sales.services.botcore import build_bot, get_site
from sales.models import SiteSetting


class Command(BaseCommand):
    help = 'Run Telegram VPN sales bot with polling.'

    def handle(self, *args, **options):
        # سرویس systemd معمولا LANG ندارد و خروجی فارسی روی ASCII خطا می‌دهد.
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
        return self.run_bot(*args, **options)

    def wait_for_token(self) -> SiteSetting:
        """Block until the operator saves a bot token in the panel.

        The service usually starts right after install, before the token has
        been entered. Exiting here would make systemd give up after a few
        rapid restarts, so the bot would stay dead even once the token is
        saved. Waiting instead means the bot comes to life on its own.
        """
        warned = False
        while True:
            site = get_site()
            if site.telegram_bot_token:
                return site
            if not warned:
                self.stdout.write(self.style.WARNING(
                    'توکن ربات تلگرام هنوز ثبت نشده است. '
                    'وارد پنل شوید و در «تنظیمات اصلی ربات» آن را ذخیره کنید. '
                    'آدرس پنل را با دستور «vpnshop info» ببینید. '
                    'ربات به محض ثبت توکن خودکار شروع می‌کند.'
                ))
                warned = True
            time.sleep(10)

    def verify_token(self, token: str) -> str:
        """Reject an invalid token with a readable message instead of a traceback.

        A mistyped token is a common setup mistake. Waiting here lets the
        operator paste the correct one in the panel without touching the server.
        """
        warned = False
        while True:
            try:
                TeleBot(token).get_me()
                return token
            except Exception as exc:
                if not warned:
                    self.stdout.write(self.style.ERROR(
                        f'توکن ربات تلگرام معتبر نیست یا سرور به تلگرام دسترسی ندارد: {exc} '
                        'توکن را در پنل، بخش «تنظیمات اصلی ربات» بررسی و اصلاح کنید. '
                        'ربات به محض درست شدن توکن خودکار شروع می‌کند.'
                    ))
                    warned = True
                time.sleep(15)
                token = get_site().telegram_bot_token or token

    def wait_while_webhook_mode(self) -> None:
        """Stay idle while the panel is set to webhook mode.

        Both transports must never run at once: getUpdates returns HTTP 409
        while a webhook is registered. Exiting instead would make systemd give
        up, so switching back to polling in the panel would leave the bot dead.
        """
        warned = False
        while get_site().telegram_use_webhook:
            if not warned:
                self.stdout.write(self.style.WARNING(
                    'حالت Webhook در پنل روشن است، پس Polling اجرا نمی‌شود. '
                    'پیام‌ها از طریق وب‌سرور دریافت می‌شوند. '
                    'اگر می‌خواهید Polling کار کند، در «تنظیمات اصلی ربات» گزینه Webhook را خاموش کنید.'
                ))
                warned = True
            time.sleep(15)

    def run_bot(self, *args, **options):
        self.wait_while_webhook_mode()
        site = self.wait_for_token()
        token = self.verify_token(site.telegram_bot_token)

        bot = build_bot(token)

        # Telegram refuses getUpdates with HTTP 409 while a webhook is set, and
        # a webhook left over from an earlier deployment of the same token
        # would otherwise keep the bot dead forever.
        self.drop_webhook(bot)

        self.stdout.write(self.style.SUCCESS('Telegram bot is running. Press Ctrl+C to stop.'))
        while True:
            try:
                bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
                return
            except Exception as exc:
                self.stdout.write(self.style.ERROR(
                    f'ارتباط ربات با تلگرام قطع شد: {exc} — ۱۵ ثانیه دیگر دوباره تلاش می‌کنم.'
                ))
                time.sleep(15)
                self.wait_while_webhook_mode()
                self.drop_webhook(bot)

    def drop_webhook(self, bot: TeleBot) -> None:
        try:
            bot.remove_webhook()
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f'حذف webhook قبلی ناموفق بود: {exc}'))
