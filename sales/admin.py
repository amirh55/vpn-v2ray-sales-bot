import types
from datetime import datetime, timedelta

from django import forms
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
from .services import reports
from .services.site_urls import (
    admin_url,
    certificate_status,
    domain_is_live,
    oxapay_webhook_url,
    sms_webhook_url,
    telegram_webhook_url,
)
from .services.telegram_webhook import delete_webhook, set_webhook, webhook_status
from .services.xui import XUIClient, XUIError

from .models import (
    BankSms,
    Broadcast,
    CardPaymentRequest,
    DiscountCode,
    DiscountRedemption,
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

class DomainForm(forms.ModelForm):
    """Just the domain fields, so they get their own page in the sidebar."""

    class Meta:
        model = SiteSetting
        fields = ('public_domain', 'force_https', 'ssl_cert_path', 'ssl_key_path')

    def clean_public_domain(self):
        value = (self.cleaned_data.get('public_domain') or '').strip().strip('/')
        if value.startswith(('http://', 'https://')):
            value = value.split('://', 1)[1].strip('/')
        # A path or port here would end up in nginx's server_name and break it.
        if '/' in value:
            raise forms.ValidationError('فقط دامنه را وارد کنید، بدون مسیر. مثل shop.example.com')
        if ' ' in value:
            raise forms.ValidationError('دامنه نباید فاصله داشته باشد.')
        return value


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
    extra_context['report_url'] = reverse('admin:sales_report')
    return _default_admin_index(self, request, extra_context)


admin.site.index = types.MethodType(_dashboard_index, admin.site)


@admin.register(SiteSetting)
class SiteSettingAdmin(ModelAdmin):
    fieldsets = (
        ('ربات و پشتیبانی', {'fields': ('title', 'telegram_bot_token', 'support_chat_id', 'admin_chat_id', 'is_shop_active')}),
        ('قیمت و درگاه', {'fields': ('dollar_rate_toman', 'oxapay_merchant_api_key', 'oxapay_sandbox', 'invoice_lifetime_minutes', 'oxapay_fee_paid_by_payer')}),
        ('دامنه و SSL', {'fields': ('domain_tools',)}),
        ('روش دریافت پیام‌های تلگرام', {
            'fields': ('telegram_use_webhook', 'telegram_webhook_secret', 'telegram_webhook_info'),
            'description': (
                'حالت پیش‌فرض Polling است و روی هر سروری کار می‌کند. '
                'Webhook سریع‌تر است و یک پروسه کمتر مصرف می‌کند، اما فقط با دامنه و SSL معتبر کار می‌کند.'
            ),
        }),
        ('گزارش فروش', {'fields': ('daily_report_enabled', 'daily_report_hour', 'report_tools')}),
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

    readonly_fields = (
        'sms_webhook_url', 'backup_tools', 'domain_tools', 'telegram_webhook_info', 'report_tools',
    )

    @admin.display(description='دامنه و SSL')
    def domain_tools(self, obj):
        return format_html(
            '<a href="{}" style="text-decoration:underline;">رفتن به صفحه دامنه و SSL</a>',
            reverse('admin:sales_domain'),
        )

    @admin.display(description='گزارش فروش')
    def report_tools(self, obj):
        return format_html(
            '<a href="{}" style="text-decoration:underline;">رفتن به صفحه گزارش فروش</a>',
            reverse('admin:sales_report'),
        )

    @admin.display(description='آدرس و وضعیت وبهوک تلگرام')
    def telegram_webhook_info(self, obj):
        """Show the address and what Telegram currently has registered.

        Saving the switch above only records intent; Telegram is told separately
        by `vpnshop webhook`, and this row is how the operator sees whether that
        step has happened.
        """
        if not obj or not obj.pk:
            return 'بعد از ذخیره نمایش داده می‌شود'
        url = telegram_webhook_url()
        if not url:
            return 'یک بار ذخیره کنید تا کلید مخفی ساخته شود.'

        rows = [f'<b>آدرس وبهوک:</b><br><code style="user-select:all;word-break:break-all;">{escape(url)}</code>']
        if not obj.telegram_use_webhook:
            rows.append('ℹ️ حالت فعلی Polling است. برای استفاده از وبهوک، گزینه بالا را روشن کنید.')
        elif not url.startswith('https://'):
            rows.append('⛔️ آدرس فعلی https نیست. تلگرام وبهوک بدون HTTPS را نمی‌پذیرد.')
        else:
            rows.append(
                'برای اعمال، روی سرور بزنید: <code>vpnshop webhook</code><br>'
                'یا از دکمه‌های زیر استفاده کنید.'
            )

        rows.append(
            f'<a href="{reverse("admin:sales_telegram_webhook_apply")}?action=status" '
            'style="text-decoration:underline;">بررسی وضعیت در تلگرام</a> — '
            f'<a href="{reverse("admin:sales_telegram_webhook_apply")}?action=set" '
            'style="text-decoration:underline;">ثبت وبهوک</a> — '
            f'<a href="{reverse("admin:sales_telegram_webhook_apply")}?action=delete" '
            'style="text-decoration:underline;">حذف وبهوک</a>'
        )
        return mark_safe('<br>'.join(rows))  # noqa: S308 - values are URLs we build

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
        elif not domain_is_live(obj):
            rows.append(
                '⛔️ <b>این دامنه هنوز اعمال نشده است.</b> ذخیره کردن در پنل کافی نیست، '
                'چون Nginx و تنظیمات سرور باید به‌روز شوند. روی سرور بزنید:<br>'
                '<code>vpnshop domain</code>'
            )
        else:
            rows.append('✅ دامنه روی سرور اعمال شده است.')
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
            path(
                'domain/',
                self.admin_site.admin_view(self.domain_view),
                name='sales_domain',
            ),
            path(
                'report/',
                self.admin_site.admin_view(self.report_view),
                name='sales_report',
            ),
            path(
                'report/csv/',
                self.admin_site.admin_view(self.report_csv_view),
                name='sales_report_csv',
            ),
            path(
                'telegram-webhook/',
                self.admin_site.admin_view(self.telegram_webhook_view),
                name='sales_telegram_webhook_apply',
            ),
        ]
        return custom + super().get_urls()

    @staticmethod
    def _report_from_request(request):
        """Resolve the query string into a report and the labels the page shows.

        Explicit dates win over the quick-pick buttons. A malformed or reversed
        pair falls back to the named range rather than erroring, because this is
        a hand-editable URL.
        """
        range_key = request.GET.get('range') or 'month'
        raw_from = (request.GET.get('from') or '').strip()
        raw_to = (request.GET.get('to') or '').strip()

        start = end = None
        if raw_from and raw_to:
            try:
                from_date = datetime.strptime(raw_from, '%Y-%m-%d').date()
                to_date = datetime.strptime(raw_to, '%Y-%m-%d').date()
            except ValueError:
                from_date = to_date = None
            if from_date and to_date and from_date <= to_date:
                start = reports.day_bounds(from_date)[0]
                end = reports.day_bounds(to_date)[1]
                range_key = 'custom'
                label = f'{from_date:%Y-%m-%d} تا {to_date:%Y-%m-%d}'

        if start is None:
            start, end, label = reports.named_range(range_key)

        return reports.build(start, end, label), range_key

    def report_view(self, request):
        report, range_key = self._report_from_request(request)

        max_daily = max((row['revenue'] for row in report.daily), default=0)
        daily_rows = [
            {
                'date': row['date'].strftime('%Y-%m-%d'),
                'count': f'{row["count"]:,}',
                'revenue': f'{row["revenue"]:,}',
                'percent': int(row['revenue'] * 100 / max_daily) if max_daily else 0,
            }
            for row in report.daily
        ]

        context = {
            **self.admin_site.each_context(request),
            'title': 'گزارش فروش',
            'report': report,
            'active_range': range_key,
            'from_value': timezone.localtime(report.start).strftime('%Y-%m-%d'),
            'to_value': (timezone.localtime(report.end) - timedelta(seconds=1)).strftime('%Y-%m-%d'),
            'start_display': timezone.localtime(report.start).strftime('%Y-%m-%d'),
            'end_display': (timezone.localtime(report.end) - timedelta(seconds=1)).strftime('%Y-%m-%d'),
            'csv_url': f'{reverse("admin:sales_report_csv")}?{request.GET.urlencode()}',
            'quick_ranges': [
                {'key': 'today', 'title': 'امروز'},
                {'key': 'yesterday', 'title': 'دیروز'},
                {'key': 'week', 'title': '۷ روز'},
                {'key': 'month', 'title': 'این ماه'},
                {'key': 'last_month', 'title': 'ماه گذشته'},
                {'key': 'year', 'title': '۱۲ ماه'},
            ],
            'cards': [
                {'title': 'درآمد فروش', 'value': f'{report.revenue_toman:,} تومان', 'icon': 'payments',
                 'hint': f'قبل از تخفیف: {report.gross_toman:,} تومان' if report.discount_toman else ''},
                {'title': 'تعداد سفارش', 'value': f'{report.order_count:,}', 'icon': 'shopping_cart',
                 'hint': f'میانگین هر سفارش: {report.average_order_toman:,} تومان' if report.order_count else ''},
                {'title': 'تخفیف داده‌شده', 'value': f'{report.discount_toman:,} تومان', 'icon': 'local_offer'},
                {'title': 'کاربر جدید', 'value': f'{report.new_users:,}', 'icon': 'person_add'},
                {'title': 'شارژ کیف پول', 'value': f'{report.wallet_topup_toman:,} تومان', 'icon': 'account_balance_wallet',
                 'hint': 'مجموع پرداخت‌های تاییدشده در این بازه'},
                {'title': 'منقضی تا ۷ روز آینده', 'value': f'{report.expiring_soon:,}', 'icon': 'schedule',
                 'hint': f'اشتراک فعال: {report.active_subscriptions:,}'},
            ],
            'tables': [
                {
                    'title': 'فروش به تفکیک روش پرداخت',
                    'first_column': 'روش پرداخت',
                    'value_column': 'درآمد',
                    'rows': [{'title': r['title'], 'count': f'{r["count"]:,}', 'value': f'{r["revenue"]:,}'} for r in report.by_source],
                },
                {
                    'title': 'فروش به تفکیک سرویس',
                    'first_column': 'سرویس',
                    'value_column': 'درآمد',
                    'rows': [{'title': r['title'], 'count': f'{r["count"]:,}', 'value': f'{r["revenue"]:,}'} for r in report.by_service],
                },
                {
                    'title': 'فروش به تفکیک پلن',
                    'first_column': 'پلن',
                    'value_column': 'درآمد',
                    'rows': [{'title': r['title'], 'count': f'{r["count"]:,}', 'value': f'{r["revenue"]:,}'} for r in report.by_plan],
                },
                {
                    'title': 'کدهای تخفیف استفاده‌شده',
                    'first_column': 'کد',
                    'value_column': 'تخفیف داده‌شده',
                    'rows': [{'title': r['title'], 'count': f'{r["count"]:,}', 'value': f'{r["discount"]:,}'} for r in report.by_discount],
                },
            ],
            'daily_rows': daily_rows,
        }
        return TemplateResponse(request, 'admin/sales/report.html', context)

    def report_csv_view(self, request):
        report, _ = self._report_from_request(request)
        response = HttpResponse(reports.to_csv(report), content_type='text/csv; charset=utf-8')
        stamp = timezone.localtime(report.start).strftime('%Y%m%d')
        response['Content-Disposition'] = f'attachment; filename="sales-report-{stamp}.csv"'
        return response

    def telegram_webhook_view(self, request):
        """Apply the webhook switch without needing shell access to the server."""
        action = request.GET.get('action') or 'status'
        if action == 'set':
            ok, message = set_webhook()
        elif action == 'delete':
            ok, message = delete_webhook()
        else:
            info = webhook_status()
            ok = info['ok']
            message = info['note']
            if info.get('current_url'):
                message += f' آدرس ثبت‌شده: {info["current_url"]}'
            if info.get('last_error_message'):
                message += f' | آخرین خطای تلگرام: {info["last_error_message"]}'

        messages.success(request, message) if ok else messages.error(request, message)
        return HttpResponseRedirect(reverse('admin:sales_sitesetting_changelist'))

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

    def domain_view(self, request):
        site = SiteSetting.get_solo()
        if request.method == 'POST':
            form = DomainForm(request.POST, instance=site)
            if form.is_valid():
                form.save()
                messages.success(
                    request,
                    'ذخیره شد. برای اینکه دامنه واقعا کار کند، روی سرور «vpnshop domain» را اجرا کنید.',
                )
                return HttpResponseRedirect(request.path)
            messages.error(request, 'مقادیر واردشده معتبر نیستند.')
        else:
            form = DomainForm(instance=site)

        context = {
            **self.admin_site.each_context(request),
            'title': 'دامنه و SSL',
            'form': form,
            'panel_url': admin_url(),
            'oxapay_url': oxapay_webhook_url(),
            'sms_url': sms_webhook_url(),
            'cert_rows': certificate_status(site),
            'domain_live': domain_is_live(site),
            'has_domain': bool(site.public_domain),
        }
        return TemplateResponse(request, 'admin/sales/domain.html', context)

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


@admin.action(description='خواندن ساختار API از پنل (openapi.json)')
def refresh_panel_openapi(modeladmin, request, queryset):
    """Ask each panel what its API looks like and remember the answer.

    Once a panel's schema is known, creating a client is a single request to the
    endpoint that panel actually documents, instead of trying the known shapes
    one after another until one is accepted.
    """
    done = failed = 0
    for panel in queryset:
        try:
            XUIClient(panel).refresh_schema()
        except XUIError as exc:
            failed += 1
            messages.warning(
                request,
                f'{panel.name}: ساختار API خوانده نشد ({exc}). '
                'این پنل با روش قبلی و امتحان کردن مسیرهای رایج کار می‌کند.',
            )
            continue
        panel.refresh_from_db()
        done += 1
        messages.success(request, f'{panel.name}: {panel.openapi_note}')
    if done:
        messages.success(request, f'{done} پنل به‌روز شد.')
    if failed and not done:
        messages.info(request, 'ساخت سرویس همچنان کار می‌کند؛ فقط از مسیر حدس‌وآزمون قبلی.')


@admin.register(XUIPanel)
class XUIPanelAdmin(ModelAdmin):
    list_display = ('name', 'base_url', 'api_base_path', 'api_mode', 'is_active', 'updated_at')
    list_filter = ('is_active', 'verify_ssl')
    search_fields = ('name', 'base_url')
    actions = [refresh_panel_openapi]
    readonly_fields = ('openapi_fetched_at', 'openapi_add_client_path', 'openapi_note', 'openapi_summary')
    fieldsets = (
        ('اتصال', {'fields': ('name', 'base_url', 'api_token', 'api_base_path', 'subscription_base_url')}),
        ('تنظیمات ارتباط', {'fields': ('verify_ssl', 'timeout_seconds', 'is_active')}),
        ('ساختار API این پنل', {
            'fields': ('openapi_summary', 'openapi_add_client_path', 'openapi_fetched_at', 'openapi_note'),
            'description': (
                'اگر پنل شما openapi.json بدهد، ربات ساختار درخواست ساخت کلاینت را از روی همان می‌سازد '
                'و دیگر نیازی به امتحان کردن چند مسیر مختلف نیست. '
                'از منوی «عملیات» در فهرست پنل‌ها می‌توانید آن را دوباره بخوانید.'
            ),
        }),
    )

    @admin.display(description='روش API')
    def api_mode(self, obj):
        return 'از روی openapi.json' if obj.openapi_add_client_path else 'حدس مسیرهای رایج'

    @admin.display(description='وضعیت')
    def openapi_summary(self, obj):
        if not obj or not obj.pk:
            return 'بعد از ذخیره نمایش داده می‌شود'
        if not obj.openapi_add_client_path:
            return (
                'هنوز خوانده نشده یا این پنل openapi.json ندارد. '
                'ساخت سرویس کار می‌کند، اما با امتحان کردن مسیرهای رایج.'
            )
        paths = (obj.openapi_schema or {}).get('paths') or {}
        return f'✅ {len(paths)} مسیر شناخته شد. ساخت کلاینت از: {obj.openapi_add_client_path}'


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


class DiscountRedemptionInline(TabularInline):
    model = DiscountRedemption
    extra = 0
    can_delete = False
    fields = ('user', 'order', 'amount_toman', 'created_at')
    readonly_fields = ('user', 'order', 'amount_toman', 'created_at')

    def has_add_permission(self, request, obj=None):
        return False


@admin.action(description='فعال کردن')
def activate_discount_codes(modeladmin, request, queryset):
    messages.success(request, f'{queryset.update(is_active=True)} کد فعال شد.')


@admin.action(description='غیرفعال کردن')
def deactivate_discount_codes(modeladmin, request, queryset):
    messages.success(request, f'{queryset.update(is_active=False)} کد غیرفعال شد.')


@admin.register(DiscountCode)
class DiscountCodeAdmin(ModelAdmin):
    list_display = ('code', 'title', 'value_display', 'usage_display', 'validity', 'is_active')
    list_filter = ('is_active', 'kind')
    search_fields = ('code', 'title', 'note')
    filter_horizontal = ('services', 'plans')
    readonly_fields = ('used_count', 'created_at', 'updated_at')
    actions = [activate_discount_codes, deactivate_discount_codes]
    inlines = [DiscountRedemptionInline]
    fieldsets = (
        ('کد', {'fields': ('code', 'title', 'is_active', 'note')}),
        ('مقدار تخفیف', {
            'fields': ('kind', 'percent', 'amount_toman', 'max_discount_toman', 'min_order_toman'),
            'description': 'برای نوع درصدی فقط «درصد تخفیف» و «سقف تخفیف» مهم است؛ برای نوع مبلغ ثابت فقط «مبلغ تخفیف».',
        }),
        ('محدودیت‌ها', {'fields': ('valid_from', 'valid_until', 'max_uses', 'max_uses_per_user', 'used_count')}),
        ('محدود به', {'fields': ('services', 'plans')}),
    )

    @admin.display(description='مقدار')
    def value_display(self, obj):
        return obj.value_text()

    @admin.display(description='استفاده')
    def usage_display(self, obj):
        if not obj.max_uses:
            return f'{obj.used_count} بار (نامحدود)'
        return f'{obj.used_count} از {obj.max_uses}'

    @admin.display(description='اعتبار')
    def validity(self, obj):
        now = timezone.now()
        if obj.valid_until and obj.valid_until < now:
            return 'منقضی شده'
        if obj.valid_from and obj.valid_from > now:
            return f'از {timezone.localtime(obj.valid_from):%Y-%m-%d}'
        if obj.valid_until:
            return f'تا {timezone.localtime(obj.valid_until):%Y-%m-%d}'
        return 'بدون محدودیت زمانی'


@admin.register(DiscountRedemption)
class DiscountRedemptionAdmin(ModelAdmin):
    list_display = ('code', 'user', 'order', 'amount_toman', 'created_at')
    list_filter = ('code',)
    search_fields = ('code__code', 'user__chat_id', 'user__username')
    readonly_fields = ('code', 'user', 'order', 'amount_toman', 'amount_usd', 'created_at', 'updated_at')

    def has_add_permission(self, request):
        return False


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
    list_display = ('id', 'user', 'service', 'plan', 'status', 'source', 'amount_toman', 'discount_code', 'expires_at', 'created_at')
    list_filter = ('status', 'source', 'service', 'plan', 'discount_code')
    search_fields = ('id', 'user__chat_id', 'user__username', 'xui_client_email', 'xui_client_uuid')
    actions = [resend_order_config, provision_and_send]
    readonly_fields = ('config_link_click', 'subscription_link_click', 'qr_preview', 'created_at', 'updated_at')
    fieldsets = (
        ('سفارش', {'fields': ('user', 'service', 'plan', 'source', 'status', 'amount_usd', 'amount_toman', 'discount_code', 'discount_toman', 'admin_note')}),
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
