from pathlib import Path
import os
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Runtime/persistent configuration:
# - Project files can be updated from GitHub safely.
# - Secrets, SQLite DB, media, and operational data should live outside Git.
# - Override with VPNSHOP_RUNTIME_DIR if you want a different location.
RUNTIME_DIR = Path(os.getenv('VPNSHOP_RUNTIME_DIR', '/var/lib/vpnshop'))

# Loading order: project defaults first, then server-level files override them.
load_dotenv(BASE_DIR / '.env', override=False)
load_dotenv(Path('/etc/vpnshop/vpnshop.env'), override=True)
load_dotenv(RUNTIME_DIR / '.env', override=True)

SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-change-me')
DEBUG = os.getenv('DEBUG', '1') == '1'
ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '127.0.0.1,localhost').split(',') if h.strip()]
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')

# The admin panel lives on a secret, randomly generated path so it is not
# discoverable by scanners hitting /admin/. Installer writes ADMIN_PATH to .env.
_admin_path = os.getenv('ADMIN_PATH', 'admin').strip().strip('/')
ADMIN_PATH = f'{_admin_path}/' if _admin_path else 'admin/'

INSTALLED_APPS = [
    'unfold',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'sales',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'vpnshop.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'vpnshop.wsgi.application'
ASGI_APPLICATION = 'vpnshop.asgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.getenv('SQLITE_DB_PATH', str(RUNTIME_DIR / 'db.sqlite3')),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'fa-ir'
TIME_ZONE = os.getenv('TIME_ZONE', 'Asia/Tehran')
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = Path(os.getenv('STATIC_ROOT', str(BASE_DIR / 'staticfiles')))
MEDIA_URL = '/media/'
MEDIA_ROOT = Path(os.getenv('MEDIA_ROOT', str(RUNTIME_DIR / 'media')))
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_REDIRECT_URL = f'/{ADMIN_PATH}'
_csrf_public = os.getenv('PUBLIC_BASE_URL')
CSRF_TRUSTED_ORIGINS = [u for u in [_csrf_public] if u and u.startswith('http')]

# Production hints: set DEBUG=0, use PostgreSQL, HTTPS, and a process manager.

UNFOLD = {
    'SITE_TITLE': 'فروشگاه VPN',
    'SITE_HEADER': 'پنل مدیریت فروش VPN',
    'SITE_SUBHEADER': 'ربات، سرویس‌ها، پرداخت و کاربران',
    'SITE_SYMBOL': 'bolt',
    'SHOW_HISTORY': True,
    'SHOW_VIEW_ON_SITE': False,
    'COLORS': {
        'primary': {
            '50': '239 246 255', '100': '219 234 254', '200': '191 219 254',
            '300': '147 197 253', '400': '96 165 250', '500': '59 130 246',
            '600': '37 99 235', '700': '29 78 216', '800': '30 64 175',
            '900': '30 58 138', '950': '23 37 84',
        },
    },
    'SIDEBAR': {
        'show_search': True,
        'show_all_applications': False,
        'navigation': [
            {
                'title': _('ربات و تنظیمات'),
                'separator': True,
                'items': [
                    {
                        'title': _('تنظیمات اصلی ربات'),
                        'icon': 'settings',
                        'link': reverse_lazy('admin:sales_sitesetting_changelist'),
                    },
                    {
                        'title': _('پنل‌های 3x-ui'),
                        'icon': 'dns',
                        'link': reverse_lazy('admin:sales_xuipanel_changelist'),
                    },
                    {
                        'title': _('پشتیبان‌گیری و بازگردانی'),
                        'icon': 'backup',
                        'link': reverse_lazy('admin:sales_backup'),
                    },
                ],
            },
            {
                'title': _('فروش'),
                'separator': True,
                'items': [
                    {
                        'title': _('سرویس‌ها'),
                        'icon': 'vpn_lock',
                        'link': reverse_lazy('admin:sales_service_changelist'),
                    },
                    {
                        'title': _('پلن‌های اشتراک'),
                        'icon': 'sell',
                        'link': reverse_lazy('admin:sales_plan_changelist'),
                    },
                    {
                        'title': _('سفارش‌ها/اشتراک‌ها'),
                        'icon': 'shopping_cart',
                        'link': reverse_lazy('admin:sales_order_changelist'),
                    },
                ],
            },
            {
                'title': _('مالی'),
                'separator': True,
                'items': [
                    {
                        'title': _('پرداخت‌ها'),
                        'icon': 'payments',
                        'link': reverse_lazy('admin:sales_payment_changelist'),
                    },
                    {
                        'title': _('تراکنش‌های کیف پول'),
                        'icon': 'account_balance_wallet',
                        'link': reverse_lazy('admin:sales_wallettransaction_changelist'),
                    },
                    {
                        'title': _('درخواست‌های کارت‌به‌کارت'),
                        'icon': 'credit_card',
                        'link': reverse_lazy('admin:sales_cardpaymentrequest_changelist'),
                    },
                ],
            },
            {
                'title': _('کاربران و پیام‌رسانی'),
                'separator': True,
                'items': [
                    {
                        'title': _('کاربران تلگرام'),
                        'icon': 'group',
                        'link': reverse_lazy('admin:sales_telegramuser_changelist'),
                    },
                    {
                        'title': _('پیام‌های پشتیبانی'),
                        'icon': 'support_agent',
                        'link': reverse_lazy('admin:sales_supportmessage_changelist'),
                    },
                    {
                        'title': _('سوالات متداول'),
                        'icon': 'help',
                        'link': reverse_lazy('admin:sales_faqitem_changelist'),
                    },
                    {
                        'title': _('ارسال پیام گروهی/تکی'),
                        'icon': 'campaign',
                        'link': reverse_lazy('admin:sales_broadcast_changelist'),
                    },
                ],
            },
        ],
    },
}
