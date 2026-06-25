from __future__ import annotations

from decimal import Decimal
from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField('زمان ایجاد', auto_now_add=True)
    updated_at = models.DateTimeField('آخرین ویرایش', auto_now=True)

    class Meta:
        abstract = True


class SiteSetting(TimeStampedModel):
    title = models.CharField('عنوان ربات/فروشگاه', max_length=120, default='فروشگاه VPN')
    telegram_bot_token = models.CharField('توکن ربات تلگرام', max_length=255, blank=True)
    support_chat_id = models.CharField('چت آیدی پشتیبانی', max_length=64, blank=True)
    admin_chat_id = models.CharField('چت آیدی مدیر برای اعلان‌ها', max_length=64, blank=True)

    dollar_rate_toman = models.DecimalField('قیمت هر دلار به تومان', max_digits=18, decimal_places=0, default=Decimal('60000'))
    oxapay_merchant_api_key = models.CharField('Merchant API Key درگاه OxaPay', max_length=255, blank=True)
    oxapay_sandbox = models.BooleanField('حالت تست OxaPay', default=True)
    invoice_lifetime_minutes = models.PositiveIntegerField('مهلت پرداخت فاکتور OxaPay / دقیقه', default=60)
    oxapay_fee_paid_by_payer = models.BooleanField('کارمزد OxaPay با پرداخت‌کننده باشد', default=True)

    card_to_card_enabled = models.BooleanField('پرداخت کارت‌به‌کارت فعال باشد', default=False)
    card_to_card_text = models.TextField('متن کارت‌به‌کارت، فقط وقتی فعال باشد به کاربر نشان داده می‌شود', blank=True)

    tutorial_text = models.TextField('متن آموزش اتصال', blank=True, default='آموزش اتصال را از این بخش تنظیم کنید.')
    contact_intro_text = models.TextField('متن بخش ارتباط با ما', blank=True, default='پیام خود را ارسال کنید. پشتیبانی پاسخ شما را بررسی می‌کند.')
    after_purchase_text = models.TextField('متن بعد از خرید موفق', blank=True, default='اشتراک شما با موفقیت ساخته شد.')
    is_shop_active = models.BooleanField('فروشگاه فعال باشد', default=True)

    class Meta:
        verbose_name = 'تنظیمات اصلی ربات'
        verbose_name_plural = 'تنظیمات اصلی ربات'

    def __str__(self) -> str:
        return self.title

    @classmethod
    def get_solo(cls) -> 'SiteSetting':
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class XUIPanel(TimeStampedModel):
    name = models.CharField('نام پنل', max_length=120)
    base_url = models.URLField('آدرس پنل 3x-ui، مثل https://panel.example.com/adminpath')
    api_token = models.CharField('API Token پنل 3x-ui', max_length=500, blank=True)
    verify_ssl = models.BooleanField('بررسی SSL فعال باشد', default=True)
    timeout_seconds = models.PositiveIntegerField('Timeout API / ثانیه', default=20)
    api_base_path = models.CharField('مسیر پایه API', max_length=120, default='/panel/api')
    subscription_base_url = models.URLField('آدرس پایه Subscription، اختیاری', blank=True)
    is_active = models.BooleanField('فعال', default=True)

    class Meta:
        verbose_name = 'پنل سنایی / 3x-ui'
        verbose_name_plural = 'پنل‌های سنایی / 3x-ui'

    def __str__(self) -> str:
        return self.name


