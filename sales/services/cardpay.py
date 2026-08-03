"""Create and settle card-to-card invoices."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from sales.models import BankSms, CardPaymentRequest, SiteSetting, TelegramUser, WalletTransaction
from sales.services.banksms import (
    extract_amounts_toman,
    find_matching_request,
    generate_unique_amount,
    looks_like_credit,
    sender_allowed,
)


def get_or_create_sms_secret() -> str:
    """The secret is minted by SiteSetting.save() when the field is empty."""
    site = SiteSetting.get_solo()
    if not site.sms_webhook_secret:
        site.save(update_fields=['sms_webhook_secret', 'updated_at'])
    return site.sms_webhook_secret


def create_request(user: TelegramUser, base_amount_toman: int) -> CardPaymentRequest:
    """Open an invoice for a distinctive figure, valid for a limited window."""
    site = SiteSetting.get_solo()
    minutes = int(site.card_invoice_minutes or 30)
    amount = generate_unique_amount(int(base_amount_toman))
    return CardPaymentRequest.objects.create(
        user=user,
        base_amount_toman=Decimal(int(base_amount_toman)),
        amount_toman=Decimal(amount),
        expires_at=timezone.now() + timedelta(minutes=minutes),
    )


def approve_request(request: CardPaymentRequest, *, auto: bool, note: str = '') -> bool:
    """Credit the wallet exactly once for this invoice.

    Returns False when the invoice was already settled, so a duplicate SMS or a
    second click cannot pay the same invoice twice.
    """
    with transaction.atomic():
        locked = CardPaymentRequest.objects.select_for_update().get(pk=request.pk)
        if locked.status == CardPaymentRequest.Status.APPROVED:
            return False

        user = TelegramUser.objects.select_for_update().get(pk=locked.user_id)
        user.wallet_balance_toman += Decimal(locked.amount_toman)
        user.save(update_fields=['wallet_balance_toman', 'updated_at'])

        WalletTransaction.objects.create(
            user=user,
            kind=WalletTransaction.Kind.CREDIT,
            amount_toman=locked.amount_toman,
            balance_after_toman=user.wallet_balance_toman,
            description=f'کارت‌به‌کارت #{locked.pk}' + (' (تایید خودکار)' if auto else ''),
        )

        locked.status = CardPaymentRequest.Status.APPROVED
        locked.auto_approved = auto
        if note:
            locked.admin_note = (locked.admin_note + '\n' + note).strip()
        locked.save(update_fields=['status', 'auto_approved', 'admin_note', 'updated_at'])

    request.refresh_from_db()
    return True


def process_incoming_sms(sender: str, text: str) -> BankSms:
    """Store a forwarded SMS and settle the invoice it pays, if any."""
    sms = BankSms(sender=(sender or '')[:64], raw_text=text or '')

    if not sender_allowed(sender):
        sms.note = 'فرستنده مجاز نیست'
        sms.save()
        return sms

    if not looks_like_credit(text):
        sms.note = 'پیامک واریز نیست'
        sms.save()
        return sms

    amounts = extract_amounts_toman(text)
    if not amounts:
        sms.note = 'مبلغی در پیامک پیدا نشد'
        sms.save()
        return sms

    matched = find_matching_request(amounts)
    if not matched:
        sms.parsed_amount_toman = Decimal(amounts[0])
        sms.note = 'فاکتور باز با این مبلغ پیدا نشد'
        sms.save()
        return sms

    sms.parsed_amount_toman = Decimal(int(matched.amount_toman))
    sms.matched_request = matched
    site = SiteSetting.get_solo()
    if not site.card_auto_confirm_enabled:
        sms.note = 'تایید خودکار خاموش است؛ منتظر تایید مدیر'
        sms.save()
        return sms

    credited = approve_request(matched, auto=True, note='تایید خودکار با پیامک بانکی')
    sms.note = 'تایید و شارژ شد' if credited else 'قبلا تایید شده بود'
    sms.save()
    return sms
