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
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001
        raise OxaPayError(f'خطا در ساخت فاکتور OxaPay: {exc}') from exc

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
