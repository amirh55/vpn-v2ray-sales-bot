from __future__ import annotations

import sys
import threading
import time
import uuid
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from telebot import TeleBot, types

from sales.models import (
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
)
from sales.services.cardpay import create_request as create_card_request
from sales.services.linking import LinkingError, link_config, refresh_usage, usage_text
from sales.services.formatting import days_text, fa_digits, parse_toman, toman, traffic_text, usd
from sales.services.oxapay import OxaPayError, create_invoice, toman_to_usd
from sales.services.provisioning import create_order_from_wallet, provision_order, renew_order_from_wallet

# Telegram gives no control over button colour, so the coloured squares stand
# in for it: green for buying, blue for wallet and renewal.
BTN_NEW = '🟢 خرید اشتراک جدید'
BTN_RENEW = '🔵 تمدید اشتراک'
BTN_WALLET = '🔵 کیف پول + شارژ'
BTN_SERVICES = '📦 سرویس‌های من'
BTN_ADD = '➕ افزودن اشتراک قدیمی'
BTN_TUTORIAL = '📚 آموزش اتصال'
BTN_CONTACT = '☎️ ارتباط با ما'
BTN_CANCEL = 'لغو و بازگشت'

MAIN_TEXT_ACTIONS = {
    BTN_NEW: 'new',
    BTN_RENEW: 'renew',
    BTN_WALLET: 'wallet',
    BTN_SERVICES: 'services',
    BTN_ADD: 'addservice',
    BTN_TUTORIAL: 'tutorial',
    BTN_CONTACT: 'contact',
    '/new': 'new',
    '/renew': 'renew',
    '/wallet': 'wallet',
    '/services': 'services',
    '/addservice': 'addservice',
    '/tutorial': 'tutorial',
    '/contact': 'contact',
}


