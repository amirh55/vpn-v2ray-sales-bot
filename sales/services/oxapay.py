from __future__ import annotations

import hmac
import hashlib
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx
from django.conf import settings as django_settings

from sales.models import Payment, SiteSetting

OXAPAY_INVOICE_URL = 'https://api.oxapay.com/v1/payment/invoice'


class OxaPayError(RuntimeError):
    pass


def toman_to_usd(amount_toman: Decimal, dollar_rate_toman: Decimal) -> Decimal:
    if not dollar_rate_toman:
        return Decimal('0')
    return (Decimal(amount_toman) / Decimal(dollar_rate_toman)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def create_invoice(payment: Payment) -> Payment:
    site = SiteSetting.get_solo()
    if not site.oxapay_merchant_api_key:
        raise OxaPayError('کلید API درگاه OxaPay در تنظیمات ثبت نشده است.')

    callback_url = f'{django_settings.PUBLIC_BASE_URL}/api/payments/oxapay/webhook/'
    payload: dict[str, Any] = {
        'amount': float(payment.amount_usd),
        'currency': 'USD',
        'lifetime': int(site.invoice_lifetime_minutes or 60),
        'fee_paid_by_payer': 1 if site.oxapay_fee_paid_by_payer else 0,
        'callback_url': callback_url,
        'order_id': payment.order_id,
        'description': f'Wallet top-up / VPN order {payment.order_id}',
        'sandbox': bool(site.oxapay_sandbox),
    }
    headers = {
        'merchant_api_key': site.oxapay_merchant_api_key,
        'Content-Type': 'application/json',
    }
    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(OXAPAY_INVOICE_URL, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001
        raise OxaPayError(f'ارتباط با OxaPay برقرار نشد: {exc}') from exc

    try:
        data = response.json()
    except ValueError:
        data = {}

    # OxaPay explains the real cause in the body, so never hide it behind a
    # bare HTTP status. Auth failures arrive as 401 or as 403 from Cloudflare.
    api_status = data.get('status') if isinstance(data, dict) else None
    api_message = data.get('message') if isinstance(data, dict) else None

    if response.status_code >= 400 or (api_status and int(api_status) >= 400):
        detail = api_message or response.text[:300] or f'HTTP {response.status_code}'
        if response.status_code in (401, 403) or (api_status and int(api_status) in (401, 403)):
            raise OxaPayError(
                f'کلید API درگاه OxaPay پذیرفته نشد. پاسخ درگاه: {detail} | '
                'بررسی کنید: ۱) حتما Merchant API Key باشد، نه Payout یا General. '
                '۲) اگر در پنل OxaPay محدودیت IP فعال کرده‌اید، IP این سرور را مجاز کنید. '
                '۳) حساب OxaPay شما فعال و تاییدشده باشد.'
            )
        raise OxaPayError(f'خطا در ساخت فاکتور OxaPay: {detail}')

    raw_data = data.get('data') if isinstance(data, dict) else None
    if not isinstance(raw_data, dict):
        raw_data = data if isinstance(data, dict) else {}

    payment_url = (
        raw_data.get('payment_url')
        or raw_data.get('pay_url')
        or raw_data.get('payLink')
        or raw_data.get('url')
        or raw_data.get('checkout_url')
        or raw_data.get('invoice_url')
    )
    track_id = str(raw_data.get('track_id') or raw_data.get('trackId') or raw_data.get('id') or '')

    if not payment_url:
        raise OxaPayError(f'لینک پرداخت در پاسخ OxaPay پیدا نشد. پاسخ خام: {data}')

    payment.payment_url = payment_url
    payment.track_id = track_id
    payment.raw_payload = data
    payment.save(update_fields=['payment_url', 'track_id', 'raw_payload', 'updated_at'])
    return payment


def validate_webhook_signature(raw_body: bytes, received_hmac: str | None) -> bool:
    site = SiteSetting.get_solo()
    if not site.oxapay_merchant_api_key or not received_hmac:
        return False
    digest = hmac.new(site.oxapay_merchant_api_key.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(digest.lower(), received_hmac.lower())
