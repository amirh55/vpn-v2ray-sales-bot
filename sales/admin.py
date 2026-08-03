import types

from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Sum
from django.utils import timezone
from django.utils.html import format_html

from unfold.admin import ModelAdmin, TabularInline

from .services.cardpay import approve_request

from .models import (
    BankSms,
    Broadcast,
    CardPaymentRequest,
    LinkedService,
    Order,
    Payment,
    Plan,
    Service,
    SiteSetting,
    SupportMessage,
    TelegramUser,
    WalletTransaction,
    XUIPanel,
)

admin.site.site_header = 'پنل مدیریت فروش کانفیگ VPN'
admin.site.site_title = 'فروشگاه VPN'
admin.site.index_title = 'مدیریت ربات، فروش، کیف پول و پنل 3x-ui'

_default_admin_index = admin.site.__class__.index


def _dashboard_index(self, request, extra_context=None):
    now = timezone.now()
    month_start = now.date().replace(day=1)
    revenue_this_month = Order.objects.filter(
        status__in=[Order.Status.PAID, Order.Status.PROVISIONED],
        created_at__date__gte=month_start,
    ).aggregate(total=Sum('amount_toman'))['total'] or 0
    wallet_total = TelegramUser.objects.aggregate(total=Sum('wallet_balance_toman'))['total'] or 0

    extra_context = extra_context or {}
    extra_context['kpi_cards'] = [
        {'title': 'کاربران ربات', 'value': TelegramUser.objects.count(), 'icon': 'group'},
        {
            'title': 'اشتراک‌های فعال',
            'value': Order.objects.filter(status=Order.Status.PROVISIONED, expires_at__gt=now).count(),
            'icon': 'vpn_lock',
        },
        {'title': 'درآمد این ماه (تومان)', 'value': f'{int(revenue_this_month):,}', 'icon': 'payments'},
        {
            'title': 'درخواست کارت‌به‌کارت در انتظار',
            'value': CardPaymentRequest.objects.filter(status=CardPaymentRequest.Status.PENDING).count(),
            'icon': 'credit_card',
        },
        {
            'title': 'پیام پشتیبانی بی‌پاسخ',
            'value': SupportMessage.objects.filter(is_answered=False).count(),
            'icon': 'support_agent',
        },
        {'title': 'مجموع موجودی کیف‌پول کاربران (تومان)', 'value': f'{int(wallet_total):,}', 'icon': 'account_balance_wallet'},
    ]
    return _default_admin_index(self, request, extra_context)


admin.site.index = types.MethodType(_dashboard_index, admin.site)


@admin.register(SiteSetting)
class SiteSettingAdmin(ModelAdmin):
    fieldsets = (
        ('ربات و پشتیبانی', {'fields': ('title', 'telegram_bot_token', 'support_chat_id', 'admin_chat_id', 'is_shop_active')}),
        ('قیمت و درگاه', {'fields': ('dollar_rate_toman', 'oxapay_merchant_api_key', 'oxapay_sandbox', 'invoice_lifetime_minutes', 'oxapay_fee_paid_by_payer')}),
        ('کارت‌به‌کارت', {
            'fields': (
                'card_to_card_enabled', 'card_to_card_text', 'card_invoice_minutes',
                'card_auto_confirm_enabled', 'sms_webhook_secret', 'sms_allowed_senders',
                'sms_webhook_url',
            ),
        }),
        ('متن‌ها', {'fields': ('tutorial_text', 'contact_intro_text', 'after_purchase_text')}),
    )

    readonly_fields = ('sms_webhook_url',)

    def has_add_permission(self, request):
        return not SiteSetting.objects.exists()

    @admin.display(description='آدرس وبهوک پیامک')
    def sms_webhook_url(self, obj):
        if not obj or not obj.pk:
            return 'بعد از ذخیره نمایش داده می‌شود'
        if not obj.sms_webhook_secret:
            return 'ابتدا کلید مخفی را ذخیره کنید یا خالی بگذارید تا خودکار ساخته شود'
        url = f'{settings.PUBLIC_BASE_URL}/api/payments/sms/webhook/?secret={obj.sms_webhook_secret}'
        return format_html(
            'این آدرس را در اپ پیامک‌فرست گوشی وارد کنید:<br>'
            '<code style="user-select:all;word-break:break-all;">{}</code>',
            url,
        )