def inline(rows: list[list[tuple[str, str]]]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    for row in rows:
        kb.row(*[types.InlineKeyboardButton(text, callback_data=data) for text, data in row])
    return kb


def safe_answer_callback(bot: TeleBot, call, text: str | None = None) -> None:
    """Answer callback query without killing polling on expired Telegram queries."""
    try:
        bot.answer_callback_query(call.id, text=text)
    except Exception:
        # Telegram returns 400 when the callback is too old or was already answered.
        # This should not stop the whole bot polling loop.
        pass


def main_reply_keyboard() -> types.ReplyKeyboardMarkup:
    """Persistent Telegram keyboard shown in the chat input area.

    This keeps the main buttons in Telegram's menu/keyboard area instead of
    attaching them under the welcome message as inline buttons.
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    kb.row(BTN_NEW)
    kb.row(BTN_RENEW, BTN_WALLET)
    kb.row(BTN_SERVICES, BTN_ADD)
    kb.row(BTN_TUTORIAL, BTN_CONTACT)
    return kb


def cancel_keyboard() -> types.InlineKeyboardMarkup:
    return inline([[('لغو و بازگشت', 'cancel')]])


def home_inline_keyboard() -> types.InlineKeyboardMarkup:
    return inline([[('🏠 منوی اصلی', 'cancel')]])


def get_site() -> SiteSetting:
    return SiteSetting.get_solo()


def ensure_user_from_message(message) -> TelegramUser:
    user, _ = TelegramUser.objects.update_or_create(
        chat_id=message.chat.id,
        defaults={
            'username': message.from_user.username or '',
            'first_name': message.from_user.first_name or '',
            'last_name': message.from_user.last_name or '',
        },
    )
    return user


def ensure_user_from_call(call) -> TelegramUser:
    user, _ = TelegramUser.objects.update_or_create(
        chat_id=call.message.chat.id,
        defaults={
            'username': call.from_user.username or '',
            'first_name': call.from_user.first_name or '',
            'last_name': call.from_user.last_name or '',
        },
    )
    return user


def reset_user_state(user: TelegramUser):
    user.state = ''
    user.temp_data = {}
    user.save(update_fields=['state', 'temp_data', 'updated_at'])


def plan_text(plan: Plan) -> str:
    return (
        f'📌 <b>{plan.name}</b>\n'
        f'سرویس: {plan.service.name}\n'
        f'مدت: {days_text(plan.duration_days)}\n'
        f'حجم: {traffic_text(plan.traffic_gb)}\n'
        f'تعداد کاربر: {fa_digits(plan.user_limit)}\n\n'
        f'{price_block(plan)}'
    )


def price_block(plan: Plan) -> str:
    """Both prices side by side, with the crypto saving spelled out.

    The dollar price is set independently and lower, so customers are told
    plainly what paying in crypto saves them.
    """
    lines = [
        f'💳 پرداخت ریالی: {toman(plan.price_toman)}',
        f'🪙 پرداخت کریپتو: {usd(plan.price_usd)}',
    ]
    percent = plan.crypto_saving_percent()
    if percent > 0:
        lines.append('')
        lines.append(
            f'🎁 با پرداخت کریپتو حدود <b>{fa_digits(percent)}٪</b> ارزان‌تر می‌خرید '
            f'(حدود {toman(plan.crypto_saving_toman())} کمتر).'
        )
    return '\n'.join(lines)


def send_main_menu(bot: TeleBot, chat_id: int):
    site = get_site()
    text = (
        f'سلام 🌿\n'
        f'به <b>{site.title}</b> خوش آمدید.\n'
        f'دکمه‌های اصلی ربات از منوی پایین تلگرام در دسترس هستند.'
    )
    bot.send_message(chat_id, text, reply_markup=main_reply_keyboard())


def edit_or_send(bot: TeleBot, call, text: str, kb=None):
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=kb, disable_web_page_preview=True)


def send_or_edit(bot: TeleBot, chat_id: int, text: str, kb=None, call=None):
    if call is not None:
        edit_or_send(bot, call, text, kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)


def create_oxapay_payment(
    user: TelegramUser,
    amount_toman: int,
    pending_plan: Plan | None = None,
    auto_purchase: bool = False,
    amount_usd: Decimal | None = None,
) -> Payment:
    site = get_site()
    # Plans carry their own dollar price; only wallet top-ups need converting.
    if amount_usd is None:
        amount_usd = toman_to_usd(Decimal(amount_toman), site.dollar_rate_toman)
    payment = Payment.objects.create(
        user=user,
        provider=Payment.Provider.OXAPAY,
        purpose=Payment.Purpose.WALLET_TOPUP,
        amount_toman=Decimal(amount_toman),
        amount_usd=amount_usd,
        order_id=f'PAY-{uuid.uuid4().hex[:14].upper()}',
        pending_plan=pending_plan,
        auto_purchase_after_paid=auto_purchase,
    )
    return create_invoice(payment)


def refund_order(order: Order, reason: str):
    with transaction.atomic():
        user = TelegramUser.objects.select_for_update().get(pk=order.user.pk)
        user.wallet_balance_toman += Decimal(order.amount_toman)
        user.save(update_fields=['wallet_balance_toman', 'updated_at'])
        WalletTransaction.objects.create(
            user=user,
            kind=WalletTransaction.Kind.REFUND,
            amount_toman=order.amount_toman,
            balance_after_toman=user.wallet_balance_toman,
            order=order,
            description=reason,
        )
        order.status = Order.Status.FAILED
        order.admin_note = (order.admin_note or '') + f'\nRefund: {reason}'
        order.save(update_fields=['status', 'admin_note', 'updated_at'])


def send_delivery(bot: TeleBot, chat_id: int, order: Order):
    text = f'✅ <b>اشتراک شما آماده است.</b>\n\n{plan_text(order.plan)}\n'
    if order.expires_at:
        text += f'\nتاریخ انقضا: {fa_digits(timezone.localtime(order.expires_at).strftime("%Y-%m-%d %H:%M"))}\n'
    if order.config_link:
        text += f'\n🔗 <b>لینک کانفیگ:</b>\n<code>{order.config_link}</code>\n'
    if order.subscription_link:
        text += f'\n🔄 <b>لینک Subscription:</b>\n<code>{order.subscription_link}</code>\n'
    if not order.config_link and not order.subscription_link:
        text += '\n⚠️ لینک کانفیگ/سابسکریپشن ساخته نشد. قالب لینک را در پنل مدیریت سرویس تنظیم کنید.'
    bot.send_message(chat_id, text, reply_markup=main_reply_keyboard(), disable_web_page_preview=True)
    if order.qr_image:
        try:
            with open(order.qr_image.path, 'rb') as fh:
                bot.send_photo(chat_id, fh, caption='QR Code اشتراک')
        except Exception:
            pass


def show_new_services(bot: TeleBot, chat_id: int, call=None):
    services = Service.objects.filter(is_active=True, plans__is_active=True).distinct().order_by('sort_order', 'name')
    rows = [[(f'🟢 {s.name}', f'svc:{s.pk}')] for s in services]
    rows.append([('بازگشت', 'cancel')])
    send_or_edit(bot, chat_id, 'سرویس مورد نظر را انتخاب کنید:', inline(rows), call=call)


def show_wallet(bot: TeleBot, user: TelegramUser, chat_id: int, call=None):
    """Step one of top-up: pick how much. The method is chosen next."""
    user.refresh_from_db()
    rows = [
        [('۱۰۰ هزار', 'topup:100000'), ('۵۰۰ هزار', 'topup:500000')],
        [('۱ میلیون', 'topup:1000000'), ('۲ میلیون', 'topup:2000000')],
        [('✏️ مبلغ دلخواه', 'topup_custom')],
        [('بازگشت', 'cancel')],
    ]
    send_or_edit(
        bot,
        chat_id,
        f'💳 موجودی کیف پول شما: {toman(user.wallet_balance_toman)}\n\nمبلغ شارژ را انتخاب کنید:',
        inline(rows),
        call=call,
    )


def show_topup_methods(bot: TeleBot, chat_id: int, amount: int, call=None):
    """Step two: pick how to pay the chosen amount."""
    site_now = get_site()
    rows = [[('🪙 پرداخت با کریپتو', f'payw:crypto:{amount}')]]
    if site_now.card_to_card_enabled:
        rows.append([('💳 کارت‌به‌کارت', f'payw:card:{amount}')])
    rows.append([('بازگشت', 'wallet')])
    send_or_edit(
        bot,
        chat_id,
        f'مبلغ شارژ: <b>{toman(amount)}</b>\n\nروش پرداخت را انتخاب کنید:',
        inline(rows),
        call=call,
    )


def show_my_services(bot: TeleBot, user: TelegramUser, chat_id: int, call=None):
    orders = Order.objects.filter(user=user, status=Order.Status.PROVISIONED).select_related('plan', 'service').order_by('-created_at')[:10]
    linked = LinkedService.objects.filter(user=user).select_related('panel').order_by('-created_at')[:10]
    if not orders and not linked:
        send_or_edit(
            bot,
            chat_id,
            'هنوز سرویسی ندارید.\n\n'
            f'اگر کانفیگی دارید که از جای دیگری تهیه کرده‌اید، با «{BTN_ADD}» می‌توانید آن را اینجا اضافه کنید '
            'و حجم و زمان باقی‌مانده‌اش را ببینید.',
            home_inline_keyboard(),
            call=call,
        )
        return

    rows = []
    text = '📦 سرویس‌های شما:\n\n'
    for o in orders:
        exp = fa_digits(timezone.localtime(o.expires_at).strftime('%Y-%m-%d')) if o.expires_at else 'بدون تاریخ'
        text += f'#{fa_digits(o.pk)} - {o.service.name} / {o.plan.name} / انقضا: {exp}\n'
        rows.append([(f'ارسال مجدد لینک #{o.pk}', f'resend:{o.pk}')])

    if linked:
        text += '\n➕ سرویس‌های افزوده‌شده:\n'
        for item in linked:
            text += f'• {item.display_name()}\n'
            rows.append([(f'📊 مصرف {item.display_name()}', f'lusage:{item.pk}')])

    rows.append([('بازگشت', 'cancel')])
    send_or_edit(bot, chat_id, text, inline(rows), call=call)


def operator_chat_ids() -> list[str]:
    """Every chat that should receive operator alerts, without repeats."""
    site = get_site()
    ids = []
    for value in (site.support_chat_id, site.admin_chat_id):
        value = (value or '').strip()
        if value and value not in ids:
            ids.append(value)
    return ids


def notify_operator(bot: TeleBot, text: str, photo_file_id: str | None = None) -> None:
    """Alert the shop owner in Telegram so the panel need not be watched.

    Delivery failures are printed rather than swallowed: a wrong chat id is
    otherwise invisible and the owner simply never hears about new messages.
    """
    targets = operator_chat_ids()
    if not targets:
        print(
            'هشدار: «چت آیدی پشتیبانی» و «چت آیدی مدیر» هر دو خالی هستند، '
            'پس اعلان پیام جدید برای شما ارسال نشد. '
            'در ربات دستور /id را بزنید تا شناسه‌تان را ببینید و در پنل ثبت کنید.',
            flush=True,
        )
        return
    for chat_id in targets:
        try:
            if photo_file_id:
                bot.send_photo(chat_id, photo_file_id, caption=text)
            else:
                bot.send_message(chat_id, text)
        except Exception as exc:  # noqa: BLE001
            print(f'ارسال اعلان به چت {chat_id} ناموفق بود: {exc}', flush=True)


def card_details_text(site: SiteSetting) -> str:
    """Card number in a code block so Telegram makes it tap-to-copy."""
    lines = []
    if site.card_number:
        lines.append(f'💳 شماره کارت:\n<code>{site.card_number}</code>')
    if site.card_holder_name:
        lines.append(f'👤 به نام: {site.card_holder_name}')
    if site.card_bank_name:
        lines.append(f'🏦 بانک: {site.card_bank_name}')
    if site.card_to_card_text:
        lines.append(site.card_to_card_text)
    return '\n'.join(lines) if lines else 'اطلاعات کارت هنوز در پنل ثبت نشده است.'


def card_invoice_text(site: SiteSetting, req: CardPaymentRequest) -> str:
    """Explain the exact figure to transfer and why it must be exact."""
    minutes = int(site.card_invoice_minutes or 30)
    header = ''
    if req.pending_plan_id:
        header = f'🛒 خرید: <b>{req.pending_plan.name}</b>\n\n'
    return (
        f'{header}{card_details_text(site)}\n\n'
        f'💰 مبلغ دقیق واریز:\n<code>{int(req.amount_toman)}</code> تومان\n\n'
        '⚠️ حتماً <b>دقیقاً همین مبلغ</b> را واریز کنید. '
        'رقم‌های آخر مخصوص سفارش شماست و با آن پرداختتان خودکار شناسایی می‌شود.\n'
        f'⏱ مهلت پرداخت: {fa_digits(minutes)} دقیقه\n\n'
        'بعد از واریز، عکس رسید یا شماره پیگیری را همین‌جا ارسال کنید.'
    )


def prompt_add_service(bot: TeleBot, user: TelegramUser, chat_id: int, call=None):
    user.state = 'awaiting_config_link'
    user.save(update_fields=['state', 'updated_at'])
    send_or_edit(
        bot,
        chat_id,
        '🔗 لینک کانفیگ خود را ارسال نمایید.\n\n'
        'لینکی که با <code>vless://</code> یا <code>vmess://</code> یا <code>trojan://</code> شروع می‌شود را '
        'کامل کپی کنید و همین‌جا بفرستید.\n\n'
        'اگر این کانفیگ روی سرورهای ما باشد، به حساب شما اضافه می‌شود و می‌توانید حجم و زمان باقی‌مانده را ببینید.',
        cancel_keyboard(),
        call=call,
    )


def show_linked_usage(bot: TeleBot, user: TelegramUser, chat_id: int, linked_id: int, call=None):
    linked = LinkedService.objects.filter(pk=linked_id, user=user).select_related('panel').first()
    if not linked:
        send_or_edit(bot, chat_id, 'این سرویس پیدا نشد.', home_inline_keyboard(), call=call)
        return
    try:
        info = refresh_usage(linked)
    except LinkingError as exc:
        send_or_edit(bot, chat_id, f'⚠️ {exc}', home_inline_keyboard(), call=call)
        return
    rows = [
        [('🔄 بروزرسانی', f'lusage:{linked.pk}')],
        [('🗑 حذف این سرویس', f'ldel:{linked.pk}')],
        [('بازگشت', 'services')],
    ]
    send_or_edit(bot, chat_id, usage_text(info, linked.display_name()), inline(rows), call=call)


def show_renew(bot: TeleBot, user: TelegramUser, chat_id: int, call=None):
    orders = Order.objects.filter(user=user, status=Order.Status.PROVISIONED).select_related('plan', 'service').order_by('-created_at')[:10]
    if not orders:
        send_or_edit(bot, chat_id, 'برای تمدید، ابتدا باید یک سرویس فعال داشته باشید.', home_inline_keyboard(), call=call)
        return
    rows = [[(f'{o.service.name} / {o.plan.name} #{o.pk}', f'reneword:{o.pk}')] for o in orders]
    rows.append([('بازگشت', 'cancel')])
    send_or_edit(bot, chat_id, 'کدام سرویس را تمدید می‌کنید؟', inline(rows), call=call)


def show_tutorial(bot: TeleBot, chat_id: int, call=None):
    site_now = get_site()
    send_or_edit(bot, chat_id, site_now.tutorial_text or 'آموزش اتصال هنوز تنظیم نشده است.', home_inline_keyboard() if call else None, call=call)


def show_contact(bot: TeleBot, user: TelegramUser, chat_id: int, call=None):
    site_now = get_site()
    user.state = 'awaiting_contact'
    user.temp_data = {}
    user.save(update_fields=['state', 'temp_data', 'updated_at'])
    send_or_edit(bot, chat_id, site_now.contact_intro_text, cancel_keyboard(), call=call)


def route_main_action(bot: TeleBot, user: TelegramUser, chat_id: int, action: str):
    site_now = get_site()
    if not site_now.is_shop_active and action not in ['contact', 'tutorial']:
        bot.send_message(chat_id, 'فروشگاه موقتاً غیرفعال است. لطفاً بعداً مراجعه کنید.', reply_markup=main_reply_keyboard())
        return
    if action == 'new':
        show_new_services(bot, chat_id)
    elif action == 'wallet':
        show_wallet(bot, user, chat_id)
    elif action == 'services':
        show_my_services(bot, user, chat_id)
    elif action == 'addservice':
        prompt_add_service(bot, user, chat_id)
    elif action == 'renew':
        show_renew(bot, user, chat_id)
    elif action == 'tutorial':
        show_tutorial(bot, chat_id)
    elif action == 'contact':
        show_contact(bot, user, chat_id)
    else:
        send_main_menu(bot, chat_id)


def notify_auto_approved_cards(bot: TeleBot):
    """Tell customers as soon as their transfer is recognised.

    The SMS webhook runs in the web process, so it cannot message anyone. This
    watcher lives in the bot process and picks up what the webhook settled.
    """
    while True:
        try:
            # Manual approvals from the panel need telling too, so this is not
            # limited to the automatic ones.
            pending = CardPaymentRequest.objects.filter(
                status=CardPaymentRequest.Status.APPROVED,
                notified_at__isnull=True,
            ).select_related('user', 'created_order', 'pending_plan')[:20]
            for req in pending:
                try:
                    if req.created_order_id:
                        bot.send_message(
                            req.user.chat_id,
                            '✅ واریز شما تایید شد و سرویس ساخته شد.',
                            reply_markup=main_reply_keyboard(),
                        )
                        send_delivery(bot, req.user.chat_id, req.created_order)
                    elif req.auto_purchase_after_paid and req.pending_plan_id:
                        bot.send_message(
                            req.user.chat_id,
                            '✅ واریز شما تایید و کیف پولتان شارژ شد، اما ساخت خودکار سرویس '
                            'انجام نشد. پشتیبانی پیگیری می‌کند و می‌توانید از «سرویس‌های من» '
                            'دوباره اقدام کنید.',
                            reply_markup=main_reply_keyboard(),
                        )
                    else:
                        bot.send_message(
                            req.user.chat_id,
                            '✅ واریز شما شناسایی شد.\n'
                            f'کیف پول شما به مبلغ {toman(req.amount_toman)} شارژ شد.',
                            reply_markup=main_reply_keyboard(),
                        )
                except Exception:
                    pass
                req.notified_at = timezone.now()
                req.save(update_fields=['notified_at', 'updated_at'])
        except Exception:
            pass
        time.sleep(10)


def process_queued_broadcasts(bot: TeleBot):
    while True:
        try:
            for bc in Broadcast.objects.filter(status=Broadcast.Status.QUEUED).order_by('created_at')[:5]:
                sent = failed = 0
                if bc.target_chat_id.strip():
                    targets = [bc.target_chat_id.strip()]
                else:
                    targets = list(TelegramUser.objects.filter(is_blocked=False).values_list('chat_id', flat=True))
                for chat_id in targets:
                    try:
                        bot.send_message(chat_id, bc.text, disable_web_page_preview=True)
                        sent += 1
                        time.sleep(0.05)
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        bc.last_error = str(exc)
                bc.sent_count = sent
                bc.failed_count = failed
                bc.status = Broadcast.Status.SENT if failed == 0 else Broadcast.Status.FAILED
                bc.save(update_fields=['sent_count', 'failed_count', 'status', 'last_error', 'updated_at'])
        except Exception:
            pass
        time.sleep(15)


class Command(BaseCommand):
    help = 'Run Telegram VPN sales bot with polling.'

    def handle(self, *args, **options):
        # سرویس systemd معمولا LANG ندارد و خروجی فارسی روی ASCII خطا می‌دهد.
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
        return self.run_bot(*args, **options)

    def wait_for_token(self) -> SiteSetting:
        """Block until the operator saves a bot token in the panel.

        The service usually starts right after install, before the token has
        been entered. Exiting here would make systemd give up after a few
        rapid restarts, so the bot would stay dead even once the token is
        saved. Waiting instead means the bot comes to life on its own.
        """
        warned = False
        while True:
            site = get_site()
            if site.telegram_bot_token:
                return site
            if not warned:
                self.stdout.write(self.style.WARNING(
                    'توکن ربات تلگرام هنوز ثبت نشده است. '
                    'وارد پنل شوید و در «تنظیمات اصلی ربات» آن را ذخیره کنید. '
                    'آدرس پنل را با دستور «vpnshop info» ببینید. '
                    'ربات به محض ثبت توکن خودکار شروع می‌کند.'
                ))
                warned = True
            time.sleep(10)

    def verify_token(self, token: str) -> str:
        """Reject an invalid token with a readable message instead of a traceback.

        A mistyped token is a common setup mistake. Waiting here lets the
        operator paste the correct one in the panel without touching the server.
        """
        warned = False
        while True:
            try:
                TeleBot(token).get_me()
                return token
            except Exception as exc:
                if not warned:
                    self.stdout.write(self.style.ERROR(
                        f'توکن ربات تلگرام معتبر نیست یا سرور به تلگرام دسترسی ندارد: {exc} '
                        'توکن را در پنل، بخش «تنظیمات اصلی ربات» بررسی و اصلاح کنید. '
                        'ربات به محض درست شدن توکن خودکار شروع می‌کند.'
                    ))
                    warned = True
                time.sleep(15)
                token = get_site().telegram_bot_token or token

    def run_bot(self, *args, **options):
        site = self.wait_for_token()
        token = self.verify_token(site.telegram_bot_token)

        bot = TeleBot(token, parse_mode='HTML')
        try:
            bot.set_my_commands([
                types.BotCommand('start', 'شروع و نمایش منو'),
                types.BotCommand('new', 'خرید اشتراک جدید'),
                types.BotCommand('wallet', 'کیف پول و شارژ'),
                types.BotCommand('services', 'سرویس‌های من'),
                types.BotCommand('addservice', 'افزودن اشتراک قدیمی با لینک کانفیگ'),
                types.BotCommand('renew', 'تمدید اشتراک'),
                types.BotCommand('tutorial', 'آموزش اتصال'),
                types.BotCommand('contact', 'ارتباط با ما'),
            ])
        except Exception:
            pass
        threading.Thread(target=process_queued_broadcasts, args=(bot,), daemon=True).start()
        threading.Thread(target=notify_auto_approved_cards, args=(bot,), daemon=True).start()

        @bot.message_handler(commands=['start', 'menu'])
        def start(message):
            user = ensure_user_from_message(message)
            reset_user_state(user)
            send_main_menu(bot, message.chat.id)

        @bot.message_handler(commands=['id'])
        def whoami(message):
            # Setting up operator alerts needs this number, and there is no
            # other way to find it from inside the bot.
            bot.send_message(
                message.chat.id,
                'شناسه چت شما:\n'
                f'<code>{message.chat.id}</code>\n\n'
                'برای دریافت اعلان پیام‌های پشتیبانی، این عدد را در پنل، '
                '«تنظیمات اصلی ربات» → «چت آیدی پشتیبانی» ثبت کنید.',
            )

        @bot.message_handler(commands=['new', 'renew', 'wallet', 'services', 'addservice', 'tutorial', 'contact'])
        def command_router(message):
            user = ensure_user_from_message(message)
            reset_user_state(user)
            action = MAIN_TEXT_ACTIONS.get('/' + message.text.split()[0].lstrip('/'))
            route_main_action(bot, user, message.chat.id, action or '')

        @bot.message_handler(content_types=['text', 'photo', 'document'])
        def text_handler(message):
            user = ensure_user_from_message(message)
            if user.is_blocked:
                return
            text_value = (message.text or '').strip()
            if text_value in MAIN_TEXT_ACTIONS:
                reset_user_state(user)
                route_main_action(bot, user, message.chat.id, MAIN_TEXT_ACTIONS[text_value])
                return
            if text_value in {BTN_CANCEL, 'لغو', 'انصراف', '🏠 منوی اصلی'}:
                reset_user_state(user)
                send_main_menu(bot, message.chat.id)
                return

            state = user.state or ''
            site_now = get_site()

            if state == 'awaiting_contact':
                text = message.text or message.caption or '[فایل/تصویر بدون متن]'
                SupportMessage.objects.create(user=user, message_text=text, telegram_message_id=message.message_id)
                admin_text = (
                    '📩 <b>پیام جدید پشتیبانی</b>\n\n'
                    f'👤 نام: {user.first_name} {user.last_name}\n'
                    f'🔖 Username: @{user.username or "-"}\n'
                    f'🆔 Chat ID: <code>{user.chat_id}</code>\n\n'
                    f'💬 {text}\n\n'
                    'برای پاسخ، از پنل بخش «ارسال پیام گروهی/تکی» با همین Chat ID استفاده کنید.'
                )
                notify_operator(bot, admin_text, photo_file_id=message.photo[-1].file_id if message.photo else None)
                reset_user_state(user)
                bot.send_message(message.chat.id, '✅ پیام شما ارسال شد. پشتیبانی بررسی می‌کند.', reply_markup=main_reply_keyboard())
                return

            if state == 'awaiting_config_link':
                raw_link = (message.text or message.caption or '').strip()
                try:
                    linked, info = link_config(user, raw_link)
                except LinkingError as exc:
                    bot.send_message(
                        message.chat.id,
                        f'⚠️ {exc}\n\nدوباره لینک را بفرستید یا «{BTN_CANCEL}» را بزنید.',
                        reply_markup=cancel_keyboard(),
                    )
                    return
                reset_user_state(user)
                bot.send_message(
                    message.chat.id,
                    '✅ سرویس شما با موفقیت اضافه شد.\n\n' + usage_text(info, linked.display_name()),
                    reply_markup=main_reply_keyboard(),
                )
                return

            if state == 'awaiting_custom_topup':
                amount = parse_toman(message.text or '')
                if amount < 10000:
                    bot.send_message(message.chat.id, 'مبلغ معتبر نیست. لطفاً مبلغ را به تومان وارد کنید. حداقل ۱۰,۰۰۰ تومان.')
                    return
                # A custom amount lands on the same method chooser as the
                # preset ones, so the two paths behave alike.
                reset_user_state(user)
                show_topup_methods(bot, message.chat.id, amount)
                return

            if state == 'awaiting_card_receipt':
                req = CardPaymentRequest.objects.filter(
                    pk=user.temp_data.get('card_request_id'), user=user
                ).first()
                if not req:
                    reset_user_state(user)
                    bot.send_message(
                        message.chat.id,
                        'این درخواست پیدا نشد. لطفاً دوباره از بخش کیف پول اقدام کنید.',
                        reply_markup=main_reply_keyboard(),
                    )
                    return

                req.receipt_text = message.text or message.caption or '[رسید تصویری]'
                if message.photo:
                    # Keep the largest size; file_id is enough to re-send it to the operator.
                    req.receipt_file_id = message.photo[-1].file_id
                elif message.document:
                    req.receipt_file_id = message.document.file_id
                req.save(update_fields=['receipt_text', 'receipt_file_id', 'updated_at'])

                admin_text = (
                    f'💳 <b>رسید کارت‌به‌کارت #{req.pk}</b>\n\n'
                    f'👤 نام: {user.first_name} {user.last_name}\n'
                    f'🔖 Username: @{user.username or "-"}\n'
                    f'🆔 Chat ID: <code>{user.chat_id}</code>\n'
                    f'💰 مبلغ دقیق: {toman(req.amount_toman)}\n'
                    f'📌 وضعیت: {"تاییدشده" if req.status == CardPaymentRequest.Status.APPROVED else "در انتظار"}'
                    f'{" (منقضی)" if req.is_expired else ""}\n\n'
                    f'🧾 {req.receipt_text}'
                )
                notify_operator(bot, admin_text, photo_file_id=req.receipt_file_id or None)

                reset_user_state(user)
                req.refresh_from_db()
                if req.status == CardPaymentRequest.Status.APPROVED:
                    reply = (
                        '✅ پرداخت شما قبلاً به‌صورت خودکار تایید و کیف پولتان شارژ شده است.\n'
                        'رسید هم برای پشتیبانی ثبت شد.'
                    )
                elif req.is_expired:
                    reply = (
                        '⏱ مهلت این فاکتور تمام شده بود، اما رسید شما ثبت شد.\n'
                        'پشتیبانی آن را بررسی و به‌صورت دستی تایید می‌کند.'
                    )
                else:
                    reply = (
                        '✅ رسید شما ثبت شد.\n'
                        'اگر مبلغ دقیق را واریز کرده باشید، تا چند دقیقه دیگر خودکار تایید می‌شود؛ '
                        'در غیر این صورت پشتیبانی بررسی می‌کند.'
                    )
                bot.send_message(message.chat.id, reply, reply_markup=main_reply_keyboard())
                return

            send_main_menu(bot, message.chat.id)

        @bot.callback_query_handler(func=lambda call: True)
        def callback(call):
            user = ensure_user_from_call(call)
            data = call.data or ''
            safe_answer_callback(bot, call)
            site_now = get_site()

            if data == 'cancel':
                reset_user_state(user)
                edit_or_send(bot, call, 'به منوی اصلی برگشتید.')
                return

            if not site_now.is_shop_active and data not in ['contact', 'tutorial']:
                edit_or_send(bot, call, 'فروشگاه موقتاً غیرفعال است. لطفاً بعداً مراجعه کنید.', home_inline_keyboard())
                return

            if data == 'new':
                show_new_services(bot, call.message.chat.id, call=call)
                return

            if data.startswith('svc:'):
                service_id = int(data.split(':')[1])
                service = Service.objects.get(pk=service_id, is_active=True)
                plans = service.plans.filter(is_active=True).order_by('sort_order', 'price_usd')
                text = f'📡 <b>{service.name}</b>\n{service.description or ""}\n\nپلن مورد نظر را انتخاب کنید:'
                rows = [[(f'{p.name} - {toman(p.price_toman)}', f'plan:{p.pk}')] for p in plans]
                rows.append([('بازگشت', 'new')])
                edit_or_send(bot, call, text, inline(rows))
                return

            if data.startswith('plan:'):
                plan = Plan.objects.select_related('service').get(pk=int(data.split(':')[1]), is_active=True)
                user.refresh_from_db()
                rows = []
                if user.wallet_balance_toman >= Decimal(plan.price_toman):
                    rows.append([('👛 پرداخت از کیف پول', f'buy:{plan.pk}')])
                rows.append([('🪙 پرداخت با کریپتو (ارزان‌تر)', f'cryptobuy:{plan.pk}')])
                if site_now.card_to_card_enabled:
                    rows.append([('💳 کارت‌به‌کارت', f'cardbuy:{plan.pk}')])
                rows.append([('بازگشت به سرویس‌ها', 'new')])
                edit_or_send(bot, call, plan_text(plan) + '\n\nروش پرداخت را انتخاب کنید:', inline(rows))
                return

            if data.startswith('cryptobuy:'):
                plan = Plan.objects.select_related('service').get(pk=int(data.split(':')[1]), is_active=True)
                try:
                    # The plan's own dollar price is billed, with no conversion,
                    # while the wallet is credited the toman price so the
                    # automatic purchase can go through.
                    payment = create_oxapay_payment(
                        user,
                        int(plan.price_toman),
                        amount_usd=Decimal(plan.price_usd),
                        pending_plan=plan,
                        auto_purchase=True,
                    )
                    text = (
                        f'🪙 پرداخت کریپتو برای <b>{plan.name}</b>\n\n'
                        f'مبلغ: {usd(plan.price_usd)}\n'
                        'بعد از پرداخت، سرویس به صورت خودکار ساخته و ارسال می‌شود.\n\n'
                        f'{payment.payment_url}'
                    )
                    edit_or_send(bot, call, text, home_inline_keyboard())
                except OxaPayError as exc:
                    edit_or_send(bot, call, f'خطا در ساخت لینک پرداخت: {exc}', home_inline_keyboard())
                return

            if data.startswith('cardbuy:'):
                plan = Plan.objects.select_related('service').get(pk=int(data.split(':')[1]), is_active=True)
                if not site_now.card_to_card_enabled:
                    edit_or_send(bot, call, 'کارت‌به‌کارت فعلاً غیرفعال است.', home_inline_keyboard())
                    return
                req = create_card_request(user, int(plan.price_toman), plan=plan)
                user.state = 'awaiting_card_receipt'
                user.temp_data = {'card_request_id': req.pk}
                user.save(update_fields=['state', 'temp_data', 'updated_at'])
                edit_or_send(bot, call, card_invoice_text(site_now, req), cancel_keyboard())
                return

            if data.startswith('buy:'):
                plan = Plan.objects.select_related('service').get(pk=int(data.split(':')[1]), is_active=True)
                price = Decimal(plan.price_toman)
                user.refresh_from_db()
                if user.wallet_balance_toman < price:
                    need = int(price - user.wallet_balance_toman)
                    text = (
                        f'موجودی کیف پول شما کافی نیست.\n'
                        f'قیمت پلن: {toman(price)}\n'
                        f'موجودی فعلی: {toman(user.wallet_balance_toman)}\n'
                        f'مبلغ موردنیاز برای شارژ: {toman(need)}'
                    )
                    rows = [[('💳 شارژ و خرید خودکار', f'chargebuy:{plan.pk}')], [('شارژ کیف پول', 'wallet')], [('بازگشت', f'plan:{plan.pk}')]]
                    edit_or_send(bot, call, text, inline(rows))
                    return
                order = None
                try:
                    order = create_order_from_wallet(user, plan)
                    order = provision_order(order)
                    send_delivery(bot, call.message.chat.id, order)
                except Exception as exc:  # noqa: BLE001
                    if order:
                        refund_order(order, f'خطا در ساخت سرویس: {exc}')
                    bot.send_message(call.message.chat.id, f'خرید انجام نشد و اگر مبلغی کم شده باشد به کیف پول برگشت داده شد.\nخطا: {exc}', reply_markup=main_reply_keyboard())
                return

            if data.startswith('chargebuy:'):
                plan = Plan.objects.get(pk=int(data.split(':')[1]), is_active=True)
                price = Decimal(plan.price_toman)
                user.refresh_from_db()
                need = int(max(Decimal('0'), price - user.wallet_balance_toman))
                try:
                    payment = create_oxapay_payment(user, need, pending_plan=plan, auto_purchase=True)
                    text = f'برای شارژ کیف پول و خرید خودکار این پلن، پرداخت را انجام دهید:\nمبلغ: {toman(need)}\n{payment.payment_url}'
                    edit_or_send(bot, call, text, home_inline_keyboard())
                except OxaPayError as exc:
                    edit_or_send(bot, call, f'خطا در ساخت لینک پرداخت: {exc}', home_inline_keyboard())
                return

            if data == 'wallet':
                show_wallet(bot, user, call.message.chat.id, call=call)
                return

            if data.startswith('topup:'):
                show_topup_methods(bot, call.message.chat.id, int(data.split(':')[1]), call=call)
                return

            if data == 'topup_custom':
                user.state = 'awaiting_custom_topup'
                user.temp_data = {}
                user.save(update_fields=['state', 'temp_data', 'updated_at'])
                edit_or_send(bot, call, 'مبلغ شارژ را به تومان وارد کنید. مثال: 250000', cancel_keyboard())
                return

            if data.startswith('payw:'):
                _, method, raw_amount = data.split(':')
                amount = int(raw_amount)
                if method == 'card':
                    if not site_now.card_to_card_enabled:
                        edit_or_send(bot, call, 'کارت‌به‌کارت فعلاً غیرفعال است.', home_inline_keyboard())
                        return
                    req = create_card_request(user, amount)
                    user.state = 'awaiting_card_receipt'
                    user.temp_data = {'card_request_id': req.pk}
                    user.save(update_fields=['state', 'temp_data', 'updated_at'])
                    edit_or_send(bot, call, card_invoice_text(site_now, req), cancel_keyboard())
                    return
                try:
                    payment = create_oxapay_payment(user, amount)
                    edit_or_send(
                        bot,
                        call,
                        f'✅ لینک پرداخت ساخته شد.\nمبلغ: {toman(amount)}\n'
                        f'معادل دلاری: {usd(payment.amount_usd)}\n\n{payment.payment_url}',
                        home_inline_keyboard(),
                    )
                except OxaPayError as exc:
                    edit_or_send(bot, call, f'خطا در ساخت لینک پرداخت: {exc}', home_inline_keyboard())
                return

            if data == 'services':
                show_my_services(bot, user, call.message.chat.id, call=call)
                return

            if data.startswith('resend:'):
                order = Order.objects.select_related('plan', 'service').get(pk=int(data.split(':')[1]), user=user)
                send_delivery(bot, call.message.chat.id, order)
                return

            if data == 'addservice':
                prompt_add_service(bot, user, call.message.chat.id, call=call)
                return

            if data.startswith('lusage:'):
                show_linked_usage(bot, user, call.message.chat.id, int(data.split(':')[1]), call=call)
                return

            if data.startswith('ldel:'):
                LinkedService.objects.filter(pk=int(data.split(':')[1]), user=user).delete()
                edit_or_send(bot, call, '🗑 سرویس از لیست شما حذف شد. کانفیگ خودش پابرجاست.', home_inline_keyboard())
                return

            if data == 'renew':
                show_renew(bot, user, call.message.chat.id, call=call)
                return

            if data.startswith('reneword:'):
                order = Order.objects.select_related('service').get(pk=int(data.split(':')[1]), user=user)
                plans = order.service.plans.filter(is_active=True)
                rows = [[(f'{p.name} - {toman(p.price_toman)}', f'renewpl:{order.pk}:{p.pk}')] for p in plans]
                rows.append([('بازگشت', 'renew')])
                edit_or_send(bot, call, 'پلن تمدید را انتخاب کنید:', inline(rows))
                return

            if data.startswith('renewpl:'):
                _, order_id, plan_id = data.split(':')
                order = Order.objects.get(pk=int(order_id), user=user)
                plan = Plan.objects.get(pk=int(plan_id), service=order.service, is_active=True)
                price = Decimal(plan.price_toman)
                user.refresh_from_db()
                if user.wallet_balance_toman < price:
                    edit_or_send(bot, call, f'موجودی کافی نیست. قیمت تمدید: {toman(price)}\nابتدا کیف پول را شارژ کنید.', inline([[('شارژ کیف پول', 'wallet')], [('بازگشت', 'renew')]]))
                    return
                try:
                    renewed = renew_order_from_wallet(order, plan)
                    bot.send_message(call.message.chat.id, f'✅ سرویس شما تمدید شد. تاریخ انقضای جدید: {fa_digits(timezone.localtime(renewed.expires_at).strftime("%Y-%m-%d %H:%M"))}', reply_markup=main_reply_keyboard())
                except Exception as exc:  # noqa: BLE001
                    bot.send_message(call.message.chat.id, f'خطا در تمدید خودکار. لطفاً با پشتیبانی ارتباط بگیرید.\n{exc}', reply_markup=main_reply_keyboard())
                return

            if data == 'tutorial':
                show_tutorial(bot, call.message.chat.id, call=call)
                return

            if data == 'contact':
                show_contact(bot, user, call.message.chat.id, call=call)
                return

            edit_or_send(bot, call, 'گزینه نامعتبر است.', home_inline_keyboard())

        # Telegram refuses getUpdates with HTTP 409 while a webhook is set, and
        # a webhook left over from an earlier deployment of the same token
        # would otherwise keep the bot dead forever.
        self.drop_webhook(bot)

        self.stdout.write(self.style.SUCCESS('Telegram bot is running. Press Ctrl+C to stop.'))
        while True:
            try:
                bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
                return
            except Exception as exc:
                self.stdout.write(self.style.ERROR(
                    f'ارتباط ربات با تلگرام قطع شد: {exc} — ۱۵ ثانیه دیگر دوباره تلاش می‌کنم.'
                ))
                time.sleep(15)
                self.drop_webhook(bot)

    def drop_webhook(self, bot: TeleBot) -> None:
        try:
            bot.remove_webhook()
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f'حذف webhook قبلی ناموفق بود: {exc}'))
