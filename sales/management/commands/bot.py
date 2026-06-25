from __future__ import annotations

import threading
import time
import uuid
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from telebot import TeleBot, types

from sales.models import (
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
)
from sales.services.formatting import days_text, fa_digits, parse_toman, toman, traffic_text, usd
from sales.services.oxapay import OxaPayError, create_invoice, toman_to_usd
from sales.services.provisioning import create_order_from_wallet, provision_order, renew_order_from_wallet

BTN_NEW = '🛒 خرید اشتراک جدید'
BTN_RENEW = '🔁 تمدید اشتراک'
BTN_WALLET = '💳 کیف پول + شارژ'
BTN_SERVICES = '📦 سرویس‌های من'
BTN_TUTORIAL = '📚 آموزش اتصال'
BTN_CONTACT = '☎️ ارتباط با ما'
BTN_CANCEL = 'لغو و بازگشت'

MAIN_TEXT_ACTIONS = {
    BTN_NEW: 'new',
    BTN_RENEW: 'renew',
    BTN_WALLET: 'wallet',
    BTN_SERVICES: 'services',
    BTN_TUTORIAL: 'tutorial',
    BTN_CONTACT: 'contact',
    '/new': 'new',
    '/renew': 'renew',
    '/wallet': 'wallet',
    '/services': 'services',
    '/tutorial': 'tutorial',
    '/contact': 'contact',
}


def inline(rows: list[list[tuple[str, str]]]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    for row in rows:
        kb.row(*[types.InlineKeyboardButton(text, callback_data=data) for text, data in row])
    return kb


def safe_answer_callback(bot: telebot.TeleBot, call, text: str | None = None) -> None:
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
    kb.row(BTN_SERVICES, BTN_TUTORIAL)
    kb.row(BTN_CONTACT)
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
        f'تعداد کاربر: {fa_digits(plan.user_limit)}\n'
        f'قیمت: {usd(plan.price_usd)} / {toman(plan.price_toman())}'
    )


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


def create_oxapay_payment(user: TelegramUser, amount_toman: int, pending_plan: Plan | None = None, auto_purchase: bool = False) -> Payment:
    site = get_site()
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
    site_now = get_site()
    user.refresh_from_db()
    rows = [
        [('۱۰۰ هزار', 'topup:100000'), ('۵۰۰ هزار', 'topup:500000')],
        [('۱ میلیون', 'topup:1000000'), ('۲ میلیون', 'topup:2000000')],
        [('مبلغ دلخواه', 'topup_custom')],
    ]
    if site_now.card_to_card_enabled:
        rows.append([('کارت‌به‌کارت', 'card_amount')])
    rows.append([('بازگشت', 'cancel')])
    send_or_edit(bot, chat_id, f'💳 موجودی کیف پول شما: {toman(user.wallet_balance_toman)}\nمبلغ شارژ را انتخاب کنید:', inline(rows), call=call)


