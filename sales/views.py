from __future__ import annotations

import hmac
import json

from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from sales.models import Payment, SiteSetting
from sales.services.cardpay import process_incoming_sms
from sales.services.oxapay import validate_webhook_signature
from sales.services.payments import settle_payment
from sales.services.telegram_webhook import handle_update


# Field names used by the various Android SMS forwarder apps.
SMS_TEXT_KEYS = ('text', 'message', 'msg', 'body', 'content', 'sms', 'Message')
SMS_SENDER_KEYS = ('from', 'sender', 'number', 'address', 'phone', 'From')


def _first_value(source, keys) -> str:
    for key in keys:
        value = source.get(key)
        if value:
            return str(value).strip()
    return ''


@csrf_exempt
def sms_webhook(request):
    """Receive bank SMS forwarded from the operator's phone.

    Accepts JSON, form-encoded or query-string bodies so it works with whatever
    generic forwarder app the operator installs.
    """
    site = SiteSetting.get_solo()
    expected = (site.sms_webhook_secret or '').strip()
    if not expected:
        return HttpResponseForbidden('sms webhook is not configured')

    provided = (
        request.GET.get('secret')
        or request.headers.get('X-Secret')
        or request.headers.get('Authorization', '').replace('Bearer ', '')
        or ''
    ).strip()
    if not hmac.compare_digest(provided, expected):
        return HttpResponseForbidden('bad secret')

    if request.method not in ('POST', 'GET'):
        return HttpResponseBadRequest('POST required')

    payload: dict = {}
    if request.body:
        try:
            parsed = json.loads(request.body.decode('utf-8'))
            if isinstance(parsed, dict):
                payload = parsed
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
    if not payload:
        payload = {**request.GET.dict(), **request.POST.dict()}

    text = _first_value(payload, SMS_TEXT_KEYS)
    sender = _first_value(payload, SMS_SENDER_KEYS)
    if not text:
        return JsonResponse({'ok': False, 'error': 'no sms text found'}, status=400)

    sms = process_incoming_sms(sender, text)
    return JsonResponse({
        'ok': True,
        'matched': bool(sms.matched_request_id),
        'note': sms.note,
    })


@csrf_exempt
def telegram_webhook(request, secret: str):
    """Receive one Telegram update.

    Two checks, both compared in constant time: the secret in the path, and the
    header Telegram attaches to every request it sends. Either one failing means
    this did not come from Telegram, and the answer is a flat 403.
    """
    site = SiteSetting.get_solo()
    expected = (site.telegram_webhook_secret or '').strip()
    if not expected:
        return HttpResponseForbidden('telegram webhook is not configured')
    if not hmac.compare_digest(secret or '', expected):
        return HttpResponseForbidden('bad secret')

    header = (request.headers.get('X-Telegram-Bot-Api-Secret-Token') or '').strip()
    if not hmac.compare_digest(header, expected):
        return HttpResponseForbidden('bad secret token header')

    if request.method != 'POST':
        return HttpResponseBadRequest('POST required')
    if not site.telegram_use_webhook:
        # Refusing rather than handling: with polling switched back on, both
        # transports would otherwise answer the same customer twice.
        return HttpResponse('webhook mode is off', content_type='text/plain')

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponseBadRequest('bad json')

    try:
        handle_update(payload)
    except Exception as exc:  # noqa: BLE001
        # Telegram retries anything that is not a 2xx, which would replay a
        # failing update forever. The failure is logged and acknowledged.
        print(f'خطا در پردازش آپدیت تلگرام: {exc}', flush=True)
    return HttpResponse('ok', content_type='text/plain')


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

    # v1 sends snake_case, the legacy API sends camelCase.
    status = str(payload.get('status', '')).lower()
    order_id = str(payload.get('order_id') or payload.get('orderId') or '')
    track_id = str(payload.get('track_id') or payload.get('trackId') or '')

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
        payment.save(update_fields=['track_id', 'raw_payload', 'updated_at'])
        settle_payment(payment)
        return HttpResponse('ok', content_type='text/plain')

    if status in ['failed', 'expired', 'cancelled', 'canceled']:
        payment.status = Payment.Status.EXPIRED if status == 'expired' else Payment.Status.FAILED
        payment.save(update_fields=['status', 'track_id', 'raw_payload', 'updated_at'])
        return HttpResponse('ok', content_type='text/plain')

    return HttpResponse('ok', content_type='text/plain')
