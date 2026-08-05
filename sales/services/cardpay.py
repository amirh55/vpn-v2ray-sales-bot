"""Create and settle card-to-card invoices."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from sales.models import BankSms, CardPaymentRequest, SiteSetting, TelegramUser, WalletTransaction
from sales.services.delivery import send_order, send_text
from sales.services.discounts import resolve
from sales.services.provisioning import create_order_from_wallet, provision_order
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


def create_request(user: TelegramUser, base_amount_toman: int, plan=None, discount=None) -> CardPaymentRequest:
    """Open an invoice for a distinctive figure, valid for a limited window.

    Passing a plan turns this into a purchase: the service is delivered as soon
    as the transfer is confirmed, instead of only topping up the wallet. A
    DiscountQuote is remembered on the row so the same code still applies when
    the transfer lands, possibly hours later.
    """
    site = SiteSetting.get_solo()
    minutes = int(site.card_invoice_minutes or 30)
    amount = generate_unique_amount(int(base_amount_toman))
    return CardPaymentRequest.objects.create(
        user=user,
        base_amount_toman=Decimal(int(base_amount_toman)),
        amount_toman=Decimal(amount),
        expires_at=timezone.now() + timedelta(minutes=minutes),
        pending_plan=plan,
        auto_purchase_after_paid=bool(plan),
        discount_code=discount.code if discount else None,
        discount_toman=Decimal(discount.off_toman) if discount else Decimal('0'),
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

    # Provisioning talks to the panel over the network, so it stays outside the
    # transaction. A failure here leaves the money in the wallet, where the
    # customer can spend it, rather than rolling back a confirmed payment.
    if locked.auto_purchase_after_paid and locked.pending_plan_id and not locked.created_order_id:
        try:
            # Re-checked rather than trusted: a transfer can land after the code
            # has expired, and resolve() returns None then.
            discount = resolve(locked.discount_code_id, locked.user, locked.pending_plan)
            order = create_order_from_wallet(locked.user, locked.pending_plan, discount=discount)
            order = provision_order(order)
            locked.created_order = order
            locked.save(update_fields=['created_order', 'updated_at'])
        except Exception as exc:  # noqa: BLE001
            locked.admin_note = (
                locked.admin_note + f'\nساخت خودکار سرویس ناموفق بود: {exc}'
            ).strip()
            locked.save(update_fields=['admin_note', 'updated_at'])

    # Deliver straight away rather than waiting for the bot's watcher, so the
    # operator sees the result immediately. The watcher still covers anything
    # left unnotified here, which is why notified_at is only stamped on success.
    if notify_customer(locked):
        locked.notified_at = timezone.now()
        locked.save(update_fields=['notified_at', 'updated_at'])

    request.refresh_from_db()
    return True


def notify_customer(request: CardPaymentRequest) -> bool:
    """Tell the customer their transfer landed, with the config when there is one."""
    if request.created_order_id:
        return send_order(request.created_order)
    if request.auto_purchase_after_paid and request.pending_plan_id:
        return send_text(
            request.user.chat_id,
            '✅ واریز شما تایید و کیف پولتان شارژ شد، اما ساخت خودکار سرویس انجام نشد. '
            'پشتیبانی پیگیری می‌کند.',
        )
    return send_text(
        request.user.chat_id,
        f'✅ واریز شما شناسایی شد.\nکیف پول شما به مبلغ {int(request.amount_toman):,} تومان شارژ شد.',
    )


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