def show_my_services(bot: TeleBot, user: TelegramUser, chat_id: int, call=None):
    orders = Order.objects.filter(user=user, status=Order.Status.PROVISIONED).select_related('plan', 'service').order_by('-created_at')[:10]
    if not orders:
        send_or_edit(bot, chat_id, 'هنوز سرویس فعالی ندارید.', home_inline_keyboard(), call=call)
        return
    rows = []
    text = '📦 سرویس‌های شما:\n\n'
    for o in orders:
        exp = fa_digits(timezone.localtime(o.expires_at).strftime('%Y-%m-%d')) if o.expires_at else 'بدون تاریخ'
        text += f'#{fa_digits(o.pk)} - {o.service.name} / {o.plan.name} / انقضا: {exp}\n'
        rows.append([(f'ارسال مجدد لینک #{o.pk}', f'resend:{o.pk}')])
    rows.append([('بازگشت', 'cancel')])
    send_or_edit(bot, chat_id, text, inline(rows), call=call)


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
    elif action == 'renew':
        show_renew(bot, user, chat_id)
    elif action == 'tutorial':
        show_tutorial(bot, chat_id)
    elif action == 'contact':
        show_contact(bot, user, chat_id)
    else:
        send_main_menu(bot, chat_id)


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
        site = get_site()
        if not site.telegram_bot_token:
            raise CommandError('توکن ربات تلگرام را در /admin/ بخش تنظیمات اصلی ثبت کنید.')

        bot = TeleBot(site.telegram_bot_token, parse_mode='HTML')
        try:
            bot.set_my_commands([
                types.BotCommand('start', 'شروع و نمایش منو'),
                types.BotCommand('new', 'خرید اشتراک جدید'),
                types.BotCommand('wallet', 'کیف پول و شارژ'),
                types.BotCommand('services', 'سرویس‌های من'),
                types.BotCommand('renew', 'تمدید اشتراک'),
                types.BotCommand('tutorial', 'آموزش اتصال'),
                types.BotCommand('contact', 'ارتباط با ما'),
            ])
        except Exception:
            pass
        threading.Thread(target=process_queued_broadcasts, args=(bot,), daemon=True).start()

        @bot.message_handler(commands=['start', 'menu'])
        def start(message):
            user = ensure_user_from_message(message)
            reset_user_state(user)
            send_main_menu(bot, message.chat.id)

        @bot.message_handler(commands=['new', 'renew', 'wallet', 'services', 'tutorial', 'contact'])
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
                if site_now.support_chat_id:
                    admin_text = (
                        '📩 پیام جدید پشتیبانی\n'
                        f'Chat ID: <code>{user.chat_id}</code>\n'
                        f'Username: @{user.username or "-"}\n'
                        f'نام: {user.first_name} {user.last_name}\n\n'
                        f'{text}'
                    )
                    try:
                        bot.send_message(site_now.support_chat_id, admin_text)
                    except Exception:
                        pass
                reset_user_state(user)
                bot.send_message(message.chat.id, '✅ پیام شما ارسال شد. پشتیبانی بررسی می‌کند.', reply_markup=main_reply_keyboard())
                return

            if state == 'awaiting_custom_topup':
                amount = parse_toman(message.text or '')
                if amount < 10000:
                    bot.send_message(message.chat.id, 'مبلغ معتبر نیست. لطفاً مبلغ را به تومان وارد کنید. حداقل ۱۰,۰۰۰ تومان.')
                    return
                try:
                    payment = create_oxapay_payment(user, amount)
                    bot.send_message(
                        message.chat.id,
                        f'✅ لینک پرداخت ساخته شد.\nمبلغ: {toman(amount)}\nمبلغ دلاری تقریبی: {usd(payment.amount_usd)}\n\n{payment.payment_url}',
                        reply_markup=main_reply_keyboard(),
                        disable_web_page_preview=True,
                    )
                except OxaPayError as exc:
                    bot.send_message(message.chat.id, f'خطا در ساخت لینک پرداخت: {exc}', reply_markup=main_reply_keyboard())
                reset_user_state(user)
                return

            if state == 'awaiting_card_receipt':
                amount = Decimal(user.temp_data.get('amount_toman') or 0)
                receipt_text = message.text or message.caption or '[رسید تصویری/فایل]'
                req = CardPaymentRequest.objects.create(user=user, amount_toman=amount, receipt_text=receipt_text)
                if site_now.support_chat_id:
                    try:
                        bot.send_message(
                            site_now.support_chat_id,
                            f'💳 درخواست کارت‌به‌کارت جدید #{req.pk}\nChat ID: <code>{user.chat_id}</code>\nمبلغ: {toman(amount)}\nرسید: {receipt_text}',
                        )
                    except Exception:
                        pass
                reset_user_state(user)
                bot.send_message(message.chat.id, '✅ رسید شما ثبت شد. بعد از تایید مدیر، کیف پول شارژ می‌شود.', reply_markup=main_reply_keyboard())
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
                rows = [[(f'{p.name} - {toman(p.price_toman())}', f'plan:{p.pk}')] for p in plans]
                rows.append([('بازگشت', 'new')])
                edit_or_send(bot, call, text, inline(rows))
                return

            if data.startswith('plan:'):
                plan = Plan.objects.select_related('service').get(pk=int(data.split(':')[1]), is_active=True)
                text = plan_text(plan)
                rows = [[('✅ خرید این پلن', f'buy:{plan.pk}')], [('بازگشت به سرویس‌ها', 'new')]]
                edit_or_send(bot, call, text, inline(rows))
                return

            if data.startswith('buy:'):
                plan = Plan.objects.select_related('service').get(pk=int(data.split(':')[1]), is_active=True)
                price = Decimal(plan.price_toman())
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
                price = Decimal(plan.price_toman())
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
                amount = int(data.split(':')[1])
                try:
                    payment = create_oxapay_payment(user, amount)
                    edit_or_send(bot, call, f'✅ لینک پرداخت ساخته شد.\nمبلغ: {toman(amount)}\nمعادل دلاری: {usd(payment.amount_usd)}\n\n{payment.payment_url}', home_inline_keyboard())
                except OxaPayError as exc:
                    edit_or_send(bot, call, f'خطا در ساخت لینک پرداخت: {exc}', home_inline_keyboard())
                return

            if data == 'topup_custom':
                user.state = 'awaiting_custom_topup'
                user.temp_data = {}
                user.save(update_fields=['state', 'temp_data', 'updated_at'])
                edit_or_send(bot, call, 'مبلغ شارژ را به تومان وارد کنید. مثال: 250000', cancel_keyboard())
                return

            if data == 'card_amount':
                if not site_now.card_to_card_enabled:
                    edit_or_send(bot, call, 'کارت‌به‌کارت فعلاً غیرفعال است.', home_inline_keyboard())
                    return
                rows = [[('۵۰۰ هزار', 'card:500000'), ('۱ میلیون', 'card:1000000')], [('۲ میلیون', 'card:2000000')], [('بازگشت', 'wallet')]]
                edit_or_send(bot, call, 'مبلغ کارت‌به‌کارت را انتخاب کنید:', inline(rows))
                return

            if data.startswith('card:'):
                amount = int(data.split(':')[1])
                if not site_now.card_to_card_enabled:
                    edit_or_send(bot, call, 'کارت‌به‌کارت فعلاً غیرفعال است.', home_inline_keyboard())
                    return
                user.state = 'awaiting_card_receipt'
                user.temp_data = {'amount_toman': amount}
                user.save(update_fields=['state', 'temp_data', 'updated_at'])
                edit_or_send(bot, call, f'{site_now.card_to_card_text}\n\nمبلغ: {toman(amount)}\nبعد از واریز، رسید یا شماره پیگیری را همینجا ارسال کنید.', cancel_keyboard())
                return

            if data == 'services':
                show_my_services(bot, user, call.message.chat.id, call=call)
                return

            if data.startswith('resend:'):
                order = Order.objects.select_related('plan', 'service').get(pk=int(data.split(':')[1]), user=user)
                send_delivery(bot, call.message.chat.id, order)
                return

            if data == 'renew':
                show_renew(bot, user, call.message.chat.id, call=call)
                return

            if data.startswith('reneword:'):
                order = Order.objects.select_related('service').get(pk=int(data.split(':')[1]), user=user)
                plans = order.service.plans.filter(is_active=True)
                rows = [[(f'{p.name} - {toman(p.price_toman())}', f'renewpl:{order.pk}:{p.pk}')] for p in plans]
                rows.append([('بازگشت', 'renew')])
                edit_or_send(bot, call, 'پلن تمدید را انتخاب کنید:', inline(rows))
                return

            if data.startswith('renewpl:'):
                _, order_id, plan_id = data.split(':')
                order = Order.objects.get(pk=int(order_id), user=user)
                plan = Plan.objects.get(pk=int(plan_id), service=order.service, is_active=True)
                price = Decimal(plan.price_toman())
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

        self.stdout.write(self.style.SUCCESS('Telegram bot is running. Press Ctrl+C to stop.'))
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