class Service(TimeStampedModel):
    name = models.CharField('نام سرویس', max_length=120)
    description = models.TextField('توضیحات سرویس', blank=True)
    panel = models.ForeignKey(XUIPanel, verbose_name='پنل 3x-ui', on_delete=models.PROTECT)
    inbound_id = models.PositiveIntegerField('شناسه Inbound در پنل 3x-ui')
    inbound_remark = models.CharField('نام/Remark اینباند برای یادآوری', max_length=150, blank=True)
    sort_order = models.PositiveIntegerField('ترتیب نمایش', default=10)
    is_active = models.BooleanField('فعال', default=True)

    # 3x-ui API versions differ. These fields keep delivery independent from hard-coded share-link logic.
    config_link_template = models.TextField(
        'قالب لینک کانفیگ، اختیاری',
        blank=True,
        help_text='متغیرها: {uuid}, {email}, {inbound_id}, {panel_base_url}, {subscription_base_url}, {service_name}, {plan_name}',
    )
    subscription_link_template = models.TextField(
        'قالب لینک Subscription، اختیاری',
        blank=True,
        help_text='مثال: https://sub.example.com/sub/{email} یا هر الگویی که در پنل شما استفاده می‌شود.',
    )

    class Meta:
        verbose_name = 'سرویس'
        verbose_name_plural = 'سرویس‌ها'
        ordering = ['sort_order', 'name']

    def __str__(self) -> str:
        return self.name


class Plan(TimeStampedModel):
    service = models.ForeignKey(Service, verbose_name='سرویس', on_delete=models.CASCADE, related_name='plans')
    name = models.CharField('نام اشتراک/پلن', max_length=120)
    description = models.TextField('توضیحات پلن', blank=True)
    price_usd = models.DecimalField('قیمت دلاری', max_digits=10, decimal_places=2)
    duration_days = models.PositiveIntegerField('مدت زمان / روز')
    traffic_gb = models.DecimalField('حجم / گیگابایت؛ ۰ یعنی نامحدود', max_digits=12, decimal_places=2, default=Decimal('0'))
    user_limit = models.PositiveIntegerField('تعداد کاربر / IP Limit', default=1)
    sort_order = models.PositiveIntegerField('ترتیب نمایش', default=10)
    is_active = models.BooleanField('فعال', default=True)

    class Meta:
        verbose_name = 'پلن اشتراک'
        verbose_name_plural = 'پلن‌های اشتراک'
        ordering = ['service__sort_order', 'sort_order', 'price_usd']

    def __str__(self) -> str:
        return f'{self.service.name} - {self.name}'

    def price_toman(self) -> int:
        settings = SiteSetting.get_solo()
        return int(self.price_usd * settings.dollar_rate_toman)


class TelegramUser(TimeStampedModel):
    chat_id = models.BigIntegerField('Chat ID', unique=True)
    username = models.CharField('Username', max_length=150, blank=True)
    first_name = models.CharField('نام', max_length=150, blank=True)
    last_name = models.CharField('نام خانوادگی', max_length=150, blank=True)
    wallet_balance_toman = models.DecimalField('موجودی کیف پول / تومان', max_digits=18, decimal_places=0, default=Decimal('0'))
    state = models.CharField('وضعیت موقت ربات', max_length=120, blank=True)
    temp_data = models.JSONField('داده موقت', default=dict, blank=True)
    is_blocked = models.BooleanField('مسدود', default=False)

    class Meta:
        verbose_name = 'کاربر تلگرام'
        verbose_name_plural = 'کاربران تلگرام'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.chat_id} @{self.username}'.strip()


