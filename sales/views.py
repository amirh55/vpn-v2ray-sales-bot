from __future__ import annotations

import json
from decimal import Decimal

from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.db import transaction
from django.views.decorators.csrf import csrf_exempt
from telebot import TeleBot

from sales.models import Payment, Plan, SiteSetting, TelegramUser, WalletTransaction
from sales.services.oxapay import validate_webhook_signature
from sales.services.provisioning import create_order_from_wallet, provision_order
from sales.services.formatting import toman


def _send_bot_message(chat_id: int, text: str):
    site = SiteSetting.get_solo()
    if not site.telegram_bot_token:
        return
    bot = TeleBot(site.telegram_bot_token, parse_mode='HTML')
    try:
        bot.send_message(chat_id, text, disable_web_page_preview=True)
    except Exception:
        pass


@csrf_exempt
def oxapay_webhook(request):
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required')

    raw = request.body
    received_hmac = request.headers.get('HMAC') or request.headers.get('hmac')
    if not validate_webhook_signature(raw, received_hmac):
        return HttpResponseForbidden('bad signature')

    try:
        payload = json.loads(raw.decode('utf-8'))
    except json.JSONDecodeError:
        return HttpResponseBadRequest('bad json')

    status = str(payload.get('status', '')).lower()
    order_id = str(payload.get('order_id') or '')
    track_id = str(payload.get('track_id') or '')

    payment = None
    if order_id:
        payment = Payment.objects.filter(order_id=order_id).select_related('user').first()
    if payment is None and track_id:
        payment = Payment.objects.filter(track_id=track_id).select_related('user').first()
    if payment is None:
        return HttpResponse('ok', content_type='text/plain')

    payment.raw_payload = payload
    if track_id and not payment.track_id:
        payment.track_id = track_id

    if status in ['paying', 'confirming', 'waiting']:
        payment.status = Payment.Status.PAYING
        payment.save(update_fields=['status', 'track_id', 'raw_payload', 'updated_at'])
        return HttpResponse('ok', content_type='text/plain')

    if status == 'paid':
        if payment.status != Payment.Status.PAID:
            with transaction.atomic():
                payment = Payment.objects.select_for_update().select_related('user').get(pk=payment.pk)
                if payment.status == Payment.Status.PAID:
                    return HttpResponse('ok', content_type='text/plain')
                payment.status = Payment.Status.PAID
                payment.save(update_fields=['status', 'track_id', 'raw_payload', 'updated_at'])
                user = TelegramUser.objects.select_for_update().get(pk=payment.user.pk)
                user.wallet_balance_toman += Decimal(payment.amount_toman)
                user.save(update_fields=['wallet_balance_toman', 'updated_at'])
                WalletTransaction.objects.create(
                    user=user,
                    kind=WalletTransaction.Kind.CREDIT,
                    amount_toman=payment.amount_toman,
                    balance_after_toman=user.wallet_balance_toman,
                    description=f'شارژ کیف پول با OxaPay / {payment.order_id}',
                )
            _send_bot_message(user.chat_id, f'✅ پرداخت شما تایید شد و کیف پولتان به مبلغ {toman(payment.amount_toman)} شارژ شد.')

            if payment.auto_purchase_after_paid and payment.pending_plan_id:
                try:
                    plan = Plan.objects.get(pk=payment.pending_plan_id)
                    order = create_order_from_wallet(user, plan)
                    order = provision_order(order)
                    text = '✅ خرید اشتراک با موفقیت انجام شد.\n\n'
                    if order.config_link:
                        text += f'🔗 لینک کانفیگ:\n<code>{order.config_link}</code>\n\n'
                    if order.subscription_link:
                        text += f'🔄 لینک سابسکریپشن:\n<code>{order.subscription_link}</code>\n'
                    _send_bot_message(user.chat_id, text)
                except Exception as exc:  # noqa: BLE001
                    _send_bot_message(user.chat_id, f'پرداخت تایید شد، اما ساخت خودکار سرویس خطا داد. پشتیبانی بررسی می‌کند.\nخطا: {exc}')
        return HttpResponse('ok', content_type='text/plain')

    if status in ['failed', 'expired', 'cancelled', 'canceled']:
        payment.status = Payment.Status.EXPIRED if status == 'expired' else Payment.Status.FAILED
        payment.save(update_fields=['status', 'track_id', 'raw_payload', 'updated_at'])
        return HttpResponse('ok', content_type='text/plain')

    return HttpResponse('ok', content_type='text/plain')
