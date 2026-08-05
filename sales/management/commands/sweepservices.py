"""Check every panel for subscriptions whose traffic has run out.

The bot and the web workers already run this on a timer. This command exists so
it can also be run by hand or from cron on an install where neither is up.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from sales.services.lifecycle import sweep_all


class Command(BaseCommand):
    help = 'Mark subscriptions whose traffic quota is used up, and un-mark renewed ones.'

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

        result = sweep_all()
        self.stdout.write(
            f'{result["checked"]} سرویس روی {result["panels"]} پنل بررسی شد. '
            f'{result["ended"]} مورد حجمش تمام شده بود، {result["revived"]} مورد دوباره فعال شد.'
        )
        if result['failed']:
            self.stdout.write(self.style.WARNING(
                f'{result["failed"]} پنل در دسترس نبود. سرویس‌های روی آن پنل‌ها دست‌نخورده ماندند.'
            ))
