"""Invoices, credit and the consequences of not paying.

A credit partner's orders are provisioned before any money arrives. What keeps
that safe is here: a cycle that opens with the first order and closes on a due
date, a credit ceiling checked under a lock, one reminder before the deadline,
configs switched off — never deleted — when the deadline passes, and switched
back on when the invoice is paid.

Three rules that look like details but are the whole design:

  * Only an *overdue* invoice blocks new orders. An invoice that is merely open
    must not, or the billing cycle would hold exactly one order.
  * A new cycle opens on the next order, never at payment time, so a partner who
    stops selling never receives an invoice for nothing.
  * Re-enabling only touches configs this invoice switched off, and only those
    that are otherwise still alive. A config the operator disabled by hand, or
    one that expired meanwhile, must not come back.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from sales.models import (
    Order,
    Partner,
    PartnerInvoice,
    PartnerInvoiceItem,
    SiteSetting,
    TelegramUser,
    WalletTransaction,
)
from sales.services import jalali
from sales.services.formatting import toman
from sales.services.provisioning import set_order_client_enabled


class PartnerBillingError(RuntimeError):
    """Something the partner should be told about, in their own words."""


# ─────────────────────────────────────────────────────────────────────────────
# دوره و فاکتور
# ─────────────────────────────────────────────────────────────────────────────


def get_or_open_invoice(partner: Partner) -> PartnerInvoice:
    """The invoice this partner's next charge belongs on.

    Opens a cycle if none is running. The cycle is dated from this moment — the
    first order — not from the start of a month, which is what the operator
    asked for: order on the 10th at noon, due on the 17th at noon, and
    everything in between lands here.
    """
    invoice = partner.open_invoice()
    if invoice is not None:
        return invoice
    now = timezone.now()
    days = int(partner.billing_cycle_days or SiteSetting.get_solo().partner_default_cycle_days or 7)
    return PartnerInvoice.objects.create(
        partner=partner,
        status=PartnerInvoice.Status.OPEN,
        opened_at=now,
        due_at=now + timezone.timedelta(days=days),
    )


def add_item(
    invoice: PartnerInvoice,
    *,
    kind: str,
    title: str,
    amount_toman: Decimal,
    amount_usd: Decimal = Decimal('0'),
    order: Order | None = None,
) -> PartnerInvoiceItem:
    item = PartnerInvoiceItem.objects.create(
        invoice=invoice,
        order=order,
        kind=kind,
        title=title[:200],
        amount_toman=Decimal(amount_toman),
        amount_usd=Decimal(amount_usd),
    )
    invoice.recalculate_total()
    return item


def cancel_item(item: PartnerInvoiceItem, reason: str) -> None:
    """Take a charge off an invoice, and close the invoice if nothing is left.

    A partner left holding an open, zero-toman invoice could neither pay it nor
    escape it, and every later order would join it — so an emptied cycle is
    closed rather than kept.
    """
    item.is_cancelled = True
    item.cancelled_at = timezone.now()
    item.cancel_reason = reason[:200]
    item.save(update_fields=['is_cancelled', 'cancelled_at', 'cancel_reason', 'updated_at'])

    invoice = item.invoice
    invoice.recalculate_total()
    if invoice.total_toman <= 0 and not invoice.items.filter(is_cancelled=False).exists():
        invoice.status = PartnerInvoice.Status.CANCELLED
        invoice.save(update_fields=['status', 'updated_at'])


# ─────────────────────────────────────────────────────────────────────────────
# گاردهای سفارش
# ─────────────────────────────────────────────────────────────────────────────


def check_can_order(partner: Partner, amount_toman: Decimal) -> None:
    """Raise with a message for the partner if this order must not go through.

    Ordered cheapest check first. Every one of these is also re-checked inside
    the order transaction under a lock; this exists so the partner is told why
    before they have picked a customer name.
    """
    if not partner.is_active:
        raise PartnerBillingError('حساب همکاری شما غیرفعال است. با پشتیبانی تماس بگیرید.')

    if not SiteSetting.get_solo().is_shop_active:
        raise PartnerBillingError('فروشگاه موقتاً غیرفعال است.')

    if partner.billing_mode != Partner.BillingMode.CREDIT:
        return

    overdue = partner.overdue_invoice()
    if overdue is not None:
        raise PartnerBillingError(
            f'فاکتور {overdue.number} به مبلغ {toman(overdue.total_toman)} سررسید شده '
            f'({jalali.format_datetime(overdue.due_at)}) و هنوز پرداخت نشده است.\n\n'
            'تا تسویه این فاکتور نمی‌توانید سفارش جدید ثبت کنید، اما همچنان می‌توانید '
            'فاکتور را ببینید، پرداخت کنید و کانفیگ‌های قبلی را مدیریت کنید.'
        )

    if not partner.is_within_credit(amount_toman):
        remaining = partner.remaining_credit_toman() or Decimal('0')
        raise PartnerBillingError(
            'این سفارش از سقف اعتبار شما عبور می‌کند.\n\n'
            f'سقف اعتبار: {toman(partner.credit_limit_toman)}\n'
            f'بدهی فعلی: {toman(partner.current_debt_toman())}\n'
            f'اعتبار باقی‌مانده: {toman(max(Decimal("0"), remaining))}\n'
            f'مبلغ این سفارش: {toman(amount_toman)}'
        )


def charge_to_invoice(
    partner: Partner,
    *,
    kind: str,
    title: str,
    amount_toman: Decimal,
    amount_usd: Decimal = Decimal('0'),
    order: Order | None = None,
) -> PartnerInvoiceItem:
    """Put a charge on the partner's open cycle, re-checking credit under a lock.

    The lock matters: two orders arriving together would otherwise both measure
    the debt before either had been added, and both would pass a ceiling that
    only one of them fits under.
    """
    with transaction.atomic():
        locked = Partner.objects.select_for_update().get(pk=partner.pk)
        check_can_order(locked, amount_toman)
        invoice = get_or_open_invoice(locked)
        return add_item(
            invoice,
            kind=kind,
            title=title,
            amount_toman=amount_toman,
            amount_usd=amount_usd,
            order=order,
        )


def refund_window_open(item: PartnerInvoiceItem) -> bool:
    """Whether deleting this config can still take its charge off the invoice.

    Two conditions, both necessary: the invoice has not been settled, and the
    charge is recent. The window is measured from the charge, not from the start
    of the cycle, because what it is there to forgive is a mistake made at the
    moment of ordering.
    """
    if item.is_cancelled or item.invoice.is_settled:
        return False
    hours = int(SiteSetting.get_solo().partner_delete_refund_hours or 0)
    if hours <= 0:
        return False
    return timezone.now() - item.created_at <= timezone.timedelta(hours=hours)


def live_item_for_order(order: Order) -> PartnerInvoiceItem | None:
    """The most recent uncancelled charge for this order."""
    return (
        PartnerInvoiceItem.objects.filter(order=order, is_cancelled=False)
        .select_related('invoice')
        .order_by('-created_at')
        .first()
    )


# ─────────────────────────────────────────────────────────────────────────────
# سررسید، تعلیق و تسویه
# ─────────────────────────────────────────────────────────────────────────────


def suspend_invoice_orders(invoice: PartnerInvoice) -> int:
    """Switch off the configs this invoice paid for. Nothing is deleted.

    Point 9 of the brief: `Active → Disabled`, with the client id, subscription
    id, expiry date and links all left exactly as they are, so paying the
    invoice can put things back rather than rebuild them.
    """
    now = timezone.now()
    count = 0
    for order in invoice.orders().select_related('service', 'service__panel', 'user', 'plan', 'partner'):
        if order.status != Order.Status.PROVISIONED or order.suspended_at is not None:
            continue
        try:
            set_order_client_enabled(order, False)
        except Exception:  # noqa: BLE001
            # An unreachable panel must not lose the fact that this order is
            # suspended; the flag is what re-enabling reads, and a later pass
            # will push the change again.
            pass
        order.suspended_at = now
        order.suspended_by_invoice = invoice
        order.save(update_fields=['suspended_at', 'suspended_by_invoice', 'updated_at'])
        count += 1
    return count


def reactivate_invoice_orders(invoice: PartnerInvoice) -> int:
    """Put back only what this invoice took away, and only if it can come back.

    Three guards, each answering a real way this goes wrong:
      * `suspended_by_invoice` — a config the operator disabled by hand in the
        panel was never claimed by an invoice, so it is not touched.
      * still PROVISIONED and not out of traffic — a config that was deleted or
        used up meanwhile must not be resurrected.
      * expiry still ahead — a subscription that ran out during the suspension
        is finished, and paying the invoice does not buy more time.
    """
    now = timezone.now()
    count = 0
    orders = Order.objects.filter(suspended_by_invoice=invoice, suspended_at__isnull=False)
    for order in orders.select_related('service', 'service__panel', 'user', 'plan', 'partner'):
        revivable = (
            order.status == Order.Status.PROVISIONED
            and order.traffic_ended_at is None
            and (order.expires_at is None or order.expires_at > now)
        )
        if revivable:
            try:
                set_order_client_enabled(order, True)
                count += 1
            except Exception:  # noqa: BLE001
                pass
        # The flag is cleared either way: this invoice no longer has a claim on
        # the config, whether or not the config is worth switching on.
        order.suspended_at = None
        order.suspended_by_invoice = None
        order.save(update_fields=['suspended_at', 'suspended_by_invoice', 'updated_at'])
    return count


def settle_invoice(invoice: PartnerInvoice, *, via: str, note: str = '') -> bool:
    """Mark an invoice paid and undo its consequences. Idempotent.

    Returns False when it was already settled, so a repeated webhook, a second
    click in the admin and the card watcher cannot all pay the same invoice.
    """
    with transaction.atomic():
        locked = PartnerInvoice.objects.select_for_update().get(pk=invoice.pk)
        if locked.status in (PartnerInvoice.Status.PAID, PartnerInvoice.Status.CANCELLED):
            return False
        locked.status = PartnerInvoice.Status.PAID
        locked.paid_at = timezone.now()
        locked.settled_by = via
        if note:
            locked.admin_note = (locked.admin_note + '\n' + note).strip()
        locked.save(update_fields=['status', 'paid_at', 'settled_by', 'admin_note', 'updated_at'])

    # Reaching the panel stays outside the transaction: a panel that is down
    # must not roll back a payment that really happened.
    reactivate_invoice_orders(locked)
    invoice.refresh_from_db()
    return True


def pay_invoice_from_wallet(invoice: PartnerInvoice) -> bool:
    """Settle an invoice out of the partner's own wallet balance."""
    with transaction.atomic():
        locked = PartnerInvoice.objects.select_for_update().get(pk=invoice.pk)
        if locked.is_settled:
            raise PartnerBillingError('این فاکتور قبلاً تسویه شده است.')
        amount = Decimal(locked.total_toman)
        if amount <= 0:
            raise PartnerBillingError('این فاکتور مبلغی برای پرداخت ندارد.')
        user = TelegramUser.objects.select_for_update().get(pk=locked.partner.user_id)
        if user.wallet_balance_toman < amount:
            need = amount - user.wallet_balance_toman
            raise PartnerBillingError(
                f'موجودی کیف پول کافی نیست.\n\n'
                f'مبلغ فاکتور: {toman(amount)}\n'
                f'موجودی شما: {toman(user.wallet_balance_toman)}\n'
                f'کسری: {toman(need)}'
            )
        user.wallet_balance_toman -= amount
        user.save(update_fields=['wallet_balance_toman', 'updated_at'])
        WalletTransaction.objects.create(
            user=user,
            kind=WalletTransaction.Kind.DEBIT,
            amount_toman=amount,
            balance_after_toman=user.wallet_balance_toman,
            description=f'تسویه فاکتور همکاری {locked.number}',
        )

    return settle_invoice(locked, via=PartnerInvoice.SettledBy.WALLET)


