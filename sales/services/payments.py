"""Settle a gateway payment exactly once.

Shared by the OxaPay webhook and by the panel's manual approval, so a payment
whose callback never arrived is rescued through the same path that a normal one
takes, with the same protection against crediting twice.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from sales.models import Payment, Plan, TelegramUser, WalletTransaction
from sales.services.delivery import send_order, send_text
from sales.services.discounts import resolve
from sales.services.formatting import toman
from sales.services.provisioning import create_order_from_wallet, provision_order


def settle_payment(payment: Payment, note: str = '') -> bool:
    """Mark paid, credit the wallet, then buy and deliver a pending plan.

    Returns False when the payment was already settled, so a repeated webhook
    or a second click in the panel cannot credit the same money twice.
    """
    with transaction.atomic():
        locked = Payment.objects.select_for_update().select_related('user').get(pk=payment.pk)
        if locked.status == Payment.Status.PAID:
            return False

        locked.status = Payment.Status.PAID
        if note:
            payload = dict(locked.raw_payload or {})
            payload['manual_note'] = note
            locked.raw_payload = payload
        locked.save(update_fields=['status', 'raw_payload', 'updated_at'])

        user = TelegramUser.objects.select_for_update().get(pk=locked.user_id)
        user.wallet_balance_toman += Decimal(locked.amount_toman)
        user.save(update_fields=['wallet_balance_toman', 'updated_at'])
        WalletTransaction.objects.create(
            user=user,
            kind=WalletTransaction.Kind.CREDIT,
            amount_toman=locked.amount_toman,
            balance_after_toman=user.wallet_balance_toman,
            description=f'شارژ کیف پول با {locked.get_provider_display()} / {locked.order_id}',
        )

    # Provisioning reaches the panel over the network, so it stays outside the
    # transaction: a failure there must not undo a confirmed payment.
    if locked.auto_purchase_after_paid and locked.pending_plan_id:
        try:
            plan = Plan.objects.get(pk=locked.pending_plan_id)
            # The code was accepted before the gateway redirect. It is checked
            # again here because it may have expired or run out while the
            # customer was paying; resolve() returns None in that case and the
            # purchase goes through at the plan's own price.
            discount = resolve(locked.discount_code_id, user, plan)
            order = provision_order(create_order_from_wallet(user, plan, discount=discount))
        except Exception as exc:  # noqa: BLE001
            send_text(
                user.chat_id,
                f'پرداخت شما تایید شد و کیف پولتان شارژ شد، اما ساخت خودکار سرویس خطا داد.\n'
                f'پشتیبانی بررسی می‌کند.\nخطا: {exc}',
            )
            return True
        send_order(order)
        return True

    send_text(user.chat_id, f'✅ پرداخت شما تایید شد و کیف پولتان به مبلغ {toman(locked.amount_toman)} شارژ شد.')
    return True
