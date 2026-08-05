"""Send operator-written messages to customers, from whichever process asks.

Broadcasts used to be handed to a queue that only the polling bot process
drained, so a message written in the panel went nowhere whenever that service
was down — and, worse, nothing in the panel said so. Sending happens here
instead, straight from the process the operator is talking to, and the result
is written back on the row so the panel can show what actually happened.

The bot process still drains anything left queued, which covers a message
queued from the list view while the web process was busy. Both sides claim a
row before sending, so a message is never delivered twice.
"""

from __future__ import annotations

import threading
import time

from django import db
from django.utils import timezone

from sales.models import Broadcast, SupportMessage, TelegramUser
from sales.services.delivery import get_bot

# Above this many recipients the send is moved to a background thread, because
# Telegram's rate limit makes a large run take longer than a request may live.
INLINE_LIMIT = 25

# Telegram tolerates roughly 30 messages a second to different chats.
SEND_INTERVAL_SECONDS = 0.05


class MessagingError(RuntimeError):
    """Something that stops any message going out, worth showing the operator."""


def audience(broadcast: Broadcast) -> list[str]:
    """Who this broadcast goes to."""
    target = (broadcast.target_chat_id or '').strip()
    if target:
        return [target]
    return [str(chat_id) for chat_id in TelegramUser.objects.filter(is_blocked=False).values_list('chat_id', flat=True)]


def claim(broadcast: Broadcast) -> bool:
    """Take ownership of this row, or report that somebody else already has it."""
    return bool(
        Broadcast.objects.filter(
            pk=broadcast.pk, status__in=[Broadcast.Status.DRAFT, Broadcast.Status.QUEUED, Broadcast.Status.FAILED]
        ).update(status=Broadcast.Status.SENDING)
    )


def _run(broadcast: Broadcast, targets: list[str]) -> tuple[int, int]:
    """Send to everyone and write the outcome back. Assumes the row is claimed."""
    bot = get_bot()
    if bot is None:
        Broadcast.objects.filter(pk=broadcast.pk).update(
            status=Broadcast.Status.FAILED,
            last_error='توکن ربات تلگرام در تنظیمات ثبت نشده است.',
            updated_at=timezone.now(),
        )
        return 0, len(targets)

    sent = failed = 0
    last_error = ''
    for chat_id in targets:
        try:
            bot.send_message(chat_id, broadcast.text, disable_web_page_preview=True)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            last_error = f'{chat_id}: {exc}'
        time.sleep(SEND_INTERVAL_SECONDS)

    Broadcast.objects.filter(pk=broadcast.pk).update(
        sent_count=sent,
        failed_count=failed,
        last_error=last_error[:2000],
        sent_at=timezone.now(),
        status=Broadcast.Status.SENT if sent and not failed else (
            Broadcast.Status.FAILED if not sent else Broadcast.Status.SENT
        ),
        updated_at=timezone.now(),
    )
    return sent, failed


def _run_detached(broadcast: Broadcast, targets: list[str]) -> None:
    try:
        _run(broadcast, targets)
    finally:
        # A thread Django did not start keeps its own connection open; without
        # this the web process leaks one per broadcast.
        db.connections.close_all()


def send(broadcast: Broadcast) -> dict:
    """Deliver a broadcast now.

    Returns what to tell the operator: whether it went out inline, was handed
    to a background run, or could not start at all.
    """
    if not (broadcast.text or '').strip():
        raise MessagingError('متن پیام خالی است.')

    targets = audience(broadcast)
    if not targets:
        Broadcast.objects.filter(pk=broadcast.pk).update(
            status=Broadcast.Status.FAILED,
            last_error='هیچ کاربری برای ارسال پیدا نشد.',
            updated_at=timezone.now(),
        )
        raise MessagingError('هیچ کاربری برای ارسال وجود ندارد. هنوز کسی به ربات /start نزده است.')

    if not claim(broadcast):
        raise MessagingError('این پیام قبلاً ارسال شده یا همین حالا در حال ارسال است.')

    if len(targets) <= INLINE_LIMIT:
        sent, failed = _run(broadcast, targets)
        return {'inline': True, 'total': len(targets), 'sent': sent, 'failed': failed}

    threading.Thread(target=_run_detached, args=(broadcast, targets), daemon=True).start()
    return {'inline': False, 'total': len(targets), 'sent': 0, 'failed': 0}


def reply_to_support(message: SupportMessage, text: str) -> None:
    """Send the operator's reply to the customer who wrote in.

    Raises MessagingError with a reason the operator can act on, rather than
    failing quietly, because a support reply that vanishes is worse than none.
    """
    text = (text or '').strip()
    if not text:
        raise MessagingError('متن پاسخ خالی است.')

    bot = get_bot()
    if bot is None:
        raise MessagingError('توکن ربات تلگرام در تنظیمات ثبت نشده است.')

    body = f'✉️ <b>پاسخ پشتیبانی</b>\n\n{text}'
    try:
        bot.send_message(message.user.chat_id, body, disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001
        SupportMessage.objects.filter(pk=message.pk).update(
            reply_error=str(exc)[:300], updated_at=timezone.now()
        )
        raise MessagingError(
            f'ارسال پاسخ ناموفق بود: {exc} '
            'معمولاً یعنی کاربر ربات را بلاک کرده یا هنوز /start نزده است.'
        ) from exc

    SupportMessage.objects.filter(pk=message.pk).update(
        is_answered=True,
        answered_at=timezone.now(),
        reply_error='',
        updated_at=timezone.now(),
    )
