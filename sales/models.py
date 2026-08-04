from __future__ import annotations

import secrets
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

    # Plans carry their own toman and dollar prices. This rate is only used to
    # convert a wallet top-up into dollars for the crypto gateway, and to show
    # customers roughly how much the crypto price saves them.
    dollar_rate_toman = models.DecimalField(
        'نرخ دلار به تومان، برای شارژ کیف پول و نمایش تخفیف',
        max_digits=18,
        decimal_places=0,
        default=Decimal('60000'),
    )
    oxapay_merchant_api_key = models.CharField('Merchant API Key درگاه OxaPay', max_length=255, blank=True)
    oxapay_sandbox = models.BooleanField('حالت تست OxaPay', default=True)
    invoice_lifetime_minutes = models.PositiveIntegerField('مهلت پرداخت فاکتور OxaPay / دقیقه', default=60)
    oxapay_fee_paid_by_payer = models.BooleanField('کارمزد OxaPay با پرداخت‌کننده باشد', default=True)

    public_domain = models.CharField(
        'دامنه پنل و ربات',
        max_length=255,
        blank=True,
        help_text='فقط دامنه، بدون https:// و بدون اسلش. مثل shop.example.com',
    )
    force_https = models.BooleanField(
        'آدرس‌ها با https ساخته شوند',
        default=True,
        help_text='اگر روی دامنه گواهی SSL دارید روشن بماند.',
    )
    ssl_cert_path = models.CharField(
        'مسیر فایل گواهی SSL',
        max_length=500,
        blank=True,
        help_text='مثل /etc/letsencrypt/live/shop.example.com/fullchain.pem',
    )
    ssl_key_path = models.CharField(
        'مسیر فایل کلید خصوصی SSL',
        max_length=500,
        blank=True,
        help_text='مثل /etc/letsencrypt/live/shop.example.com/privkey.pem',
    )

    card_to_card_enabled = models.BooleanField('پرداخت کارت‌به‌کارت فعال باشد', default=False)
    card_number = models.CharField(
        'شماره کارت',
        max_length=32,
        blank=True,
        help_text='۱۶ رقم، با یا بدون فاصله. به کاربر به صورت قابل کپی نمایش داده می‌شود.',
    )
    card_holder_name = models.CharField('نام صاحب کارت', max_length=120, blank=True)
    card_bank_name = models.CharField('نام بانک', max_length=120, blank=True)
    card_to_card_text = models.TextField('توضیح اضافه کارت‌به‌کارت، اختیاری', blank=True)
    card_invoice_minutes = models.PositiveIntegerField('مهلت پرداخت کارت‌به‌کارت / دقیقه', default=30)
    card_auto_confirm_enabled = models.BooleanField('تایید خودکار کارت‌به‌کارت با پیامک بانکی', default=False)
    sms_webhook_secret = models.CharField(
        'کلید مخفی وبهوک پیامک',
        max_length=120,
        blank=True,
        help_text='در آدرس وبهوک اپ پیامک‌فرست قرار می‌گیرد. خالی بگذارید تا خودکار ساخته شود.',
    )
    sms_allowed_senders = models.CharField(
        'شماره‌های مجاز بانک',
        max_length=500,
        blank=True,
        help_text='با کاما جدا کنید، مثل: 200080,7575,9830000. خالی یعنی هر فرستنده‌ای پذیرفته می‌شود.',
    )

    tutorial_text = models.TextField('متن آموزش اتصال', blank=True, default='آموزش اتصال را از این بخش تنظیم کنید.')
    contact_intro_text = models.TextField('متن بخش ارتباط با ما', blank=True, default='پیام خود را ارسال کنید. پشتیبانی پاسخ شما را بررسی می‌کند.')
    faq_intro_text = models.TextField(
        'متن بالای بخش سوالات متداول',
        blank=True,
        default='سوال خود را از لیست زیر انتخاب کنید تا پاسخ آن را ببینید.',
    )
    after_purchase_text = models.TextField('متن بعد از خرید موفق', blank=True, default='اشتراک شما با موفقیت ساخته شد.')
    is_shop_active = models.BooleanField('فروشگاه فعال باشد', default=True)

    class Meta:
        verbose_name = 'تنظیمات اصلی ربات'
        verbose_name_plural = 'تنظیمات اصلی ربات'

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        # The operator never needs to invent this; an empty field means
        # "generate one", which is what the field's help text promises.
        if not self.sms_webhook_secret:
            self.sms_webhook_secret = secrets.token_urlsafe(24)
            update_fields = kwargs.get('update_fields')
            if update_fields is not None and 'sms_webhook_secret' not in update_fields:
                kwargs['update_fields'] = list(update_fields) + ['sms_webhook_secret']
        super().save(*args, **kwargs)

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
    # Two independent prices. The dollar price is set lower on purpose to steer
    # customers towards crypto, so it is never derived from the toman price.
    price_toman = models.DecimalField('قیمت تومانی، برای کارت‌به‌کارت و کیف پول', max_digits=18, decimal_places=0, default=Decimal('0'))
    price_usd = models.DecimalField('قیمت دلاری، برای پرداخت کریپتو', max_digits=10, decimal_places=2)
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

    def crypto_equivalent_toman(self) -> int:
        """Roughly what the dollar price costs in toman, for showing the saving."""
        settings = SiteSetting.get_solo()
        return int(self.price_usd * settings.dollar_rate_toman)

    def crypto_saving_toman(self) -> int:
        """How much cheaper paying in crypto is. Zero when it is not cheaper."""
        return max(0, int(self.price_toman) - self.crypto_equivalent_toman())

    def crypto_saving_percent(self) -> int:
        base = int(self.price_toman)
        if base <= 0:
            return 0
        return int(round(self.crypto_saving_toman() * 100 / base))


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
    # amount_toman is the exact figure the customer must transfer. It carries a
    # small random tail so an incoming bank SMS identifies one invoice only.
    amount_toman = models.DecimalField('مبلغ دقیق واریز / تومان', max_digits=18, decimal_places=0)
    base_amount_toman = models.DecimalField('مبلغ درخواستی اولیه / تومان', max_digits=18, decimal_places=0, default=Decimal('0'))
    expires_at = models.DateTimeField('انقضای فاکتور', null=True, blank=True)
    status = models.CharField('وضعیت', max_length=20, choices=Status.choices, default=Status.PENDING)
    receipt_text = models.TextField('متن/شماره پیگیری رسید', blank=True)
    receipt_file_id = models.CharField('شناسه تصویر رسید در تلگرام', max_length=255, blank=True)
    auto_approved = models.BooleanField('تایید خودکار با پیامک', default=False)
    # Set when the customer is buying a plan rather than topping up, so the
    # service is delivered as soon as the transfer is recognised.
    pending_plan = models.ForeignKey(
        'Plan',
        verbose_name='پلن در انتظار خرید',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    auto_purchase_after_paid = models.BooleanField('بعد از تایید، سرویس خودکار ساخته شود', default=False)
    created_order = models.ForeignKey(
        'Order',
        verbose_name='سفارش ساخته‌شده',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='card_requests',
    )
    # The webhook settles payments in the web process, which has no bot loop.
    # This marks whether the customer has already been told.
    notified_at = models.DateTimeField('زمان اطلاع‌رسانی به کاربر', null=True, blank=True)
    admin_note = models.TextField('یادداشت مدیر', blank=True)

    class Meta:
        verbose_name = 'درخواست کارت‌به‌کارت'
        verbose_name_plural = 'درخواست‌های کارت‌به‌کارت'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.user.chat_id} {self.amount_toman} {self.status}'

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= timezone.now())