class Order(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = 'pending', 'در انتظار پرداخت/پردازش'
        PAID = 'paid', 'پرداخت‌شده'
        PROVISIONED = 'provisioned', 'تحویل‌شده'
        FAILED = 'failed', 'ناموفق'
        EXPIRED = 'expired', 'منقضی'
        CANCELLED = 'cancelled', 'لغوشده'

    class Source(models.TextChoices):
        WALLET = 'wallet', 'کیف پول'
        OXAPAY = 'oxapay', 'OxaPay'
        CARD = 'card', 'کارت‌به‌کارت'
        ADMIN = 'admin', 'ثبت دستی مدیر'

    user = models.ForeignKey(TelegramUser, verbose_name='کاربر', on_delete=models.PROTECT, related_name='orders')
    service = models.ForeignKey(Service, verbose_name='سرویس', on_delete=models.PROTECT)
    plan = models.ForeignKey(Plan, verbose_name='پلن', on_delete=models.PROTECT)
    source = models.CharField('روش پرداخت', max_length=20, choices=Source.choices, default=Source.WALLET)
    status = models.CharField('وضعیت', max_length=20, choices=Status.choices, default=Status.PENDING)
    amount_usd = models.DecimalField('مبلغ دلاری', max_digits=10, decimal_places=2)
    amount_toman = models.DecimalField('مبلغ تومان', max_digits=18, decimal_places=0)
    xui_client_uuid = models.CharField('UUID کلاینت', max_length=80, blank=True)
    xui_client_email = models.CharField('Email/شناسه کلاینت در 3x-ui', max_length=150, blank=True)
    expires_at = models.DateTimeField('تاریخ انقضا', null=True, blank=True)
    traffic_bytes = models.BigIntegerField('حجم به بایت؛ ۰ یعنی نامحدود', default=0)
    user_limit = models.PositiveIntegerField('تعداد کاربر', default=1)
    config_link = models.TextField('لینک کانفیگ', blank=True)
    subscription_link = models.TextField('لینک Subscription', blank=True)
    qr_image = models.ImageField('QR Code', upload_to='qrcodes/', blank=True)
    admin_note = models.TextField('یادداشت مدیر', blank=True)

    class Meta:
        verbose_name = 'سفارش/اشتراک'
        verbose_name_plural = 'سفارش‌ها/اشتراک‌ها'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'#{self.pk} {self.user.chat_id} {self.plan.name}'

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.PROVISIONED and (self.expires_at is None or self.expires_at > timezone.now())


class WalletTransaction(TimeStampedModel):
    class Kind(models.TextChoices):
        CREDIT = 'credit', 'افزایش موجودی'
        DEBIT = 'debit', 'کاهش موجودی'
        REFUND = 'refund', 'برگشت وجه'

    user = models.ForeignKey(TelegramUser, verbose_name='کاربر', on_delete=models.PROTECT, related_name='wallet_transactions')
    kind = models.CharField('نوع', max_length=20, choices=Kind.choices)
    amount_toman = models.DecimalField('مبلغ تومان', max_digits=18, decimal_places=0)
    balance_after_toman = models.DecimalField('موجودی بعد از تراکنش', max_digits=18, decimal_places=0)
    order = models.ForeignKey(Order, verbose_name='سفارش مرتبط', on_delete=models.SET_NULL, null=True, blank=True)
    description = models.CharField('توضیح', max_length=255, blank=True)

    class Meta:
        verbose_name = 'تراکنش کیف پول'
        verbose_name_plural = 'تراکنش‌های کیف پول'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.user.chat_id} {self.kind} {self.amount_toman}'