# ─────────────────────────────────────────────────────────────────────────────
# ورکر: هشدار و سررسید
# ─────────────────────────────────────────────────────────────────────────────


def warning_text(invoice: PartnerInvoice) -> str:
    hours = int(SiteSetting.get_solo().partner_invoice_warn_hours or 24)
    return (
        f'⏰ <b>یادآوری سررسید فاکتور</b>\n\n'
        f'فاکتور {invoice.number} به مبلغ <b>{toman(invoice.total_toman)}</b>\n'
        f'در تاریخ {jalali.format_datetime(invoice.due_at)} سررسید می‌شود.\n\n'
        f'{hours} ساعت فرصت دارید فاکتور را پرداخت کنید.\n'
        'در صورت عدم پرداخت، کانفیگ‌های مربوط به این فاکتور غیرفعال خواهند شد.\n\n'
        'کانفیگ‌ها حذف نمی‌شوند و بعد از پرداخت دوباره فعال می‌شوند.'
    )


def overdue_text(invoice: PartnerInvoice, suspended: int) -> str:
    text = (
        f'⛔️ <b>فاکتور سررسید شد</b>\n\n'
        f'فاکتور {invoice.number} به مبلغ <b>{toman(invoice.total_toman)}</b> پرداخت نشد.\n'
    )
    if suspended:
        text += f'\n{suspended} کانفیگ مربوط به این فاکتور غیرفعال شد.\n'
    text += (
        '\nکانفیگ‌ها حذف نشده‌اند؛ تاریخ انقضا و اطلاعاتشان محفوظ است و '
        'بلافاصله بعد از پرداخت دوباره فعال می‌شوند.\n\n'
        'تا تسویه این فاکتور امکان ثبت سفارش جدید ندارید.'
    )
    return text


