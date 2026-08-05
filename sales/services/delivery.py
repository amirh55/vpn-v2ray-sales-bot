"""Send a finished order to the customer.

Both processes need this: the bot delivers after a purchase, and the web
process delivers after the operator approves a payment by hand. Keeping the
message here means the customer sees the same thing either way.
"""

from __future__ import annotations

from telebot import TeleBot

from sales.models import Order, SiteSetting
from sales.services import jalali
from sales.services.formatting import days_text, traffic_text


def order_delivery_text(order: Order) -> str:
    lines = [
        '✅ <b>اشتراک شما آماده است.</b>',
        '',
        f'📌 <b>{order.plan.name}</b>',
        f'سرویس: {order.service.name}',
        f'مدت: {days_text(order.plan.duration_days)}',
        f'حجم: {traffic_text(order.plan.traffic_gb)}',
    ]
    if order.expires_at:
        lines.append(f'تاریخ انقضا: {jalali.format_datetime(order.expires_at)}')
    if order.config_link:
        lines += ['', '🔗 <b>لینک کانفیگ:</b>', f'<code>{order.config_link}</code>']
    if order.subscription_link:
        lines += ['', '🔄 <b>لینک Subscription:</b>', f'<code>{order.subscription_link}</code>']
    if not order.config_link and not order.subscription_link:
        lines += ['', '⚠️ لینک کانفیگ/سابسکریپشن ساخته نشد. قالب لینک را در پنل تنظیم کنید.']
    return '\n'.join(lines)


def get_bot() -> TeleBot | None:
    token = (SiteSetting.get_solo().telegram_bot_token or '').strip()
    return TeleBot(token, parse_mode='HTML') if token else None


def send_text(chat_id, text: str) -> bool:
    bot = get_bot()
    if not bot:
        return False
    try:
        bot.send_message(chat_id, text, disable_web_page_preview=True)
        return True
    except Exception:  # noqa: BLE001
        return False


# Telegram rejects a photo caption longer than this.
CAPTION_LIMIT = 1024


def deliver_order(bot, chat_id, order: Order, reply_markup=None) -> bool:
    """Send the QR image carrying the config text as its caption.

    A long config link can push the text past Telegram's caption limit, in
    which case the text is sent on its own and the QR follows it.
    """
    text = order_delivery_text(order)

    if order.qr_image and len(text) <= CAPTION_LIMIT:
        try:
            with open(order.qr_image.path, 'rb') as handle:
                bot.send_photo(chat_id, handle, caption=text, reply_markup=reply_markup)
            return True
        except Exception:  # noqa: BLE001
            # Fall through and deliver as text so the customer still gets the config.
            pass

    try:
        bot.send_message(chat_id, text, reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        return False

    if order.qr_image:
        try:
            with open(order.qr_image.path, 'rb') as handle:
                bot.send_photo(chat_id, handle, caption='QR Code اشتراک')
        except Exception:  # noqa: BLE001
            # The links already went out; a missing QR is not a failed delivery.
            pass
    return True


def send_order(order: Order) -> bool:
    """Deliver the config links and QR code. False when nothing was sent."""
    bot = get_bot()
    if not bot:
        return False
    return deliver_order(bot, order.user.chat_id, order)
