"""Push a sales summary to the operator's Telegram chat.

Meant to be run hourly from cron: the command itself decides whether this is
the hour the operator asked for, so the schedule never has to be rewritten when
they change the time in the panel.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand
from django.utils import timezone

from sales.models import SiteSetting
from sales.services import reports
from sales.services.delivery import get_bot


class Command(BaseCommand):
    help = 'Send the daily or monthly sales summary to the admin chat.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--period',
            choices=['daily', 'monthly', 'auto'],
            default='auto',
            help='auto: بر اساس ساعت و روز تصمیم می‌گیرد. daily/monthly: ارسال مستقیم.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='بدون توجه به ساعت و به تنظیم روشن/خاموش بودن، همین حالا بفرست.',
        )

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

        site = SiteSetting.get_solo()
        force = options['force']
        if not site.daily_report_enabled and not force:
            self.stdout.write('ارسال گزارش در پنل خاموش است. کاری انجام نشد.')
            return

        chat_id = (site.admin_chat_id or site.support_chat_id or '').strip()
        if not chat_id:
            self.stdout.write(self.style.ERROR(
                '«چت آیدی مدیر» در پنل خالی است، پس گزارش جایی ارسال نشد. '
                'در ربات دستور /id را بزنید و عدد را در پنل ثبت کنید.'
            ))
            sys.exit(1)

        period = options['period']
        now = timezone.localtime()
        if period == 'auto':
            if not force and now.hour != int(site.daily_report_hour or 23):
                self.stdout.write(f'ساعت ارسال نرسیده است (تنظیم: {site.daily_report_hour}). کاری انجام نشد.')
                return
            # The monthly figure is only complete once the month is over, so it
            # goes out on the first day and covers the month before it.
            period = 'monthly' if now.day == 1 else 'daily'

        if period == 'monthly':
            start, end, label = reports.named_range('last_month')
        else:
            start, end, label = reports.named_range('today')

        report = reports.build(start, end, label)
        bot = get_bot()
        if bot is None:
            self.stdout.write(self.style.ERROR('توکن ربات ثبت نشده است، پس گزارش ارسال نشد.'))
            sys.exit(1)

        try:
            bot.send_message(chat_id, reports.telegram_summary(report), disable_web_page_preview=True)
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.ERROR(f'ارسال گزارش ناموفق بود: {exc}'))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS(f'گزارش «{label}» برای چت {chat_id} ارسال شد.'))