def due_soon_invoices():
    """Invoices inside the warning window that have not been warned yet."""
    now = timezone.now()
    hours = int(SiteSetting.get_solo().partner_invoice_warn_hours or 24)
    return PartnerInvoice.objects.filter(
        status=PartnerInvoice.Status.OPEN,
        warned_at__isnull=True,
        due_at__gt=now,
        due_at__lte=now + timezone.timedelta(hours=hours),
    ).select_related('partner', 'partner__user')


def claim_warning(invoice: PartnerInvoice) -> bool:
    """Take ownership of sending this invoice's one reminder.

    A conditional update rather than a read-then-write, so that two workers —
    which is the normal shape in webhook mode — cannot both decide the reminder
    is unsent. Whoever's update touches a row sends it; the other gets nothing.
    """
    claimed = PartnerInvoice.objects.filter(pk=invoice.pk, warned_at__isnull=True).update(
        warned_at=timezone.now(), updated_at=timezone.now()
    )
    return bool(claimed)


def overdue_invoices():
    return PartnerInvoice.objects.filter(
        status=PartnerInvoice.Status.OPEN, due_at__lte=timezone.now()
    ).select_related('partner', 'partner__user')


def claim_overdue(invoice: PartnerInvoice) -> bool:
    """Move an invoice to overdue exactly once."""
    claimed = PartnerInvoice.objects.filter(
        pk=invoice.pk, status=PartnerInvoice.Status.OPEN
    ).update(
        status=PartnerInvoice.Status.OVERDUE,
        suspended_at=timezone.now(),
        updated_at=timezone.now(),
    )
    return bool(claimed)


def run_billing_pass(send) -> dict[str, int]:
    """One sweep of the reminders and the deadlines.

    `send(chat_id, text)` is injected so this stays testable without a bot and
    usable from either process.
    """
    totals = {'warned': 0, 'overdue': 0, 'suspended': 0}

    for invoice in due_soon_invoices():
        if not claim_warning(invoice):
            continue
        send(invoice.partner.user.chat_id, warning_text(invoice))
        totals['warned'] += 1

    for invoice in overdue_invoices():
        if not claim_overdue(invoice):
            continue
        invoice.refresh_from_db()
        suspended = suspend_invoice_orders(invoice)
        totals['overdue'] += 1
        totals['suspended'] += suspended
        send(invoice.partner.user.chat_id, overdue_text(invoice, suspended))

    return totals