@admin.register(XUIPanel)
class XUIPanelAdmin(ModelAdmin):
    list_display = ('name', 'base_url', 'api_base_path', 'is_active', 'updated_at')
    list_filter = ('is_active', 'verify_ssl')
    search_fields = ('name', 'base_url')


class PlanInline(TabularInline):
    model = Plan
    extra = 1
    fields = ('name', 'price_usd', 'price_toman_preview', 'duration_days', 'traffic_gb', 'user_limit', 'sort_order', 'is_active')
    readonly_fields = ('price_toman_preview',)

    @admin.display(description='قیمت تومان')
    def price_toman_preview(self, obj):
        if not obj or obj.pk is None:
            return 'بعد از ذخیره محاسبه می‌شود'
        return f'{obj.price_toman():,} تومان'


@admin.register(Service)
class ServiceAdmin(ModelAdmin):
    list_display = ('name', 'panel', 'inbound_id', 'inbound_remark', 'sort_order', 'is_active')
    list_filter = ('is_active', 'panel')
    search_fields = ('name', 'description', 'inbound_remark')
    inlines = [PlanInline]
    fieldsets = (
        ('اطلاعات سرویس', {'fields': ('name', 'description', 'panel', 'inbound_id', 'inbound_remark', 'sort_order', 'is_active')}),
        ('قالب تحویل لینک‌ها', {'fields': ('config_link_template', 'subscription_link_template')}),
    )


@admin.register(Plan)
class PlanAdmin(ModelAdmin):
    list_display = ('name', 'service', 'price_usd', 'price_toman_col', 'duration_days', 'traffic_gb', 'user_limit', 'is_active')
    list_filter = ('is_active', 'service')
    search_fields = ('name', 'description', 'service__name')
    list_editable = ('price_usd', 'duration_days', 'traffic_gb', 'user_limit', 'is_active')

    @admin.display(description='قیمت تومان')
    def price_toman_col(self, obj):
        return f'{obj.price_toman():,}'


@admin.register(TelegramUser)
class TelegramUserAdmin(ModelAdmin):
    list_display = ('chat_id', 'username', 'full_name', 'wallet_balance_toman', 'is_blocked', 'created_at')
    list_filter = ('is_blocked',)
    search_fields = ('chat_id', 'username', 'first_name', 'last_name')
    readonly_fields = ('created_at', 'updated_at')

    @admin.display(description='نام')
    def full_name(self, obj):
        return f'{obj.first_name} {obj.last_name}'.strip()


@admin.register(Order)
class OrderAdmin(ModelAdmin):
    list_display = ('id', 'user', 'service', 'plan', 'status', 'source', 'amount_toman', 'expires_at', 'created_at')
    list_filter = ('status', 'source', 'service', 'plan')
    search_fields = ('id', 'user__chat_id', 'user__username', 'xui_client_email', 'xui_client_uuid')
    readonly_fields = ('config_link_click', 'subscription_link_click', 'qr_preview', 'created_at', 'updated_at')
    fieldsets = (
        ('سفارش', {'fields': ('user', 'service', 'plan', 'source', 'status', 'amount_usd', 'amount_toman', 'admin_note')}),
        ('تحویل 3x-ui', {'fields': ('xui_client_uuid', 'xui_client_email', 'expires_at', 'traffic_bytes', 'user_limit')}),
        ('لینک‌ها', {'fields': ('config_link', 'subscription_link', 'config_link_click', 'subscription_link_click', 'qr_image', 'qr_preview')}),
        ('زمان‌ها', {'fields': ('created_at', 'updated_at')}),
    )

    @admin.display(description='لینک کانفیگ')
    def config_link_click(self, obj):
        if obj.config_link:
            return format_html('<a href="{}" target="_blank">باز کردن</a>', obj.config_link)
        return '-'

    @admin.display(description='لینک Subscription')
    def subscription_link_click(self, obj):
        if obj.subscription_link:
            return format_html('<a href="{}" target="_blank">باز کردن</a>', obj.subscription_link)
        return '-'

    @admin.display(description='پیش‌نمایش QR')
    def qr_preview(self, obj):
        if obj.qr_image:
            return format_html('<img src="{}" style="max-width:180px;border:1px solid #ddd;border-radius:12px;" />', obj.qr_image.url)
        return '-'


