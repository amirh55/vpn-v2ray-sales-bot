from __future__ import annotations

import hmac
import hashlib
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx

from sales.models import Payment, SiteSetting

OXAPAY_INVOICE_URL = 'https://api.oxapay.com/v1/payment/invoice'
# Keys issued by the older OxaPay dashboard are rejected by the v1 endpoint but
# still work here, so accounts on either generation can take payments.
OXAPAY_LEGACY_INVOICE_URL = 'https://api.oxapay.com/merchants/request'
LEGACY_SUCCESS_CODE = 100
# Legacy has no sandbox flag; the sandbox is a reserved merchant value.
LEGACY_SANDBOX_MERCHANT = 'sandbox'


class OxaPayError(RuntimeError):
    pass


class OxaPayAuthError(OxaPayError):
    """The gateway rejected the key, so a different API generation may fit."""


def toman_to_usd(amount_toman: Decimal, dollar_rate_toman: Decimal) -> Decimal:
    if not dollar_rate_toman:
        return Decimal('0')
    return (Decimal(amount_toman) / Decimal(dollar_rate_toman)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def get_merchant_key() -> str:
    """Return the configured merchant key, without stray whitespace.

    A key pasted with a trailing space or newline is rejected as 401, which
    reads exactly like a wrong key and wastes a lot of debugging time.
    """
    site = SiteSetting.get_solo()
    return (site.oxapay_merchant_api_key or '').strip()


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[Any, int, str]:
    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(url, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001
        raise OxaPayError(f'ارتباط با OxaPay برقرار نشد: {exc}') from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    return data, response.status_code, response.text[:300]


def _callback_url() -> str:
    # Imported here because site_urls imports models, and this module is
    # imported during app loading.
    from sales.services.site_urls import oxapay_webhook_url

    return oxapay_webhook_url()


def create_invoice_v1(payment: Payment, site: SiteSetting, merchant_key: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'amount': float(payment.amount_usd),
        'currency': 'USD',
        'lifetime': int(site.invoice_lifetime_minutes or 60),
        'fee_paid_by_payer': 1 if site.oxapay_fee_paid_by_payer else 0,
        'callback_url': _callback_url(),
        'order_id': payment.order_id,
        'description': f'Wallet top-up / VPN order {payment.order_id}',
        'sandbox': bool(site.oxapay_sandbox),
    }
    headers = {'merchant_api_key': merchant_key, 'Content-Type': 'application/json'}
    data, http_status, raw_text = _post_json(OXAPAY_INVOICE_URL, payload, headers)

    api_status = data.get('status') if isinstance(data, dict) else None
    api_message = data.get('message') if isinstance(data, dict) else None
    detail = api_message or raw_text or f'HTTP {http_status}'

    # Auth failures come back as 401, or as 403 from Cloudflare with 401 inside.
    is_auth_failure = http_status in (401, 403) or (api_status and int(api_status) in (401, 403))
    if is_auth_failure:
        raise OxaPayAuthError(detail)
    if http_status >= 400 or (api_status and int(api_status) >= 400):
        raise OxaPayError(f'خطا در ساخت فاکتور OxaPay: {detail}')

    body = data.get('data') if isinstance(data, dict) else None
    if not isinstance(body, dict):
        body = data if isinstance(data, dict) else {}
    payment_url = body.get('payment_url') or body.get('pay_url') or body.get('payLink') or body.get('url')
    if not payment_url:
        raise OxaPayError(f'لینک پرداخت در پاسخ OxaPay پیدا نشد. پاسخ خام: {data}')
    return {
        'payment_url': payment_url,
        'track_id': str(body.get('track_id') or body.get('trackId') or body.get('id') or ''),
        'raw': data,
        'api': 'v1',
    }


def create_invoice_legacy(payment: Payment, site: SiteSetting, merchant_key: str) -> dict[str, Any]:
    """Invoice through the older merchants/request API.

    Sandbox here is a reserved merchant value rather than a flag, so test mode
    never touches the real account and never moves real funds.
    """
    merchant = LEGACY_SANDBOX_MERCHANT if site.oxapay_sandbox else merchant_key
    payload: dict[str, Any] = {
        'merchant': merchant,
        'amount': float(payment.amount_usd),
        'currency': 'USD',
        'lifeTime': int(site.invoice_lifetime_minutes or 60),
        'feePaidByPayer': 1 if site.oxapay_fee_paid_by_payer else 0,
        'callbackUrl': _callback_url(),
        'orderId': payment.order_id,
        'description': f'Wallet top-up / VPN order {payment.order_id}',
    }
    data, http_status, raw_text = _post_json(
        OXAPAY_LEGACY_INVOICE_URL, payload, {'Content-Type': 'application/json'}
    )

    result = data.get('result') if isinstance(data, dict) else None
    message = (data.get('message') if isinstance(data, dict) else None) or raw_text or f'HTTP {http_status}'
    if result != LEGACY_SUCCESS_CODE:
        raise OxaPayError(f'خطا در ساخت فاکتور OxaPay: {message}')

    payment_url = data.get('payLink')
    if not payment_url:
        raise OxaPayError(f'لینک پرداخت در پاسخ OxaPay پیدا نشد. پاسخ خام: {data}')
    return {
        'payment_url': payment_url,
        'track_id': str(data.get('trackId') or ''),
        'raw': data,
        'api': 'legacy',
    }


def create_invoice(payment: Payment) -> Payment:
    site = SiteSetting.get_solo()
    merchant_key = get_merchant_key()
    if not merchant_key:
        raise OxaPayError('کلید API درگاه OxaPay در تنظیمات ثبت نشده است.')

    try:
        result = create_invoice_v1(payment, site, merchant_key)
    except OxaPayAuthError as auth_exc:
        # Older dashboard keys are only valid on the legacy endpoint.
        try:
            result = create_invoice_legacy(payment, site, merchant_key)
        except OxaPayError as legacy_exc:
            raise OxaPayError(
                f'کلید API درگاه OxaPay پذیرفته نشد. پاسخ v1: {auth_exc} | پاسخ نسخه قدیمی: {legacy_exc} | '
                'بررسی کنید: ۱) حتما Merchant API Key باشد، نه Payout یا General. '
                '۲) اگر در پنل OxaPay محدودیت IP فعال کرده‌اید، IP این سرور را مجاز کنید. '
                '۳) حساب OxaPay شما فعال و تاییدشده باشد.'
            ) from legacy_exc

    payment.payment_url = result['payment_url']
    payment.track_id = result['track_id']
    payment.raw_payload = result['raw']
    payment.save(update_fields=['payment_url', 'track_id', 'raw_payload', 'updated_at'])
    return payment


def validate_webhook_signature(raw_body: bytes, received_hmac: str | None) -> bool:
    """Verify the HMAC-SHA512 the gateway computes over the raw request body."""
    site = SiteSetting.get_solo()
    merchant_key = get_merchant_key()
    if not merchant_key or not received_hmac:
        return False

    candidates = [merchant_key]
    if site.oxapay_sandbox:
        # Legacy sandbox invoices are issued under the reserved merchant value,
        # so their callbacks are signed with it rather than the account key.
        candidates.append(LEGACY_SANDBOX_MERCHANT)

    for key in candidates:
        digest = hmac.new(key.encode(), raw_body, hashlib.sha512).hexdigest()
        if hmac.compare_digest(digest.lower(), received_hmac.lower()):
            return True
    return False
