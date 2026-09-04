"""The partner panel, inside the same bot the customers use.

No second bot and no second menu: `/work` opens a panel for whoever is a partner
and offers to apply for whoever is not. Everything a partner does here ends up
in the ordinary Order the rest of the shop already understands, which is what
keeps renewal, resend, the traffic sweep and the admin working unchanged.

Access is decided by chat id on every callback, not by `/work` being hard to
guess — a guessed callback from a customer gets the same refusal as a typed
command.

Imports from botcore are safe at module level: botcore does not import this
module until `register_handlers` runs, by which point it is fully loaded.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from telebot import TeleBot

from sales.models import (
    Order,
    Partner,
    PartnerInvoice,
    PartnerInvoiceItem,
    Plan,
    Service,
    SiteSetting,
    TelegramUser,
)
from sales.services import jalali, partner_billing
from sales.services.botcore import (
    cancel_keyboard,
    create_oxapay_payment,
    edit_or_send,
    inline,
    main_reply_keyboard,
    notify_operator,
    send_delivery,
    send_or_edit,
)
from sales.services.cardpay import create_request as create_card_request
from sales.services.clientname import (
    ClientNameError,
    is_taken as client_name_taken,
    unique_suggestion as unique_client_name,
    validate as validate_client_name,
)
from sales.services.formatting import days_text, fa_digits, toman, traffic_text
from sales.services.oxapay import OxaPayError
from sales.services.partner_billing import PartnerBillingError
from sales.services.partner_pricing import price_lines, quote as price_quote
from sales.services.provisioning import (
    create_partner_order,
    delete_order_client,
    extend_order,
    provision_order,
    push_renewal_to_panel,
)

# States. Prefixed so they cannot collide with the customer flow's states, which
# share the same `TelegramUser.state` column.
ST_REQUEST = 'pw_request'
ST_CUSTOMER = 'pw_customer'


def get_partner(user: TelegramUser) -> Partner | None:
    """The active partner behind this chat, or None.

    Called at the top of every partner callback. An inactive partner is treated
    as no partner for anything that acts, but is told why rather than being
    silently ignored.
    """
    return Partner.objects.filter(user=user, is_active=True).select_related('user').first()


def program_enabled() -> bool:
    return bool(SiteSetting.get_solo().partner_program_enabled)


# ─────────────────────────────────────────────────────────────────────────────
# ورود
# ─────────────────────────────────────────────────────────────────────────────


def panel_keyboard(partner: Partner):
    rows = [
        [('🛒 سفارش جدید', 'pw:new')],
        [('📦 کانفیگ‌های من', 'pw:list'), ('👤 حساب همکاری', 'pw:me')],
    ]
    if partner.billing_mode == Partner.BillingMode.CREDIT:
        rows.append([('🧾 فاکتورها', 'pw:inv'), ('💰 بدهی و اعتبار', 'pw:debt')])
    rows.append([('📊 گزارش فروش', 'pw:report')])
    rows.append([('🏠 منوی اصلی', 'cancel')])
    return inline(rows)


def panel_text(partner: Partner) -> str:
    site = SiteSetting.get_solo()
    lines = [f'🤝 <b>پنل همکاری — {partner.display_name}</b>']
    if site.partner_panel_welcome_text:
        lines += ['', site.partner_panel_welcome_text]

    if partner.billing_mode == Partner.BillingMode.CREDIT:
        overdue = partner.overdue_invoice()
        debt = partner.current_debt_toman()
        lines += ['', f'نوع همکاری: اعتباری، تسویه هر {fa_digits(partner.billing_cycle_days)} روز']
        if overdue is not None:
            lines.append(
                f'⛔️ فاکتور {overdue.number} سررسید شده است. '
                'تا تسویه، امکان سفارش جدید ندارید.'
            )
        elif debt > 0:
            invoice = partner.open_invoice()
            lines.append(f'🧾 فاکتور جاری: {toman(debt)}')
            if invoice:
                lines.append(f'سررسید: {jalali.format_datetime(invoice.due_at)}')
        else:
            lines.append('✅ بدهی ندارید.')
    else:
        partner.user.refresh_from_db()
        lines += ['', 'نوع همکاری: پرداخت فوری', f'💳 موجودی کیف پول: {toman(partner.user.wallet_balance_toman)}']

    return '\n'.join(lines)


def show_panel(bot: TeleBot, partner: Partner, chat_id: int, call=None):
    send_or_edit(bot, chat_id, panel_text(partner), panel_keyboard(partner), call=call)


def handle_work(bot: TeleBot, user: TelegramUser, chat_id: int, call=None):
    """The `/work` entry point, and the only place that decides who sees what."""
    site = SiteSetting.get_solo()
    if not site.partner_program_enabled:
        send_or_edit(bot, chat_id, 'این بخش فعلاً فعال نیست.', call=call)
        return

    partner = Partner.objects.filter(user=user).first()
    if partner and partner.is_active:
        show_panel(bot, partner, chat_id, call=call)
        return
    if partner and not partner.is_active:
        send_or_edit(
            bot, chat_id,
            '⛔️ حساب همکاری شما غیرفعال است.\n\n'
            'کانفیگ‌ها و فاکتورهای شما محفوظ است. برای فعال‌سازی با پشتیبانی تماس بگیرید.',
            call=call,
        )
        return

    pending = user.partner_requests.filter(status='pending').first()
    if pending:
        send_or_edit(
            bot, chat_id,
            '⏳ درخواست همکاری شما ثبت شده و در حال بررسی است.\n'
            f'تاریخ ثبت: {jalali.format_datetime(pending.created_at)}\n\n'
            'نتیجه از همین ربات به شما اطلاع داده می‌شود.',
            call=call,
        )
        return

    if not site.partner_request_enabled:
        send_or_edit(bot, chat_id, 'پذیرش همکار جدید فعلاً بسته است.', call=call)
        return

    send_or_edit(
        bot, chat_id,
        '🤝 <b>همکاری در فروش</b>\n\n'
        + (site.partner_request_intro_text or '')
        + '\n\nبا همکاری در فروش، اشتراک‌ها را با قیمت ویژه می‌گیرید و '
        'برای مشتریان خودتان کانفیگ می‌سازید.',
        inline([[('📝 درخواست همکاری', 'pw:req')], [('بازگشت', 'cancel')]]),
        call=call,
    )


# ─────────────────────────────────────────────────────────────────────────────
# درخواست همکاری
# ─────────────────────────────────────────────────────────────────────────────

REQUEST_PROMPT = (
    '📝 <b>درخواست همکاری</b>\n\n'
    'اطلاعات زیر را در <b>یک پیام</b> و هر کدام در یک خط بفرستید:\n\n'
    '۱. نام و نام خانوادگی\n'
    '۲. شماره تماس\n'
    '۳. کانال یا محل فروش شما\n'
    '۴. حدود فروش ماهانه\n\n'
    'مثال:\n'
    '<code>رضا محمدی\n۰۹۱۲۱۲۳۴۵۶۷\nکانال تلگرام ۵ هزار عضو\nحدود ۵۰ کاربر</code>'
)


def start_request(bot: TeleBot, user: TelegramUser, chat_id: int, call=None):
    user.state = ST_REQUEST
    user.temp_data = {}
    user.save(update_fields=['state', 'temp_data', 'updated_at'])
    send_or_edit(bot, chat_id, REQUEST_PROMPT, cancel_keyboard(), call=call)


def submit_request(bot: TeleBot, user: TelegramUser, chat_id: int, text: str) -> None:
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    if not lines:
        bot.send_message(chat_id, 'چیزی دریافت نشد. اطلاعات را طبق نمونه بفرستید.', reply_markup=cancel_keyboard())
        return
    if len(lines) < 2:
        bot.send_message(
            chat_id,
            '⚠️ حداقل نام و شماره تماس لازم است، هر کدام در یک خط.',
            reply_markup=cancel_keyboard(),
        )
        return

    request = user.partner_requests.create(
        full_name=lines[0][:120],
        phone=lines[1][:20],
        sales_channel=(lines[2] if len(lines) > 2 else '')[:200],
        monthly_volume=(lines[3] if len(lines) > 3 else '')[:100],
        note='\n'.join(lines[4:])[:2000],
    )
    user.state = ''
    user.temp_data = {}
    user.save(update_fields=['state', 'temp_data', 'updated_at'])

    notify_operator(
        bot,
        '🤝 <b>درخواست همکاری جدید</b>\n\n'
        f'👤 {request.full_name}\n'
        f'📞 {request.phone}\n'
        f'📢 {request.sales_channel or "-"}\n'
        f'📈 {request.monthly_volume or "-"}\n'
        f'🆔 Chat ID: <code>{user.chat_id}</code>\n\n'
        'در پنل، «درخواست‌های همکاری» را باز کنید و با اکشن «تایید درخواست و ساخت همکار» تاییدش کنید.',
    )
    bot.send_message(
        chat_id,
        '✅ درخواست شما ثبت شد.\nبعد از بررسی، نتیجه از همین ربات اطلاع داده می‌شود.',
        reply_markup=main_reply_keyboard(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# سفارش جدید
# ─────────────────────────────────────────────────────────────────────────────


def show_services(bot: TeleBot, partner: Partner, chat_id: int, call=None):
    try:
        partner_billing.check_can_order(partner, Decimal('0'))
    except PartnerBillingError as exc:
        rows = [[('🧾 فاکتورها', 'pw:inv')]] if partner.billing_mode == Partner.BillingMode.CREDIT else []
        rows.append([('بازگشت', 'pw:home')])
        send_or_edit(bot, chat_id, f'⛔️ {exc}', inline(rows), call=call)
        return

    plans = partner.available_plans()
    services = Service.objects.filter(
        is_active=True, pk__in=plans.values('service_id')
    ).distinct().order_by('sort_order', 'name')
    if not services:
        send_or_edit(
            bot, chat_id,
            'هیچ سرویسی برای شما فعال نیست. با پشتیبانی تماس بگیرید.',
            inline([[('بازگشت', 'pw:home')]]), call=call,
        )
        return
    rows = [[(f'🟢 {s.name}', f'pw:svc:{s.pk}')] for s in services]
    rows.append([('بازگشت', 'pw:home')])
    send_or_edit(bot, chat_id, 'سرویس مورد نظر را انتخاب کنید:', inline(rows), call=call)


def show_plans(bot: TeleBot, partner: Partner, chat_id: int, service_id: int, call=None):
    plans = partner.available_plans().filter(service_id=service_id).order_by('sort_order', 'price_usd')
    if not plans:
        send_or_edit(bot, chat_id, 'پلنی برای شما در این سرویس فعال نیست.', inline([[('بازگشت', 'pw:new')]]), call=call)
        return
    rows = []
    for plan in plans:
        final = partner.final_price_toman(plan)
        rows.append([(f'{plan.name} — {toman(final)}', f'pw:plan:{plan.pk}')])
    rows.append([('بازگشت', 'pw:new')])
    send_or_edit(bot, chat_id, 'پلن مورد نظر را انتخاب کنید:\n\nقیمت‌ها، قیمت همکاری شماست.', inline(rows), call=call)


CUSTOMER_PROMPT = (
    '👤 <b>نام مشتری</b>\n\n'
    'نام مشتری این کانفیگ را بفرستید.\n'
    'این نام در پنل کنار کانفیگ ثبت می‌شود تا بعداً بدانید مال کیست.\n\n'
    'اگر نام انگلیسی و بدون فاصله بفرستید، همان نام کاربری کانفیگ هم می‌شود؛ '
    'در غیر این صورت یک نام کاربری خودکار ساخته می‌شود.'
)


def ask_customer(bot: TeleBot, partner: Partner, user: TelegramUser, chat_id: int, plan: Plan, call=None):
    quote = price_quote(partner, plan)
    try:
        partner_billing.check_can_order(partner, quote.final_toman)
    except PartnerBillingError as exc:
        send_or_edit(bot, chat_id, f'⛔️ {exc}', inline([[('بازگشت', 'pw:home')]]), call=call)
        return

    user.state = ST_CUSTOMER
    user.temp_data = {**(user.temp_data or {}), 'pw_plan': plan.pk}
    user.save(update_fields=['state', 'temp_data', 'updated_at'])

    text = (
        f'📌 <b>{plan.name}</b> — {plan.service.name}\n'
        f'مدت: {days_text(plan.duration_days)} | حجم: {traffic_text(plan.traffic_gb)}\n\n'
        f'{price_lines(quote)}\n\n{CUSTOMER_PROMPT}'
    )
    send_or_edit(
        bot, chat_id, text,
        inline([[('🎲 بدون نام، کانفیگ تصادفی', f'pw:rand:{plan.pk}')], [('بازگشت', f'pw:svc:{plan.service_id}')]]),
        call=call,
    )


def resolve_names(user: TelegramUser, panel, raw: str) -> tuple[str, str]:
    """Turn one typed line into a customer label and a config username.

    One question instead of two: a partner types their customer's name, and if
    that name happens to be usable as a config username it is used, which is
    what a partner typing `reza2` expects. A Persian name, or one already taken,
    still becomes the label and the config gets a generated name — so the
    partner never hits a dead end over naming rules they did not ask about.
    """
    label = (raw or '').strip()[:120]
    try:
        candidate = validate_client_name(label)
        if not client_name_taken(candidate, panel):
            return label, candidate
    except ClientNameError:
        pass
    return label, unique_client_name(user, panel)


def show_confirm(bot: TeleBot, partner: Partner, user: TelegramUser, chat_id: int):
    data = user.temp_data or {}
    plan = Plan.objects.select_related('service', 'service__panel').filter(pk=data.get('pw_plan'), is_active=True).first()
    if not plan:
        reset(user)
        bot.send_message(chat_id, 'این پلن دیگر در دسترس نیست.', reply_markup=main_reply_keyboard())
        return

    quote = price_quote(partner, plan)
    label = data.get('pw_label') or ''
    client_name = data.get('pw_client') or ''

    lines = [
        '🧾 <b>تایید سفارش</b>',
        '',
        f'📌 {plan.name} — {plan.service.name}',
        f'مدت: {days_text(plan.duration_days)} | حجم: {traffic_text(plan.traffic_gb)}',
        f'👤 مشتری: {label or "بدون نام"}',
        f'🔖 نام کانفیگ: <code>{client_name}</code>',
        '',
        f'💰 مبلغ: <b>{toman(quote.final_toman)}</b>',
    ]

    if partner.billing_mode == Partner.BillingMode.CREDIT:
        invoice = partner.open_invoice()
        if invoice:
            lines += [
                '',
                f'به فاکتور {invoice.number} اضافه می‌شود.',
                f'سررسید: {jalali.format_datetime(invoice.due_at)}',
                f'جمع فاکتور بعد از این سفارش: {toman(Decimal(invoice.total_toman) + quote.final_toman)}',
            ]
        else:
            due = timezone.now() + timezone.timedelta(days=partner.billing_cycle_days)
            lines += [
                '',
                'با این سفارش، دوره تسویه جدید شروع می‌شود.',
                f'سررسید: {jalali.format_datetime(due)}',
            ]
        lines.append('\nکانفیگ بلافاصله ساخته و برای شما ارسال می‌شود.')
    else:
        partner.user.refresh_from_db()
        balance = partner.user.wallet_balance_toman
        lines += ['', f'💳 موجودی کیف پول: {toman(balance)}']
        if balance < quote.final_toman:
            lines.append(f'⚠️ کسری: {toman(quote.final_toman - balance)} — ابتدا کیف پول را شارژ کنید.')
            bot.send_message(
                chat_id, '\n'.join(lines),
                reply_markup=inline([[('💳 شارژ کیف پول', 'wallet')], [('بازگشت', 'pw:home')]]),
            )
            return
        lines.append('\nمبلغ از کیف پول کسر و کانفیگ ساخته می‌شود.')

    bot.send_message(
        chat_id, '\n'.join(lines),
        reply_markup=inline([[('✅ ثبت سفارش', 'pw:ok')], [('انصراف', 'pw:home')]]),
    )


def reset(user: TelegramUser) -> None:
    user.state = ''
    user.temp_data = {}
    user.save(update_fields=['state', 'temp_data', 'updated_at'])


def place_order(bot: TeleBot, partner: Partner, user: TelegramUser, chat_id: int):
    """Create, charge and provision, in the order that survives a failure.

    The charge is recorded before the panel is called, because the credit check
    has to happen under a lock; if provisioning then fails the charge is taken
    straight back off the invoice, so a partner is never billed for a config
    that does not exist.
    """
    data = user.temp_data or {}
    plan = Plan.objects.select_related('service', 'service__panel').filter(pk=data.get('pw_plan'), is_active=True).first()
    if not plan:
        reset(user)
        bot.send_message(chat_id, 'این پلن دیگر در دسترس نیست.', reply_markup=main_reply_keyboard())
        return
    if not partner.may_sell(plan):
        reset(user)
        bot.send_message(chat_id, 'این پلن برای شما مجاز نیست.', reply_markup=main_reply_keyboard())
        return

    quote = price_quote(partner, plan)
    label = data.get('pw_label') or ''
    client_name = data.get('pw_client') or unique_client_name(user, plan.service.panel)
    on_credit = partner.billing_mode == Partner.BillingMode.CREDIT

    try:
        partner_billing.check_can_order(partner, quote.final_toman)
    except PartnerBillingError as exc:
        reset(user)
        bot.send_message(chat_id, f'⛔️ {exc}', reply_markup=main_reply_keyboard())
        return

    order = None
    item = None
    try:
        if on_credit:
            order = create_partner_order(
                partner, plan, quote=quote, customer_label=label,
                client_name=client_name, on_credit=True,
            )
            item = partner_billing.charge_to_invoice(
                partner,
                kind=PartnerInvoiceItem.Kind.NEW,
                title=f'{plan.service.name} / {plan.name} — مشتری: {label or client_name}',
                amount_toman=quote.final_toman,
                amount_usd=quote.final_usd,
                order=order,
            )
        else:
            order = charge_wallet_order(partner, plan, quote, label, client_name)
        order = provision_order(order)
    except PartnerBillingError as exc:
        if order is not None:
            order.status = Order.Status.FAILED
            order.save(update_fields=['status', 'updated_at'])
        reset(user)
        bot.send_message(chat_id, f'⛔️ {exc}', reply_markup=main_reply_keyboard())
        return
    except Exception as exc:  # noqa: BLE001
        rollback_failed_order(order, item, str(exc))
        reset(user)
        bot.send_message(
            chat_id,
            f'❌ سفارش ثبت نشد و مبلغی از شما گرفته نشد.\n\nخطا: {exc}',
            reply_markup=main_reply_keyboard(),
        )
        return

    reset(user)
    send_delivery(bot, chat_id, order)
    notify_operator(
        bot,
        f'🤝 <b>سفارش همکار</b>\n\n'
        f'همکار: {partner.display_name}\n'
        f'مشتری: {label or "-"}\n'
        f'پلن: {plan.service.name} / {plan.name}\n'
        f'مبلغ: {toman(quote.final_toman)}\n'
        f'نوع: {"اعتباری" if on_credit else "پرداخت فوری"}',
    )


def charge_wallet_order(partner: Partner, plan: Plan, quote, label: str, client_name: str) -> Order:
    """A prepaid partner buys out of their own wallet balance.

    The config is created only after the money is taken, which is what "پرداخت
    فوری" asks for. Topping the wallet up goes through the shop's existing card
    and crypto flows unchanged.
    """
    from sales.models import WalletTransaction

    with transaction.atomic():
        user = TelegramUser.objects.select_for_update().get(pk=partner.user_id)
        if user.wallet_balance_toman < quote.final_toman:
            raise PartnerBillingError('موجودی کیف پول کافی نیست.')
        order = create_partner_order(
            partner, plan, quote=quote, customer_label=label,
            client_name=client_name, on_credit=False,
        )
        user.wallet_balance_toman -= quote.final_toman
        user.save(update_fields=['wallet_balance_toman', 'updated_at'])
        WalletTransaction.objects.create(
            user=user,
            kind=WalletTransaction.Kind.DEBIT,
            amount_toman=quote.final_toman,
            balance_after_toman=user.wallet_balance_toman,
            order=order,
            description=f'فروش همکاری — {plan.name}',
        )
        return order


def rollback_failed_order(order: Order | None, item: PartnerInvoiceItem | None, reason: str) -> None:
    """Undo a partner order whose config never got made."""
    if item is not None:
        partner_billing.cancel_item(item, f'ساخت کانفیگ ناموفق: {reason}'[:200])
    if order is None:
        return
    if order.source != Order.Source.PARTNER_CREDIT and order.amount_toman > 0:
        from sales.models import WalletTransaction

        with transaction.atomic():
            user = TelegramUser.objects.select_for_update().get(pk=order.user_id)
            user.wallet_balance_toman += Decimal(order.amount_toman)
            user.save(update_fields=['wallet_balance_toman', 'updated_at'])
            WalletTransaction.objects.create(
                user=user,
                kind=WalletTransaction.Kind.REFUND,
                amount_toman=order.amount_toman,
                balance_after_toman=user.wallet_balance_toman,
                order=order,
                description=f'برگشت وجه سفارش ناموفق: {reason}'[:255],
            )
    order.status = Order.Status.FAILED
    order.admin_note = (order.admin_note or '') + f'\nسفارش همکار ناموفق: {reason}'
    order.save(update_fields=['status', 'admin_note', 'updated_at'])


# ─────────────────────────────────────────────────────────────────────────────
# کانفیگ‌های همکار
# ─────────────────────────────────────────────────────────────────────────────


def partner_orders(partner: Partner):
    return (
        Order.objects.filter(partner=partner, status=Order.Status.PROVISIONED)
        .select_related('plan', 'service')
        .order_by('-created_at')
    )


def order_state_mark(order: Order) -> str:
    if order.suspended_at:
        return '⛔️ غیرفعال بابت بدهی'
    reason = order.ended_reason()
    return f'⛔️ {reason}' if reason else '✅ فعال'


def show_orders(bot: TeleBot, partner: Partner, chat_id: int, call=None):
    orders = partner_orders(partner)[:20]
    if not orders:
        send_or_edit(
            bot, chat_id, 'هنوز کانفیگی نفروخته‌اید.',
            inline([[('🛒 سفارش جدید', 'pw:new')], [('بازگشت', 'pw:home')]]), call=call,
        )
        return
    lines = ['📦 <b>کانفیگ‌های شما</b>', '']
    rows = []
    for order in orders:
        who = order.customer_label or order.xui_client_email or f'#{order.pk}'
        expiry = jalali.format_date(order.expires_at) if order.expires_at else '-'
        lines.append(f'• {who} — {order.plan.name} — انقضا {expiry} — {order_state_mark(order)}')
        rows.append([(f'{who} ({order_state_mark(order)})', f'pw:cfg:{order.pk}')])
    rows.append([('بازگشت', 'pw:home')])
    send_or_edit(bot, chat_id, '\n'.join(lines), inline(rows), call=call)


def show_order(bot: TeleBot, partner: Partner, chat_id: int, order_id: int, call=None):
    order = partner_orders(partner).filter(pk=order_id).first()
    if not order:
        send_or_edit(bot, chat_id, 'این کانفیگ پیدا نشد.', inline([[('بازگشت', 'pw:list')]]), call=call)
        return
    lines = [
        f'🔖 <b>{order.customer_label or order.xui_client_email}</b>',
        '',
        f'پلن: {order.service.name} / {order.plan.name}',
        f'نام کانفیگ: <code>{order.xui_client_email}</code>',
        f'انقضا: {jalali.format_datetime(order.expires_at) if order.expires_at else "-"}',
        f'حجم: {traffic_text(order.plan.traffic_gb)}',
        f'وضعیت: {order_state_mark(order)}',
        f'مبلغ پرداختی شما: {toman(order.amount_toman)}',
    ]
    if order.suspended_at:
        lines += ['', '⛔️ این کانفیگ به دلیل فاکتور پرداخت‌نشده غیرفعال شده است. '
                      'با تسویه فاکتور، دوباره فعال می‌شود.']
    rows = [
        [('🔁 تمدید', f'pw:renew:{order.pk}'), ('📤 ارسال مجدد', f'pw:resend:{order.pk}')],
        [('🗑 حذف کانفیگ', f'pw:del:{order.pk}')],
        [('بازگشت', 'pw:list')],
    ]
    send_or_edit(bot, chat_id, '\n'.join(lines), inline(rows), call=call)


def show_renew_plans(bot: TeleBot, partner: Partner, chat_id: int, order_id: int, call=None):
    order = partner_orders(partner).filter(pk=order_id).first()
    if not order:
        send_or_edit(bot, chat_id, 'این کانفیگ پیدا نشد.', inline([[('بازگشت', 'pw:list')]]), call=call)
        return
    plans = partner.available_plans().filter(service=order.service).order_by('sort_order', 'price_usd')
    if not plans:
        send_or_edit(bot, chat_id, 'پلنی برای تمدید در دسترس نیست.', inline([[('بازگشت', f'pw:cfg:{order.pk}')]]), call=call)
        return
    rows = [
        [(f'{p.name} — {toman(partner.final_price_toman(p))}', f'pw:rnwp:{order.pk}:{p.pk}')]
        for p in plans
    ]
    rows.append([('بازگشت', f'pw:cfg:{order.pk}')])
    send_or_edit(bot, chat_id, 'پلن تمدید را انتخاب کنید:', inline(rows), call=call)


def do_renew(bot: TeleBot, partner: Partner, chat_id: int, order_id: int, plan_id: int, call=None):
    order = partner_orders(partner).filter(pk=order_id).first()
    plan = partner.available_plans().filter(pk=plan_id, service=order.service if order else None).first()
    if not order or not plan:
        send_or_edit(bot, chat_id, 'تمدید ممکن نیست.', inline([[('بازگشت', 'pw:list')]]), call=call)
        return

    quote = price_quote(partner, plan)
    on_credit = partner.billing_mode == Partner.BillingMode.CREDIT
    try:
        partner_billing.check_can_order(partner, quote.final_toman)
        if on_credit:
            partner_billing.charge_to_invoice(
                partner,
                kind=PartnerInvoiceItem.Kind.RENEW,
                title=f'تمدید {plan.name} — {order.customer_label or order.xui_client_email}',
                amount_toman=quote.final_toman,
                amount_usd=quote.final_usd,
                order=order,
            )
            with transaction.atomic():
                locked = Order.objects.select_for_update().select_related('service', 'service__panel').get(pk=order.pk)
                extend_order(locked, plan, quote.final_toman, quote.final_usd)
        else:
            locked = renew_from_wallet(partner, order, plan, quote)
        # A renewed subscription is paid for again, so a suspension from an
        # older invoice no longer applies to it.
        if locked.suspended_at:
            locked.suspended_at = None
            locked.suspended_by_invoice = None
            locked.save(update_fields=['suspended_at', 'suspended_by_invoice', 'updated_at'])
        push_renewal_to_panel(locked)
    except PartnerBillingError as exc:
        send_or_edit(bot, chat_id, f'⛔️ {exc}', inline([[('بازگشت', f'pw:cfg:{order.pk}')]]), call=call)
        return
    except Exception as exc:  # noqa: BLE001
        send_or_edit(bot, chat_id, f'❌ تمدید انجام نشد.\n{exc}', inline([[('بازگشت', f'pw:cfg:{order.pk}')]]), call=call)
        return

    locked.refresh_from_db()
    send_or_edit(
        bot, chat_id,
        f'✅ تمدید شد.\nانقضای جدید: {jalali.format_datetime(locked.expires_at)}\n'
        f'مبلغ: {toman(quote.final_toman)}',
        inline([[('بازگشت', f'pw:cfg:{order.pk}')]]), call=call,
    )


def renew_from_wallet(partner: Partner, order: Order, plan: Plan, quote) -> Order:
    from sales.models import WalletTransaction

    with transaction.atomic():
        locked = Order.objects.select_for_update().select_related('service', 'service__panel').get(pk=order.pk)
        user = TelegramUser.objects.select_for_update().get(pk=partner.user_id)
        if user.wallet_balance_toman < quote.final_toman:
            raise PartnerBillingError(
                f'موجودی کیف پول کافی نیست.\nمبلغ تمدید: {toman(quote.final_toman)}\n'
                f'موجودی شما: {toman(user.wallet_balance_toman)}'
            )
        user.wallet_balance_toman -= quote.final_toman
        user.save(update_fields=['wallet_balance_toman', 'updated_at'])
        WalletTransaction.objects.create(
            user=user,
            kind=WalletTransaction.Kind.DEBIT,
            amount_toman=quote.final_toman,
            balance_after_toman=user.wallet_balance_toman,
            order=locked,
            description=f'تمدید همکاری — {plan.name}',
        )
        extend_order(locked, plan, quote.final_toman, quote.final_usd)
        return locked


def confirm_delete(bot: TeleBot, partner: Partner, chat_id: int, order_id: int, call=None):
    order = partner_orders(partner).filter(pk=order_id).first()
    if not order:
        send_or_edit(bot, chat_id, 'این کانفیگ پیدا نشد.', inline([[('بازگشت', 'pw:list')]]), call=call)
        return
    item = partner_billing.live_item_for_order(order)
    refundable = item is not None and partner_billing.refund_window_open(item)
    text = [
        f'🗑 <b>حذف کانفیگ {order.customer_label or order.xui_client_email}</b>',
        '',
        'کانفیگ از پنل حذف می‌شود و مشتری شما بلافاصله قطع خواهد شد. این کار برگشت‌پذیر نیست.',
        '',
    ]
    if refundable:
        text.append(f'✅ مبلغ {toman(item.amount_toman)} از فاکتور {item.invoice.number} برداشته می‌شود.')
    elif item is not None:
        hours = int(SiteSetting.get_solo().partner_delete_refund_hours or 0)
        text.append(
            f'⚠️ مهلت {fa_digits(hours)} ساعته برگشت وجه گذشته یا فاکتور تسویه شده است؛ '
            f'مبلغ {toman(item.amount_toman)} در فاکتور می‌ماند.'
        )
    else:
        text.append('⚠️ مبلغ این کانفیگ برگشت داده نمی‌شود.')
    send_or_edit(
        bot, chat_id, '\n'.join(text),
        inline([[('🗑 بله، حذف کن', f'pw:delok:{order.pk}')], [('انصراف', f'pw:cfg:{order.pk}')]]),
        call=call,
    )


def do_delete(bot: TeleBot, partner: Partner, chat_id: int, order_id: int, call=None):
    order = partner_orders(partner).filter(pk=order_id).select_related('service', 'service__panel').first()
    if not order:
        send_or_edit(bot, chat_id, 'این کانفیگ پیدا نشد.', inline([[('بازگشت', 'pw:list')]]), call=call)
        return

    item = partner_billing.live_item_for_order(order)
    refundable = item is not None and partner_billing.refund_window_open(item)

    try:
        delete_order_client(order)
    except Exception as exc:  # noqa: BLE001
        send_or_edit(
            bot, chat_id,
            f'❌ حذف از پنل انجام نشد و هیچ تغییری در فاکتور داده نشد.\n\n{exc}',
            inline([[('بازگشت', f'pw:cfg:{order.pk}')]]), call=call,
        )
        return

    order.status = Order.Status.CANCELLED
    order.admin_note = (order.admin_note or '') + f'\nحذف توسط همکار {partner.display_name}'
    order.save(update_fields=['status', 'admin_note', 'updated_at'])

    if refundable:
        partner_billing.cancel_item(item, 'حذف کانفیگ توسط همکار در مهلت برگشت')
        message = f'✅ کانفیگ حذف شد و مبلغ {toman(item.amount_toman)} از فاکتور برداشته شد.'
    elif item is not None:
        message = '✅ کانفیگ حذف شد. مهلت برگشت وجه گذشته بود، پس مبلغ در فاکتور باقی ماند.'
    else:
        message = '✅ کانفیگ حذف شد.'

    notify_operator(
        bot,
        f'🗑 <b>حذف کانفیگ توسط همکار</b>\n\n'
        f'همکار: {partner.display_name}\n'
        f'کانفیگ: {order.xui_client_email}\n'
        f'مشتری: {order.customer_label or "-"}\n'
        f'برگشت وجه: {"بله" if refundable else "خیر"}',
    )
    send_or_edit(bot, chat_id, message, inline([[('بازگشت', 'pw:list')]]), call=call)


# ─────────────────────────────────────────────────────────────────────────────
# فاکتور، بدهی، گزارش، حساب
# ─────────────────────────────────────────────────────────────────────────────


def show_invoices(bot: TeleBot, partner: Partner, chat_id: int, call=None):
    invoices = partner.invoices.order_by('-opened_at')[:15]
    if not invoices:
        send_or_edit(bot, chat_id, 'هنوز فاکتوری ندارید.', inline([[('بازگشت', 'pw:home')]]), call=call)
        return
    rows = []
    lines = ['🧾 <b>فاکتورهای شما</b>', '']
    for invoice in invoices:
        mark = {
            PartnerInvoice.Status.OPEN: '🟡 باز',
            PartnerInvoice.Status.OVERDUE: '⛔️ معوق',
            PartnerInvoice.Status.PAID: '✅ پرداخت‌شده',
            PartnerInvoice.Status.CANCELLED: '⚪️ لغوشده',
        }[invoice.status]
        lines.append(f'• {invoice.number} — {toman(invoice.total_toman)} — {mark}')
        rows.append([(f'{invoice.number} ({mark})', f'pw:invd:{invoice.pk}')])
    rows.append([('بازگشت', 'pw:home')])
    send_or_edit(bot, chat_id, '\n'.join(lines), inline(rows), call=call)


def show_invoice(bot: TeleBot, partner: Partner, chat_id: int, invoice_id: int, call=None):
    invoice = partner.invoices.filter(pk=invoice_id).first()
    if not invoice:
        send_or_edit(bot, chat_id, 'این فاکتور پیدا نشد.', inline([[('بازگشت', 'pw:inv')]]), call=call)
        return
    lines = [
        f'🧾 <b>فاکتور {invoice.number}</b>',
        '',
        f'شروع دوره: {jalali.format_datetime(invoice.opened_at)}',
        f'سررسید: {jalali.format_datetime(invoice.due_at)}',
        f'وضعیت: {invoice.get_status_display()}',
        '',
        '<b>ردیف‌ها:</b>',
    ]
    for item in invoice.items.filter(is_cancelled=False).order_by('created_at'):
        lines.append(f'• {item.title} — {toman(item.amount_toman)}')
    cancelled = invoice.items.filter(is_cancelled=True).count()
    if cancelled:
        lines.append(f'\n({fa_digits(cancelled)} ردیف لغو شده و در جمع حساب نشده است.)')
    lines += ['', f'💰 <b>جمع: {toman(invoice.total_toman)}</b>']
    if invoice.paid_at:
        lines.append(f'✅ پرداخت‌شده در {jalali.format_datetime(invoice.paid_at)}')

    rows = []
    if invoice.is_payable:
        rows.append([('💳 پرداخت فاکتور', f'pw:pay:{invoice.pk}')])
    rows.append([('بازگشت', 'pw:inv')])
    send_or_edit(bot, chat_id, '\n'.join(lines), inline(rows), call=call)


def show_payment_methods(bot: TeleBot, partner: Partner, chat_id: int, invoice_id: int, call=None):
    invoice = partner.invoices.filter(pk=invoice_id).first()
    if not invoice or not invoice.is_payable:
        send_or_edit(bot, chat_id, 'این فاکتور قابل پرداخت نیست.', inline([[('بازگشت', 'pw:inv')]]), call=call)
        return
    site = SiteSetting.get_solo()
    partner.user.refresh_from_db()
    rows = [[('💰 پرداخت از کیف پول', f'pw:payw:{invoice.pk}')]]
    if site.card_to_card_enabled:
        rows.append([('💳 کارت‌به‌کارت', f'pw:payc:{invoice.pk}')])
    rows.append([('🪙 پرداخت با کریپتو', f'pw:payo:{invoice.pk}')])
    rows.append([('بازگشت', f'pw:invd:{invoice.pk}')])
    send_or_edit(
        bot, chat_id,
        f'مبلغ فاکتور {invoice.number}: <b>{toman(invoice.total_toman)}</b>\n'
        f'موجودی کیف پول شما: {toman(partner.user.wallet_balance_toman)}\n\n'
        'روش پرداخت را انتخاب کنید:',
        inline(rows), call=call,
    )


def pay_invoice(bot: TeleBot, partner: Partner, user: TelegramUser, chat_id: int, invoice_id: int, method: str, call=None):
    invoice = partner.invoices.filter(pk=invoice_id).first()
    if not invoice or not invoice.is_payable:
        send_or_edit(bot, chat_id, 'این فاکتور قابل پرداخت نیست.', inline([[('بازگشت', 'pw:inv')]]), call=call)
        return
    amount = int(invoice.total_toman)

    if method == 'wallet':
        try:
            partner_billing.pay_invoice_from_wallet(invoice)
        except PartnerBillingError as exc:
            send_or_edit(
                bot, chat_id, f'⚠️ {exc}',
                inline([[('💳 شارژ کیف پول', 'wallet')], [('بازگشت', f'pw:pay:{invoice.pk}')]]), call=call,
            )
            return
        invoice.refresh_from_db()
        send_or_edit(
            bot, chat_id,
            f'✅ فاکتور {invoice.number} تسویه شد.\n\n'
            'کانفیگ‌هایی که به دلیل این فاکتور غیرفعال شده بودند، دوباره فعال شدند.',
            inline([[('بازگشت', 'pw:home')]]), call=call,
        )
        notify_operator(bot, f'✅ فاکتور {invoice.number} همکار {partner.display_name} از کیف پول تسویه شد.')
        return

    if method == 'card':
        site = SiteSetting.get_solo()
        if not site.card_to_card_enabled:
            send_or_edit(bot, chat_id, 'کارت‌به‌کارت فعلاً غیرفعال است.', inline([[('بازگشت', 'pw:inv')]]), call=call)
            return
        request = create_card_request(user, amount)
        request.partner_invoice = invoice
        request.save(update_fields=['partner_invoice', 'updated_at'])
        user.state = 'awaiting_card_receipt'
        user.temp_data = {'card_request_id': request.pk}
        user.save(update_fields=['state', 'temp_data', 'updated_at'])
        from sales.services.botcore import card_invoice_text

        send_or_edit(
            bot, chat_id,
            f'🧾 پرداخت فاکتور {invoice.number}\n\n' + card_invoice_text(site, request),
            cancel_keyboard(), call=call,
        )
        return

    try:
        payment = create_oxapay_payment(user, amount)
        payment.purpose = payment.Purpose.PARTNER_INVOICE
        payment.partner_invoice = invoice
        payment.save(update_fields=['purpose', 'partner_invoice', 'updated_at'])
    except OxaPayError as exc:
        send_or_edit(bot, chat_id, f'خطا در ساخت لینک پرداخت: {exc}', inline([[('بازگشت', 'pw:inv')]]), call=call)
        return
    send_or_edit(
        bot, chat_id,
        f'🪙 پرداخت فاکتور {invoice.number}\nمبلغ: {toman(amount)}\n\n'
        'بعد از پرداخت، فاکتور خودکار تسویه و کانفیگ‌ها فعال می‌شوند.\n\n'
        f'{payment.payment_url}',
        inline([[('بازگشت', 'pw:inv')]]), call=call,
    )


def show_debt(bot: TeleBot, partner: Partner, chat_id: int, call=None):
    debt = partner.current_debt_toman()
    remaining = partner.remaining_credit_toman()
    lines = ['💰 <b>بدهی و اعتبار</b>', '']
    lines.append(f'بدهی فعلی: <b>{toman(debt)}</b>')
    if partner.credit_limit_toman <= 0:
        lines.append('سقف اعتبار: نامحدود')
    else:
        lines.append(f'سقف اعتبار: {toman(partner.credit_limit_toman)}')
        lines.append(f'اعتبار باقی‌مانده: {toman(max(Decimal("0"), remaining or Decimal("0")))}')

    overdue = partner.overdue_invoice()
    invoice = partner.open_invoice()
    if overdue is not None:
        lines += ['', f'⛔️ فاکتور {overdue.number} سررسید شده است. تا تسویه، امکان سفارش جدید ندارید.']
    elif invoice is not None:
        lines += ['', f'🧾 فاکتور جاری {invoice.number} — سررسید {jalali.format_datetime(invoice.due_at)}']
    else:
        lines += ['', '✅ فاکتور بازی ندارید.']

    rows = [[('🧾 فاکتورها', 'pw:inv')], [('بازگشت', 'pw:home')]]
    send_or_edit(bot, chat_id, '\n'.join(lines), inline(rows), call=call)


def show_report(bot: TeleBot, partner: Partner, chat_id: int, call=None):
    from django.db.models import Count, Sum

    orders = Order.objects.filter(partner=partner).exclude(status=Order.Status.FAILED)
    now = timezone.now()
    month_start = now - timezone.timedelta(days=30)

    total = orders.aggregate(n=Count('id'), s=Sum('amount_toman'))
    recent = orders.filter(created_at__gte=month_start).aggregate(n=Count('id'), s=Sum('amount_toman'))
    active = orders.filter(status=Order.Status.PROVISIONED, suspended_at__isnull=True).count()

    lines = [
        '📊 <b>گزارش فروش شما</b>',
        '',
        f'کل فروش: {fa_digits(total["n"] or 0)} سفارش — {toman(total["s"] or 0)}',
        f'۳۰ روز اخیر: {fa_digits(recent["n"] or 0)} سفارش — {toman(recent["s"] or 0)}',
        f'کانفیگ فعال: {fa_digits(active)}',
    ]

    by_plan = (
        orders.values('plan__name')
        .annotate(n=Count('id'), s=Sum('amount_toman'))
        .order_by('-n')[:8]
    )
    if by_plan:
        lines += ['', '<b>به تفکیک پلن:</b>']
        for row in by_plan:
            lines.append(f'• {row["plan__name"]}: {fa_digits(row["n"])} — {toman(row["s"] or 0)}')

    send_or_edit(bot, chat_id, '\n'.join(lines), inline([[('بازگشت', 'pw:home')]]), call=call)


def show_account(bot: TeleBot, partner: Partner, chat_id: int, call=None):
    partner.user.refresh_from_db()
    lines = [
        '👤 <b>اطلاعات حساب همکاری</b>',
        '',
        f'نام: {partner.display_name}',
        f'Chat ID: <code>{partner.user.chat_id}</code>',
        f'نوع همکاری: {partner.get_billing_mode_display()}',
    ]
    if partner.billing_mode == Partner.BillingMode.CREDIT:
        lines.append(f'مهلت پرداخت: {fa_digits(partner.billing_cycle_days)} روز')
        lines.append(
            'سقف اعتبار: نامحدود' if partner.credit_limit_toman <= 0
            else f'سقف اعتبار: {toman(partner.credit_limit_toman)}'
        )
    else:
        lines.append(f'موجودی کیف پول: {toman(partner.user.wallet_balance_toman)}')
    if partner.discount_percent:
        lines.append(f'تخفیف همکاری: {fa_digits(partner.discount_percent)}٪ روی قیمت همکاری')
    lines.append(f'تاریخ عضویت: {jalali.format_date(partner.created_at)}')

    plans = partner.available_plans().select_related('service')[:12]
    if plans:
        lines += ['', '<b>قیمت‌های شما:</b>']
        for plan in plans:
            lines.append(f'• {plan.service.name} / {plan.name}: {toman(partner.final_price_toman(plan))}')

    send_or_edit(bot, chat_id, '\n'.join(lines), inline([[('بازگشت', 'pw:home')]]), call=call)


# ─────────────────────────────────────────────────────────────────────────────
# مسیریابی
# ─────────────────────────────────────────────────────────────────────────────


def handle_text(bot: TeleBot, user: TelegramUser, message) -> bool:
    """Handle a partner state. Returns True when the message was consumed."""
    state = user.state or ''
    if state == ST_REQUEST:
        submit_request(bot, user, message.chat.id, message.text or '')
        return True

    if state == ST_CUSTOMER:
        partner = get_partner(user)
        if not partner:
            reset(user)
            return False
        plan = Plan.objects.select_related('service', 'service__panel').filter(
            pk=(user.temp_data or {}).get('pw_plan'), is_active=True
        ).first()
        if not plan:
            reset(user)
            bot.send_message(message.chat.id, 'این پلن دیگر در دسترس نیست.', reply_markup=main_reply_keyboard())
            return True
        label, client_name = resolve_names(user, plan.service.panel, message.text or '')
        user.state = ''
        user.temp_data = {**(user.temp_data or {}), 'pw_label': label, 'pw_client': client_name}
        user.save(update_fields=['state', 'temp_data', 'updated_at'])
        show_confirm(bot, partner, user, message.chat.id)
        return True

    return False


def handle_callback(bot: TeleBot, user: TelegramUser, call) -> bool:
    """Route a `pw:` callback. Returns True when it was ours to handle."""
    data = call.data or ''
    if not data.startswith('pw:'):
        return False

    chat_id = call.message.chat.id
    if not program_enabled():
        edit_or_send(bot, call, 'این بخش فعلاً فعال نیست.')
        return True

    action = data[3:]

    # The application form is the one screen open to people who are not partners.
    if action == 'req':
        if Partner.objects.filter(user=user).exists():
            handle_work(bot, user, chat_id, call=call)
            return True
        if not SiteSetting.get_solo().partner_request_enabled:
            edit_or_send(bot, call, 'پذیرش همکار جدید فعلاً بسته است.')
            return True
        start_request(bot, user, chat_id, call=call)
        return True

    partner = get_partner(user)
    if partner is None:
        # Guessing a callback gets the same answer as typing the command.
        handle_work(bot, user, chat_id, call=call)
        return True

    if action == 'home':
        show_panel(bot, partner, chat_id, call=call)
    elif action == 'new':
        show_services(bot, partner, chat_id, call=call)
    elif action.startswith('svc:'):
        show_plans(bot, partner, chat_id, int(action.split(':')[1]), call=call)
    elif action.startswith('plan:'):
        plan = partner.available_plans().filter(pk=int(action.split(':')[1])).select_related('service').first()
        if plan is None:
            edit_or_send(bot, call, 'این پلن برای شما مجاز نیست.', inline([[('بازگشت', 'pw:new')]]))
        else:
            ask_customer(bot, partner, user, chat_id, plan, call=call)
    elif action.startswith('rand:'):
        plan = partner.available_plans().filter(pk=int(action.split(':')[1])).select_related('service', 'service__panel').first()
        if plan is None:
            edit_or_send(bot, call, 'این پلن برای شما مجاز نیست.', inline([[('بازگشت', 'pw:new')]]))
            return True
        user.state = ''
        user.temp_data = {
            **(user.temp_data or {}),
            'pw_plan': plan.pk,
            'pw_label': '',
            'pw_client': unique_client_name(user, plan.service.panel),
        }
        user.save(update_fields=['state', 'temp_data', 'updated_at'])
        show_confirm(bot, partner, user, chat_id)
    elif action == 'ok':
        place_order(bot, partner, user, chat_id)
    elif action == 'list':
        show_orders(bot, partner, chat_id, call=call)
    elif action.startswith('cfg:'):
        show_order(bot, partner, chat_id, int(action.split(':')[1]), call=call)
    elif action.startswith('renew:'):
        show_renew_plans(bot, partner, chat_id, int(action.split(':')[1]), call=call)
    elif action.startswith('rnwp:'):
        _, order_id, plan_id = action.split(':')
        do_renew(bot, partner, chat_id, int(order_id), int(plan_id), call=call)
    elif action.startswith('resend:'):
        order = partner_orders(partner).filter(pk=int(action.split(':')[1])).first()
        if order is None:
            edit_or_send(bot, call, 'این کانفیگ پیدا نشد.', inline([[('بازگشت', 'pw:list')]]))
        else:
            send_delivery(bot, chat_id, order)
    elif action.startswith('del:'):
        confirm_delete(bot, partner, chat_id, int(action.split(':')[1]), call=call)
    elif action.startswith('delok:'):
        do_delete(bot, partner, chat_id, int(action.split(':')[1]), call=call)
    elif action == 'inv':
        show_invoices(bot, partner, chat_id, call=call)
    elif action.startswith('invd:'):
        show_invoice(bot, partner, chat_id, int(action.split(':')[1]), call=call)
    elif action.startswith('pay:'):
        show_payment_methods(bot, partner, chat_id, int(action.split(':')[1]), call=call)
    elif action.startswith('payw:'):
        pay_invoice(bot, partner, user, chat_id, int(action.split(':')[1]), 'wallet', call=call)
    elif action.startswith('payc:'):
        pay_invoice(bot, partner, user, chat_id, int(action.split(':')[1]), 'card', call=call)
    elif action.startswith('payo:'):
        pay_invoice(bot, partner, user, chat_id, int(action.split(':')[1]), 'oxapay', call=call)
    elif action == 'debt':
        show_debt(bot, partner, chat_id, call=call)
    elif action == 'report':
        show_report(bot, partner, chat_id, call=call)
    elif action == 'me':
        show_account(bot, partner, chat_id, call=call)
    else:
        show_panel(bot, partner, chat_id, call=call)
    return True
