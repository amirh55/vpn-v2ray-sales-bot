from django.contrib import admin, messages
from django.utils.html import format_html

from .models import (
    Broadcast,
    CardPaymentRequest,
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


@admin.register(SiteSetting)
class SiteSettingAdmin(admin.ModelAdmin):
    fieldsets = (
        ('ربات و پشتیبانی', {'fields': ('title', 'telegram_bot_token', 'support_chat_id', 'admin_chat_id', 'is_shop_active')}),
        ('قیمت و درگاه', {'fields': ('dollar_rate_toman', 'oxapay_merchant_api_key', 'oxapay_sandbox', 'invoice_lifetime_minutes', 'oxapay_fee_paid_by_payer')}),
        ('کارت‌به‌کارت', {'fields': ('card_to_card_enabled', 'card_to_card_text')}),
        ('متن‌ها', {'fields': ('tutorial_text', 'contact_intro_text', 'after_purchase_text')}),
    )

    def has_add_permission(self, request):
        return not SiteSetting.objects.exists()


@admin.register(XUIPanel)
class XUIPanelAdmin(admin.ModelAdmin):
    list_display = ('name', 'base_url', 'api_base_path', 'is_active', 'updated_at')
    list_filter = ('is_active', 'verify_ssl')
    search_fields = ('name', 'base_url')


class PlanInline(admin.TabularInline):
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
class ServiceAdmin(admin.ModelAdmin):
    list_display = ('name', 'panel', 'inbound_id', 'inbound_remark', 'sort_order', 'is_active')
    list_filter = ('is_active', 'panel')
    search_fields = ('name', 'description', 'inbound_remark')
    inlines = [PlanInline]
    fieldsets = (
        ('اطلاعات سرویس', {'fields': ('name', 'description', 'panel', 'inbound_id', 'inbound_remark', 'sort_order', 'is_active')}),
        ('قالب تحویل لینک‌ها', {'fields': ('config_link_template', 'subscription_link_template')}),
    )


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'service', 'price_usd', 'price_toman_col', 'duration_days', 'traffic_gb', 'user_limit', 'is_active')
    list_filter = ('is_active', 'service')
    search_fields = ('name', 'description', 'service__name')
    list_editable = ('price_usd', 'duration_days', 'traffic_gb', 'user_limit', 'is_active')

    @admin.display(description='قیمت تومان')
    def price_toman_col(self, obj):
        return f'{obj.price_toman():,}'


@admin.register(TelegramUser)
class TelegramUserAdmin(admin.ModelAdmin):
    list_display = ('chat_id', 'username', 'full_name', 'wallet_balance_toman', 'is_blocked', 'created_at')
    list_filter = ('is_blocked',)
    search_fields = ('chat_id', 'username', 'first_name', 'last_name')
    readonly_fields = ('created_at', 'updated_at')

    @admin.display(description='نام')
    def full_name(self, obj):
        return f'{obj.first_name} {obj.last_name}'.strip()


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
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
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ('user', 'kind', 'amount_toman', 'balance_after_toman', 'order', 'created_at')
    list_filter = ('kind',)
    search_fields = ('user__chat_id', 'description')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('order_id', 'user', 'provider', 'purpose', 'status', 'amount_toman', 'amount_usd', 'track_id', 'created_at')
    list_filter = ('provider', 'purpose', 'status')
    search_fields = ('order_id', 'track_id', 'user__chat_id', 'user__username')
    readonly_fields = ('raw_payload', 'created_at', 'updated_at')


@admin.register(SupportMessage)
class SupportMessageAdmin(admin.ModelAdmin):
    list_display = ('user', 'short_text', 'is_answered', 'created_at')
    list_filter = ('is_answered',)
    search_fields = ('user__chat_id', 'user__username', 'message_text')

    @admin.display(description='متن')
    def short_text(self, obj):
        return obj.message_text[:80]


@admin.action(description='تایید و شارژ کیف پول')
def approve_card_requests(modeladmin, request, queryset):
    count = 0
    for req in queryset.filter(status=CardPaymentRequest.Status.PENDING):
        user = req.user
        user.wallet_balance_toman += req.amount_toman
        user.save(update_fields=['wallet_balance_toman', 'updated_at'])
        WalletTransaction.objects.create(
            user=user,
            kind=WalletTransaction.Kind.CREDIT,
            amount_toman=req.amount_toman,
            balance_after_toman=user.wallet_balance_toman,
            description=f'تایید کارت‌به‌کارت #{req.pk}',
        )
        req.status = CardPaymentRequest.Status.APPROVED
        req.save(update_fields=['status', 'updated_at'])
        count += 1
    messages.success(request, f'{count} درخواست تایید و کیف پول شارژ شد. برای اطلاع به کاربر، یک Broadcast تکی بسازید یا از ربات استفاده کنید.')


@admin.register(CardPaymentRequest)
class CardPaymentRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'amount_toman', 'status', 'receipt_text', 'created_at')
    list_filter = ('status',)
    search_fields = ('user__chat_id', 'user__username', 'receipt_text')
    actions = [approve_card_requests]


@admin.action(description='قرار دادن در صف ارسال')
def queue_broadcasts(modeladmin, request, queryset):
    updated = queryset.update(status=Broadcast.Status.QUEUED)
    messages.success(request, f'{updated} پیام در صف ارسال قرار گرفت. تا وقتی دستور bot در حال اجرا باشد ارسال می‌شود.')


@admin.register(Broadcast)
class BroadcastAdmin(admin.ModelAdmin):
    list_display = ('title', 'target_chat_id', 'status', 'sent_count', 'failed_count', 'updated_at')
    list_filter = ('status',)
    search_fields = ('title', 'text', 'target_chat_id')
    actions = [queue_broadcasts]