class BankSms(TimeStampedModel):
    """A bank SMS forwarded from the operator's phone.

    Kept even when it matches nothing, so unmatched deposits can be found by
    hand and so a misbehaving forwarder is visible.
    """

    sender = models.CharField('شماره فرستنده', max_length=64, blank=True)
    raw_text = models.TextField('متن پیامک')
    parsed_amount_toman = models.DecimalField('مبلغ استخراج‌شده / تومان', max_digits=18, decimal_places=0, null=True, blank=True)
    matched_request = models.ForeignKey(
        CardPaymentRequest,
        verbose_name='درخواست منطبق',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bank_messages',
    )
    note = models.CharField('نتیجه بررسی', max_length=255, blank=True)

    class Meta:
        verbose_name = 'پیامک بانکی'
        verbose_name_plural = 'پیامک‌های بانکی'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.sender} {self.parsed_amount_toman or "?"}'


class LinkedService(TimeStampedModel):
    """A config the user bought elsewhere, matched to a panel client by UUID.

    Orders cover subscriptions sold by this bot. This model covers configs the
    user already owns, so they can watch remaining traffic and time here
    without buying again. Usage is always read live from the panel; nothing
    about consumption is cached on this row.
    """

    user = models.ForeignKey(TelegramUser, verbose_name='کاربر', on_delete=models.CASCADE, related_name='linked_services')
    panel = models.ForeignKey(XUIPanel, verbose_name='پنل 3x-ui', on_delete=models.CASCADE)
    client_uuid = models.CharField('شناسه کلاینت / UUID', max_length=120)
    client_email = models.CharField('Email/شناسه کلاینت در پنل', max_length=150, blank=True)
    inbound_id = models.PositiveIntegerField('شناسه Inbound', default=0)
    label = models.CharField('نام دلخواه', max_length=150, blank=True)
    config_link = models.TextField('لینک کانفیگ ارسالی کاربر', blank=True)

    class Meta:
        verbose_name = 'سرویس افزوده‌شده کاربر'
        verbose_name_plural = 'سرویس‌های افزوده‌شده کاربران'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'client_uuid'], name='unique_user_linked_client'),
        ]

    def __str__(self) -> str:
        return f'{self.user.chat_id} {self.client_email or self.client_uuid}'

    def display_name(self) -> str:
        return self.label or self.client_email or self.client_uuid[:12]


class FaqItem(TimeStampedModel):
    """One question the customer can tap to read its answer.

    The operator writes these in the panel, so the bot's help section can be
    changed without touching code.
    """

    question = models.CharField(
        'سوال',
        max_length=100,
        help_text='روی دکمه نمایش داده می‌شود، پس کوتاه بنویسید. حداکثر ۱۰۰ کاراکتر.',
    )
    answer = models.TextField('پاسخ', help_text='می‌توانید از تگ‌های <b> و <code> و لینک استفاده کنید.')
    sort_order = models.PositiveIntegerField('ترتیب نمایش', default=10)
    is_active = models.BooleanField('فعال', default=True)

    class Meta:
        verbose_name = 'سوال متداول'
        verbose_name_plural = 'سوالات متداول'
        ordering = ['sort_order', 'pk']

    def __str__(self) -> str:
        return self.question


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
