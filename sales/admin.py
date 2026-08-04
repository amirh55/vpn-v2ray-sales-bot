import types

from django.contrib import admin, messages
from django.db.models import Sum
from django.http import HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from unfold.admin import ModelAdmin, TabularInline

from .services.backup import RestoreError, backup_filename, create_backup, restore_backup
from .services.cardpay import approve_request
from .services.delivery import send_order
from .services.payments import settle_payment
from .services.provisioning import provision_order
from .services.site_urls import admin_url, certificate_status, oxapay_webhook_url, sms_webhook_url

from .models import (
    BankSms,
    Broadcast,
    CardPaymentRequest,
    FaqItem,
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
        ('دامنه و SSL', {
            'fields': ('public_domain', 'force_https', 'ssl_cert_path', 'ssl_key_path', 'domain_status'),
            'description': (
                'دامنه‌ای که پنل و ربات روی آن در دسترس هستند. '
                'آدرس webhook درگاه پرداخت و پیامک از همین ساخته می‌شود. '
                'بعد از ذخیره، برای اعمال روی Nginx روی سرور بزنید: <code>vpnshop domain</code>'
            ),
        }),
        ('کارت‌به‌کارت', {
            'fields': (
                'card_to_card_enabled', 'card_number', 'card_holder_name', 'card_bank_name',
                'card_to_card_text', 'card_invoice_minutes',
            ),
        }),
        ('تایید خودکار با پیامک بانکی', {
            'fields': ('card_auto_confirm_enabled', 'sms_webhook_secret', 'sms_allowed_senders', 'sms_webhook_url'),
        }),
        ('متن‌ها', {'fields': ('tutorial_text', 'contact_intro_text', 'faq_intro_text', 'after_purchase_text')}),
        ('پشتیبان‌گیری', {'fields': ('backup_tools',)}),
    )

    readonly_fields = ('sms_webhook_url', 'backup_tools', 'domain_status')

    @admin.display(description='وضعیت دامنه و گواهی')
    def domain_status(self, obj):
        if not obj or not obj.pk:
            return 'بعد از ذخیره نمایش داده می‌شود'
        rows = [
            f'<b>آدرس فعلی پنل:</b> <code>{escape(admin_url())}</code>',
            f'<b>Webhook درگاه:</b> <code>{escape(oxapay_webhook_url())}</code>',
        ]
        for row in certificate_status(obj):
            mark = '✅' if row['ok'] else '⚠️'
            # The paths are typed by the operator, so escape before embedding.
            path = f' <code>{escape(row["path"])}</code>' if row['path'] else ''
            rows.append(f'{mark} <b>{escape(row["label"])}:</b>{path} — {escape(row["note"])}')
        if not obj.public_domain:
            rows.append('⚠️ دامنه وارد نشده؛ فعلا از مقدار فایل نصب استفاده می‌شود.')
        return mark_safe('<br>'.join(rows))  # noqa: S308 - values are paths and URLs we build

    def has_add_permission(self, request):
        return not SiteSetting.objects.exists()

    def get_urls(self):
        # Hung off this model because it is where operators already go, and it
        # keeps the views behind the admin's own authentication.
        custom = [
            path(
                'backup/',
                self.admin_site.admin_view(self.backup_view),
                name='sales_backup',
            ),
            path(
                'backup/download/',
                self.admin_site.admin_view(self.backup_download_view),
                name='sales_backup_download',
            ),
        ]
        return custom + super().get_urls()

    def backup_view(self, request):
        if request.method == 'POST':
            uploaded = request.FILES.get('backup_file')
            if not uploaded:
                messages.error(request, 'فایلی انتخاب نشده است.')
            else:
                try:
                    result = restore_backup(uploaded.read())
                except RestoreError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(
                        request,
                        'بازگردانی انجام شد. '
                        f'{result["media_files"]} فایل بازگردانی شد. '
                        'برای اعمال کامل، سرویس‌ها را با vpnshop restart ری‌استارت کنید.',
                    )
            return HttpResponseRedirect(request.path)

        context = {
            **self.admin_site.each_context(request),
            'title': 'پشتیبان‌گیری و بازگردانی',
        }
        return TemplateResponse(request, 'admin/sales/backup.html', context)

    def backup_download_view(self, request):
        response = HttpResponse(create_backup(), content_type='application/zip')
        response['Content-Disposition'] = f'attachment; filename="{backup_filename()}"'
        return response

    @admin.display(description='پشتیبان‌گیری')
    def backup_tools(self, obj):
        return format_html(
            '<a href="{}" style="text-decoration:underline;">'
            'رفتن به صفحه پشتیبان‌گیری و بازگردانی</a>',
            reverse('admin:sales_backup'),
        )

    @admin.display(description='آدرس وبهوک پیامک')
    def sms_webhook_url(self, obj):
        if not obj or not obj.pk:
            return 'بعد از ذخیره نمایش داده می‌شود'
        if not obj.sms_webhook_secret:
            return 'ابتدا کلید مخفی را ذخیره کنید یا خالی بگذارید تا خودکار ساخته شود'
        url = sms_webhook_url()
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
    fields = ('name', 'price_toman', 'price_usd', 'duration_days', 'traffic_gb', 'user_limit', 'sort_order', 'is_active')


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
    list_display = ('name', 'service', 'price_toman', 'price_usd', 'crypto_saving', 'duration_days', 'traffic_gb', 'user_limit', 'is_active')
    list_filter = ('is_active', 'service')
    search_fields = ('name', 'description', 'service__name')
    list_editable = ('price_toman', 'price_usd', 'duration_days', 'traffic_gb', 'user_limit', 'is_active')

    @admin.display(description='تخفیف کریپتو')
    def crypto_saving(self, obj):
        percent = obj.crypto_saving_percent()
        if percent <= 0:
            return 'ندارد'
        return f'{percent}٪ ({obj.crypto_saving_toman():,} تومان)'