@admin.register(WalletTransaction)
class WalletTransactionAdmin(ModelAdmin):
    list_display = ('user', 'kind', 'amount_toman', 'balance_after_toman', 'order', 'created_at')
    list_filter = ('kind',)
    search_fields = ('user__chat_id', 'description')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Payment)
class PaymentAdmin(ModelAdmin):
    list_display = ('order_id', 'user', 'provider', 'purpose', 'status', 'amount_toman', 'amount_usd', 'track_id', 'created_at')
    list_filter = ('provider', 'purpose', 'status')
    search_fields = ('order_id', 'track_id', 'user__chat_id', 'user__username')
    readonly_fields = ('raw_payload', 'created_at', 'updated_at')


@admin.register(SupportMessage)
class SupportMessageAdmin(ModelAdmin):
    list_display = ('user', 'short_text', 'is_answered', 'created_at')
    list_filter = ('is_answered',)
    search_fields = ('user__chat_id', 'user__username', 'message_text')

    @admin.display(description='متن')
    def short_text(self, obj):
        return obj.message_text[:80]


@admin.action(description='تایید و شارژ کیف پول')
def approve_card_requests(modeladmin, request, queryset):
    # Expired invoices are approved too: the customer may have paid late or the
    # bank SMS may never have arrived, and the operator has seen the receipt.
    count = skipped = 0
    for req in queryset.filter(status=CardPaymentRequest.Status.PENDING):
        if approve_request(req, auto=False, note=f'تایید دستی توسط {request.user.username}'):
            count += 1
        else:
            skipped += 1
    text = f'{count} درخواست تایید و کیف پول شارژ شد. کاربر خودکار مطلع می‌شود.'
    if skipped:
        text += f' {skipped} مورد قبلاً تایید شده بود.'
    messages.success(request, text)


@admin.register(CardPaymentRequest)
class CardPaymentRequestAdmin(ModelAdmin):
    list_display = (
        'id', 'user', 'amount_toman', 'status', 'auto_approved',
        'expiry_state', 'has_receipt', 'created_at',
    )
    list_filter = ('status', 'auto_approved')
    search_fields = ('user__chat_id', 'user__username', 'receipt_text', 'amount_toman')
    readonly_fields = ('base_amount_toman', 'expires_at', 'auto_approved', 'notified_at', 'created_at', 'updated_at')
    actions = [approve_card_requests]

    @admin.display(description='مهلت', boolean=False)
    def expiry_state(self, obj):
        if not obj.expires_at:
            return '-'
        if obj.status == CardPaymentRequest.Status.APPROVED:
            return 'تایید شده'
        return 'منقضی' if obj.is_expired else 'باز'

    @admin.display(description='رسید', boolean=True)
    def has_receipt(self, obj):
        return bool(obj.receipt_text or obj.receipt_file_id)


@admin.register(BankSms)
class BankSmsAdmin(ModelAdmin):
    list_display = ('created_at', 'sender', 'parsed_amount_toman', 'matched_request', 'note')
    list_filter = ('note',)
    search_fields = ('sender', 'raw_text', 'note')
    readonly_fields = ('sender', 'raw_text', 'parsed_amount_toman', 'matched_request', 'note', 'created_at', 'updated_at')

    def has_add_permission(self, request):
        return False


@admin.register(LinkedService)
class LinkedServiceAdmin(ModelAdmin):
    list_display = ('user', 'label', 'client_email', 'panel', 'inbound_id', 'created_at')
    list_filter = ('panel',)
    search_fields = ('user__chat_id', 'user__username', 'client_uuid', 'client_email', 'label')
    readonly_fields = ('created_at', 'updated_at')


@admin.action(description='قرار دادن در صف ارسال')
def queue_broadcasts(modeladmin, request, queryset):
    updated = queryset.update(status=Broadcast.Status.QUEUED)
    messages.success(request, f'{updated} پیام در صف ارسال قرار گرفت. تا وقتی دستور bot در حال اجرا باشد ارسال می‌شود.')


@admin.register(Broadcast)
class BroadcastAdmin(ModelAdmin):
    list_display = ('title', 'target_chat_id', 'status', 'sent_count', 'failed_count', 'updated_at')
    list_filter = ('status',)
    search_fields = ('title', 'text', 'target_chat_id')
    actions = [queue_broadcasts]
