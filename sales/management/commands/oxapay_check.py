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
from sales.services.oxapay import (
    LEGACY_SUCCESS_CODE,
    OXAPAY_INVOICE_URL,
    OXAPAY_LEGACY_INVOICE_URL,
    get_merchant_key,
)


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

        # --- new v1 API ---
        self.stdout.write('  [۱] تست روی API جدید (v1)')
        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(
                    OXAPAY_INVOICE_URL,
                    json={'amount': 1, 'currency': 'USD', 'lifetime': 60,
                          'description': 'vpnshop key check', 'sandbox': True},
                    headers={'merchant_api_key': key, 'Content-Type': 'application/json'},
                )
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.ERROR(f'  ارتباط با OxaPay برقرار نشد: {exc}'))
            self.stdout.write('  اینترنت سرور یا دسترسی به api.oxapay.com را بررسی کنید.')
            return

        try:
            data = response.json()
        except ValueError:
            data = {}
        invoice = (data.get('data') or {}) if isinstance(data, dict) else {}
        pay_url = invoice.get('payment_url') if isinstance(invoice, dict) else None

        self.stdout.write(f'      کد HTTP: {response.status_code}')
        if isinstance(data, dict) and data.get('message'):
            self.stdout.write(f'      پیام: {data["message"]}')

        if pay_url:
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('  ✅ کلید روی API جدید سالم است.'))
            self.stdout.write(f'  لینک پرداخت تستی: {pay_url}')
            self.stdout.write(line)
            return

        # --- legacy API ---
        self.stdout.write('')
        self.stdout.write('  [۲] تست روی API قدیمی (legacy)')
        try:
            with httpx.Client(timeout=30) as client:
                legacy = client.post(
                    OXAPAY_LEGACY_INVOICE_URL,
                    json={'merchant': key, 'amount': 1, 'currency': 'USD', 'lifeTime': 60,
                          'description': 'vpnshop key check'},
                    headers={'Content-Type': 'application/json'},
                )
            legacy_data = legacy.json()
        except Exception as exc:  # noqa: BLE001
            legacy_data = {}
            self.stdout.write(self.style.ERROR(f'      ارتباط ناموفق: {exc}'))

        if isinstance(legacy_data, dict) and legacy_data.get('message'):
            self.stdout.write(f'      پیام: {legacy_data["message"]}')

        if isinstance(legacy_data, dict) and legacy_data.get('result') == LEGACY_SUCCESS_CODE:
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS(
                '  ✅ کلید شما از نوع قدیمی است و روی API قدیمی کار می‌کند.'
            ))
            self.stdout.write(f'  لینک پرداخت تستی: {legacy_data.get("payLink")}')
            self.stdout.write('  ربات این حالت را خودکار تشخیص می‌دهد؛ کاری لازم نیست.')
            self.stdout.write(line)
            return

        self.stdout.write('')
        self.stdout.write(self.style.ERROR('  ❌ کلید روی هیچ‌کدام از دو API کار نکرد.'))

        # Identify what the key actually is, instead of guessing. Each endpoint
        # accepts only its own key type, so a success elsewhere is proof.
        self.stdout.write('')
        self.stdout.write('  [۳] تشخیص نوع کلید')
        # Only the General endpoint is probed. It is a GET and therefore safe.
        # The payout endpoint validates the body before the key, so proving a
        # key is a payout key would mean sending a well-formed payout — which
        # would move real funds if the key turned out to be valid.
        is_general = None
        try:
            with httpx.Client(timeout=20) as client:
                probe = client.get(
                    'https://api.oxapay.com/v1/general/account/balance',
                    headers={'general_api_key': key},
                )
            probe_data = probe.json()
            probe_status = probe_data.get('status') if isinstance(probe_data, dict) else None
            is_general = not (
                probe.status_code in (401, 403)
                or (probe_status and int(probe_status) in (401, 403))
            )
        except Exception:  # noqa: BLE001
            is_general = None

        if is_general:
            self.stdout.write(self.style.WARNING('      این کلید یک «General API Key» است، نه Merchant API Key.'))
            self.stdout.write('      General فقط برای swap و تبدیل ارز است و فاکتور نمی‌سازد.')
            self.stdout.write('      در پنل OxaPay به صفحه Merchant Service بروید و از آنجا کلید بسازید.')
        elif is_general is False:
            self.stdout.write('      این کلید، General API Key هم نیست.')
            self.stdout.write('      یعنی یا کلید ناقص کپی شده، یا مرچنت شما هنوز فعال نیست،')
            self.stdout.write('      یا محدودیت IP دارید و IP این سرور مجاز نیست.')
            self.stdout.write('      (نوع Payout به‌عمد تست نمی‌شود چون تستش یعنی اجرای یک واریز واقعی.)')
        else:
            self.stdout.write('      تشخیص نوع کلید ممکن نشد؛ ارتباط با OxaPay برقرار نشد.')

        try:
            with httpx.Client(timeout=10) as client:
                server_ip = client.get('https://api.ipify.org').text.strip()
            self.stdout.write('')
            self.stdout.write(f'      IP خروجی این سرور: {server_ip}')
            self.stdout.write('      اگر در OxaPay بخش Allowed IP را پر کرده‌اید، همین IP را اضافه کنید.')
        except Exception:  # noqa: BLE001
            pass
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
