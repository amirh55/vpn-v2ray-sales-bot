import sys

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from sales.models import Order, SiteSetting, TelegramUser
from sales.services.site_urls import admin_url, oxapay_webhook_url, public_base_url, sms_webhook_url


def _mask(value: str) -> str:
    if not value:
        return 'تنظیم نشده'
    if len(value) <= 10:
        return '*' * len(value)
    return f'{value[:6]}...{value[-4:]}'


class Command(BaseCommand):
    help = 'نمایش آدرس پنل مدیریت و اطلاعات نصب، برای وقتی که آدرس پنل را گم کرده‌اید.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--url-only',
            action='store_true',
            help='فقط آدرس پنل چاپ شود، بدون بقیه اطلاعات.',
        )

    def handle(self, *args, **options):
        # سرورهای تازه نصب اغلب locale ندارند و خروجی فارسی خطا می‌دهد.
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

        panel_url = admin_url()
        base_url = public_base_url()

        if options['url_only']:
            self.stdout.write(panel_url)
            return

        line = '=' * 58
        self.stdout.write(self.style.SUCCESS(line))
        self.stdout.write(self.style.SUCCESS('  اطلاعات پنل مدیریت فروش VPN'))
        self.stdout.write(self.style.SUCCESS(line))

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'  آدرس پنل:  {panel_url}'))
        self.stdout.write(f'  مسیر مخفی: /{settings.ADMIN_PATH}')
        self.stdout.write('')

        User = get_user_model()
        admins = list(User.objects.filter(is_superuser=True).values_list('username', flat=True))
        self.stdout.write('  کاربران مدیر: ' + (', '.join(admins) if admins else 'هیچ کاربری ساخته نشده'))
        self.stdout.write('  فراموشی رمز:  vpnshop passwd <username>')
        self.stdout.write('')

        try:
            site = SiteSetting.objects.first()
        except Exception:
            site = None

        if site:
            self.stdout.write('  عنوان فروشگاه: ' + site.title)
            self.stdout.write('  توکن ربات:     ' + _mask(site.telegram_bot_token))
            self.stdout.write('  کلید OxaPay:   ' + _mask(site.oxapay_merchant_api_key))
            self.stdout.write('  وضعیت فروشگاه: ' + ('فعال' if site.is_shop_active else 'غیرفعال'))
        else:
            self.stdout.write('  تنظیمات اصلی ربات هنوز ثبت نشده است؛ وارد پنل شوید و آن را کامل کنید.')

        self.stdout.write('')
        self.stdout.write(f'  کاربران ربات:  {TelegramUser.objects.count()}')
        self.stdout.write(f'  کل سفارش‌ها:   {Order.objects.count()}')
        self.stdout.write('')

        self.stdout.write('  Webhook درگاه: ' + oxapay_webhook_url())
        if site and site.card_to_card_enabled and sms_webhook_url():
            self.stdout.write('  Webhook پیامک: ' + sms_webhook_url())
        self.stdout.write(f'  مسیر داده‌ها:  {settings.DATABASES["default"]["NAME"]}')
        self.stdout.write('')

        if settings.DEBUG:
            self.stdout.write(self.style.WARNING('  هشدار: DEBUG روشن است. روی سرور واقعی DEBUG=0 بگذارید.'))
        if base_url.startswith('http://') and '127.0.0.1' not in base_url:
            self.stdout.write(self.style.WARNING('  هشدار: پنل روی HTTP است. مسیر مخفی فقط با HTTPS واقعا امن می‌ماند.'))

        self.stdout.write(self.style.SUCCESS(line))