class Payment(TimeStampedModel):
    class Provider(models.TextChoices):
        OXAPAY = 'oxapay', 'OxaPay'
        CARD = 'card', 'کارت‌به‌کارت'
        ADMIN = 'admin', 'ثبت دستی مدیر'

    class Status(models.TextChoices):
        CREATED = 'created', 'ایجاد شده'
        PAYING = 'paying', 'در حال پرداخت'
        PAID = 'paid', 'پرداخت موفق'
        FAILED = 'failed', 'ناموفق'
        EXPIRED = 'expired', 'منقضی'
        CANCELLED = 'cancelled', 'لغوشده'

    class Purpose(models.TextChoices):
        WALLET_TOPUP = 'wallet_topup', 'شارژ کیف پول'
        DIRECT_ORDER = 'direct_order', 'پرداخت مستقیم سفارش'

    user = models.ForeignKey(TelegramUser, verbose_name='کاربر', on_delete=models.PROTECT, related_name='payments')
    provider = models.CharField('درگاه/روش', max_length=20, choices=Provider.choices, default=Provider.OXAPAY)
    purpose = models.CharField('هدف پرداخت', max_length=30, choices=Purpose.choices, default=Purpose.WALLET_TOPUP)
    status = models.CharField('وضعیت', max_length=20, choices=Status.choices, default=Status.CREATED)
    amount_toman = models.DecimalField('مبلغ تومان', max_digits=18, decimal_places=0)
    amount_usd = models.DecimalField('مبلغ دلاری فاکتور', max_digits=12, decimal_places=2, default=Decimal('0'))
    payment_url = models.URLField('لینک پرداخت', blank=True)
    order_id = models.CharField('Order ID داخلی برای درگاه', max_length=80, unique=True)
    track_id = models.CharField('Track ID درگاه', max_length=120, blank=True, db_index=True)
    pending_plan = models.ForeignKey(Plan, verbose_name='پلن قابل خرید بعد از شارژ', on_delete=models.SET_NULL, null=True, blank=True)
    auto_purchase_after_paid = models.BooleanField('بعد از پرداخت خودکار خرید انجام شود', default=False)
    raw_payload = models.JSONField('Payload خام', default=dict, blank=True)

    class Meta:
        verbose_name = 'پرداخت'
        verbose_name_plural = 'پرداخت‌ها'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.order_id} {self.status}'


class SupportMessage(TimeStampedModel):
    user = models.ForeignKey(TelegramUser, verbose_name='کاربر', on_delete=models.PROTECT, related_name='support_messages')
    message_text = models.TextField('متن پیام', blank=True)
    telegram_message_id = models.BigIntegerField('Message ID', null=True, blank=True)
    is_answered = models.BooleanField('پاسخ داده شد', default=False)
    admin_note = models.TextField('یادداشت مدیر', blank=True)

    class Meta:
        verbose_name = 'پیام پشتیبانی'
        verbose_name_plural = 'پیام‌های پشتیبانی'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.user.chat_id} {self.created_at:%Y-%m-%d}'


class CardPaymentRequest(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = 'pending', 'در انتظار بررسی'
        APPROVED = 'approved', 'تایید شده'
        REJECTED = 'rejected', 'رد شده'

    user = models.ForeignKey(TelegramUser, verbose_name='کاربر', on_delete=models.PROTECT)
    amount_toman = models.DecimalField('مبلغ تومان', max_digits=18, decimal_places=0)
    status = models.CharField('وضعیت', max_length=20, choices=Status.choices, default=Status.PENDING)
    receipt_text = models.TextField('متن/شماره پیگیری رسید', blank=True)
    admin_note = models.TextField('یادداشت مدیر', blank=True)

    class Meta:
        verbose_name = 'درخواست کارت‌به‌کارت'
        verbose_name_plural = 'درخواست‌های کارت‌به‌کارت'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.user.chat_id} {self.amount_toman} {self.status}'


class Broadcast(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'پیش‌نویس'
        QUEUED = 'queued', 'در صف ارسال'
        SENT = 'sent', 'ارسال شده'
        FAILED = 'failed', 'ناموفق'

    title = models.CharField('عنوان داخلی', max_length=150)
    text = models.TextField('متن پیام')
    target_chat_id = models.CharField('چت آیدی خاص؛ خالی یعنی همه کاربران', max_length=64, blank=True)
    status = models.CharField('وضعیت', max_length=20, choices=Status.choices, default=Status.DRAFT)
    sent_count = models.PositiveIntegerField('تعداد ارسال موفق', default=0)
    failed_count = models.PositiveIntegerField('تعداد ناموفق', default=0)
    last_error = models.TextField('آخرین خطا', blank=True)

    class Meta:
        verbose_name = 'ارسال پیام گروهی/تکی'
        verbose_name_plural = 'ارسال پیام گروهی/تکی'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return self.title
