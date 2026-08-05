"""Register, remove or inspect the Telegram webhook.

Saving the switch in the panel only records the operator's intent. Telegram has
to be told separately, and that is what this command does, so `vpnshop webhook`
can run it after a deploy or a domain change.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from sales.models import SiteSetting
from sales.services.site_urls import telegram_webhook_url
from sales.services.telegram_webhook import delete_webhook, set_webhook, webhook_status


class Command(BaseCommand):
    help = 'Set, delete or show the Telegram webhook. Default action follows the panel switch.'

    def add_arguments(self, parser):
        parser.add_argument(
            'action',
            nargs='?',
            choices=['auto', 'set', 'delete', 'status'],
            default='auto',
            help='auto: از روی تنظیم پنل تصمیم می‌گیرد. set/delete/status: اجرای مستقیم.',
        )

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

        action = options['action']
        if action == 'auto':
            action = 'set' if SiteSetting.get_solo().telegram_use_webhook else 'delete'

        if action == 'status':
            info = webhook_status()
            self.stdout.write(f'وضعیت: {info["note"]}')
            if info.get('current_url'):
                self.stdout.write(f'آدرس ثبت‌شده در تلگرام: {info["current_url"]}')
            self.stdout.write(f'آدرس مورد انتظار: {telegram_webhook_url() or "-"}')
            if info.get('pending_update_count'):
                self.stdout.write(f'پیام‌های در صف: {info["pending_update_count"]}')
            if info.get('last_error_message'):
                self.stdout.write(self.style.WARNING(f'آخرین خطای تلگرام: {info["last_error_message"]}'))
            return

        ok, message = set_webhook() if action == 'set' else delete_webhook()
        self.stdout.write(self.style.SUCCESS(message) if ok else self.style.ERROR(message))
        if not ok:
            sys.exit(1)