@admin.register(TelegramUser)
class TelegramUserAdmin(ModelAdmin):
    list_display = ('chat_id', 'username', 'full_name', 'wallet_balance_toman', 'is_blocked', 'created_at')
    list_filter = ('is_blocked',)
    search_fields = ('chat_id', 'username', 'first_name', 'last_name')
    readonly_fields = ('created_at', 'updated_at')

    @admin.display(description='نام')
    def full_name(self, obj):
        return f'{obj.first_name} {obj.last_name}'.strip()


@admin.action(description='ارسال دوباره کانفیگ به کاربر در تلگرام')
def resend_order_config(modeladmin, request, queryset):
    sent = failed = 0
    for order in queryset:
        if send_order(order):
            sent += 1
        else:
            failed += 1
    if sent:
        messages.success(request, f'کانفیگ {sent} سفارش دوباره برای کاربر ارسال شد.')
    if failed:
        messages.error(
            request,
            f'{failed} مورد ارسال نشد. توکن ربات را بررسی کنید و مطمئن شوید کاربر ربات را بلاک نکرده است.',
        )


@admin.action(description='ساخت سرویس در پنل و ارسال به کاربر')
def provision_and_send(modeladmin, request, queryset):
    # For orders whose payment landed but whose 3x-ui call failed at the time.
    done = failed = 0
    for order in queryset:
        try:
            order = provision_order(order)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            messages.error(request, f'سفارش #{order.pk}: ساخت سرویس ناموفق بود: {exc}')
            continue
        send_order(order)
        done += 1
    if done:
        messages.success(request, f'{done} سرویس ساخته و برای کاربر ارسال شد.')


@admin.register(Order)
class OrderAdmin(ModelAdmin):
    list_display = ('id', 'user', 'service', 'plan', 'status', 'source', 'amount_toman', 'expires_at', 'created_at')
    list_filter = ('status', 'source', 'service', 'plan')
    search_fields = ('id', 'user__chat_id', 'user__username', 'xui_client_email', 'xui_client_uuid')
    actions = [resend_order_config, provision_and_send]
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


@admin.action(description='تایید دستی پرداخت، شارژ کیف پول و تحویل سرویس')
def approve_payments_manually(modeladmin, request, queryset):
    """Rescue payments whose gateway callback never arrived."""
    done = skipped = 0
    for payment in queryset:
        if payment.status == Payment.Status.PAID:
            skipped += 1
            continue
        try:
            settle_payment(payment, note=f'تایید دستی توسط {request.user.username}')
        except Exception as exc:  # noqa: BLE001
            messages.error(request, f'پرداخت {payment.order_id}: {exc}')
            continue
        done += 1
    if done:
        messages.success(
            request,
            f'{done} پرداخت تایید شد. کیف پول شارژ شد و اگر پلنی در انتظار بود، سرویس ساخته و ارسال شد.',
        )
    if skipped:
        messages.warning(request, f'{skipped} مورد از قبل پرداخت‌شده بود و دوباره پردازش نشد.')


@admin.register(Payment)
class PaymentAdmin(ModelAdmin):
    list_display = ('order_id', 'user', 'provider', 'purpose', 'status', 'amount_toman', 'amount_usd', 'track_id', 'created_at')
    list_filter = ('provider', 'purpose', 'status')
    search_fields = ('order_id', 'track_id', 'user__chat_id', 'user__username')
    readonly_fields = ('raw_payload', 'created_at', 'updated_at')
    actions = [approve_payments_manually]


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


@admin.register(FaqItem)
class FaqItemAdmin(ModelAdmin):
    list_display = ('question', 'sort_order', 'is_active', 'updated_at')
    list_filter = ('is_active',)
    list_editable = ('sort_order', 'is_active')
    search_fields = ('question', 'answer')


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
