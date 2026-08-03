"""Diagnose the OxaPay merchant key without going through the bot.

Testing the key with curl is awkward because the key has to be pasted into a
shell, where quoting and stray characters cause failures that look like auth
errors. This command uses the key exactly as the bot stores it.
"""

from __future__ import annotations

import sys

import httpx
from django.core.management.base import BaseCommand

from sales.models import SiteSetting
from sales.services.oxapay import OXAPAY_INVOICE_URL, get_merchant_key


class Command(BaseCommand):
    help = 'بررسی کلید API درگاه OxaPay و نمایش پاسخ واقعی درگاه.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--key',
            default='',
            help='به جای کلید ذخیره‌شده، این کلید را تست کن.',
        )

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

        raw_key = options['key'] or SiteSetting.get_solo().oxapay_merchant_api_key or ''
        key = raw_key.strip()

        line = '=' * 58
        self.stdout.write(line)
        self.stdout.write('  بررسی درگاه OxaPay')
        self.stdout.write(line)

        if not key:
            self.stdout.write(self.style.ERROR(
                '  کلیدی ثبت نشده است. وارد پنل شوید و در «تنظیمات اصلی ربات» '
                'مقدار «Merchant API Key درگاه OxaPay» را پر کنید.'
            ))
            return

        masked = f'{key[:6]}...{key[-4:]}' if len(key) > 12 else '*' * len(key)
        self.stdout.write(f'  کلید: {masked}')
        self.stdout.write(f'  طول کلید: {len(key)} کاراکتر')
        if raw_key != key:
            self.stdout.write(self.style.WARNING(
                '  هشدار: کلید ذخیره‌شده فاصله یا خط جدید اضافی داشت. '
                'آن را در پنل دوباره و بدون فاصله ذخیره کنید.'
            ))
        self.stdout.write('')

        payload = {
            'amount': 1,
            'currency': 'USD',
            'lifetime': 60,
            'description': 'vpnshop key check',
            'sandbox': True,
        }
        headers = {'merchant_api_key': key, 'Content-Type': 'application/json'}

        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(OXAPAY_INVOICE_URL, json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.ERROR(f'  ارتباط با OxaPay برقرار نشد: {exc}'))
            self.stdout.write('  اینترنت سرور یا دسترسی به api.oxapay.com را بررسی کنید.')
            return

        try:
            data = response.json()
        except ValueError:
            data = {}

        message = data.get('message') if isinstance(data, dict) else None
        api_status = data.get('status') if isinstance(data, dict) else None
        invoice = (data.get('data') or {}) if isinstance(data, dict) else {}

        self.stdout.write(f'  کد HTTP: {response.status_code}')
        if api_status:
            self.stdout.write(f'  status در پاسخ: {api_status}')
        if message:
            self.stdout.write(f'  پیام درگاه: {message}')
        self.stdout.write('')

        pay_url = invoice.get('payment_url') if isinstance(invoice, dict) else None
        if pay_url:
            self.stdout.write(self.style.SUCCESS('  ✅ کلید سالم است و فاکتور تستی ساخته شد.'))
            self.stdout.write(f'  لینک پرداخت تستی: {pay_url}')
            self.stdout.write(line)
            return

        self.stdout.write(self.style.ERROR('  ❌ ساخت فاکتور ناموفق بود.'))
        self.stdout.write('')
        self.stdout.write('  رایج‌ترین علت: نوع کلید اشتباه است.')
        self.stdout.write('  - Merchant API Key: برای ساخت فاکتور و دریافت پرداخت. همین لازم است.')
        self.stdout.write('    محل ساخت: صفحه Merchant Service در پنل OxaPay.')
        self.stdout.write('  - General API Key: فقط برای swap و تبدیل ارز. برای فاکتور کار نمی‌کند.')
        self.stdout.write('    محل ساخت: صفحه Account Settings.')
        self.stdout.write('  - Payout API Key: فقط برای واریز به کاربران.')
        self.stdout.write('')
        self.stdout.write('  اگر مطمئنید Merchant API Key است، این‌ها را بررسی کنید:')
        self.stdout.write('  ۱) در OxaPay یک Merchant ساخته باشید و کلید را از خود آن گرفته باشید.')
        self.stdout.write('  ۲) اگر Allowed IP تنظیم کرده‌اید، IP این سرور را اضافه کنید.')
        self.stdout.write('  ۳) حساب و سرویس مرچنت شما فعال باشد.')
        self.stdout.write(line)
