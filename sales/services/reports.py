"""Aggregate what was sold over a period.

One place computes the numbers, so the panel page, the CSV export and the
nightly Telegram summary can never disagree about what "revenue this month"
means. Revenue is counted from orders that reached PAID or PROVISIONED, which is
the point at which the customer's money became the shop's.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from sales.models import (
    CardPaymentRequest,
    DiscountRedemption,
    Order,
    PartnerInvoice,
    PartnerInvoiceItem,
    Payment,
    TelegramUser,
)

# Orders in these states represent money actually taken.
EARNED_STATES = [Order.Status.PAID, Order.Status.PROVISIONED]


@dataclass
class Report:
    start: datetime
    end: datetime
    label: str
    revenue_toman: int = 0
    discount_toman: int = 0
    order_count: int = 0
    new_users: int = 0
    wallet_topup_toman: int = 0
    expiring_soon: int = 0
    active_subscriptions: int = 0
    by_source: list[dict] = field(default_factory=list)
    by_plan: list[dict] = field(default_factory=list)
    by_service: list[dict] = field(default_factory=list)
    by_discount: list[dict] = field(default_factory=list)
    daily: list[dict] = field(default_factory=list)
    # ── همکاری در فروش ────────────────────────────────────────────────────
    # Owed, not earned: charges sitting on invoices that have not been settled.
    # Kept out of revenue_toman entirely, which is the whole point of the split.
    partner_owed_toman: int = 0
    partner_overdue_toman: int = 0
    # Settled partner charges in this period, counted off the invoice rather
    # than off the order, because a renewal is a second charge on an order that
    # already had one.
    partner_settled_toman: int = 0
    partner_margin_toman: int = 0
    by_partner: list[dict] = field(default_factory=list)

    @property
    def average_order_toman(self) -> int:
        return int(self.revenue_toman / self.order_count) if self.order_count else 0

    @property
    def gross_toman(self) -> int:
        """What the same orders would have brought in with no codes given out."""
        return self.revenue_toman + self.discount_toman


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Local midnight to midnight, as timezone-aware datetimes."""
    start = timezone.make_aware(datetime.combine(day, datetime.min.time()))
    return start, start + timedelta(days=1)


def month_bounds(day: date) -> tuple[datetime, datetime]:
    first = day.replace(day=1)
    next_first = (first + timedelta(days=32)).replace(day=1)
    return day_bounds(first)[0], day_bounds(next_first)[0]


def named_range(name: str) -> tuple[datetime, datetime, str]:
    """Resolve a quick-pick range into bounds and a label for the heading."""
    today = timezone.localdate()
    if name == 'today':
        start, end = day_bounds(today)
        return start, end, 'امروز'
    if name == 'yesterday':
        start, end = day_bounds(today - timedelta(days=1))
        return start, end, 'دیروز'
    if name == 'week':
        start = day_bounds(today - timedelta(days=6))[0]
        return start, day_bounds(today)[1], '۷ روز گذشته'
    if name == 'last_month':
        last_day_prev = today.replace(day=1) - timedelta(days=1)
        start, end = month_bounds(last_day_prev)
        return start, end, 'ماه گذشته'
    if name == 'year':
        start = day_bounds(today - timedelta(days=364))[0]
        return start, day_bounds(today)[1], '۱۲ ماه گذشته'
    start, end = month_bounds(today)
    return start, end, 'این ماه'


def build(start: datetime, end: datetime, label: str = '') -> Report:
    """Every figure for one half-open period [start, end)."""
    # Ranged on revenue_at rather than created_at. For every order that predates
    # the partner program the two are the same instant, so historical periods
    # come out unchanged; what it buys is that a partner's credit order — which
    # is provisioned before any money arrives — has no revenue_at yet and so
    # cannot be counted as income until its invoice is settled.
    orders = Order.objects.filter(
        status__in=EARNED_STATES, revenue_at__gte=start, revenue_at__lt=end
    )

    totals = orders.aggregate(
        revenue=Sum('amount_toman'),
        discount=Sum('discount_toman'),
        count=Count('id'),
    )
    report = Report(
        start=start,
        end=end,
        label=label,
        revenue_toman=int(totals['revenue'] or 0),
        discount_toman=int(totals['discount'] or 0),
        order_count=int(totals['count'] or 0),
    )

    report.new_users = TelegramUser.objects.filter(created_at__gte=start, created_at__lt=end).count()

    # Money that came in as wallet credit, whichever way it arrived. Kept apart
    # from revenue because a top-up is not a sale until it is spent.
    gateway = Payment.objects.filter(
        status=Payment.Status.PAID, created_at__gte=start, created_at__lt=end
    ).aggregate(total=Sum('amount_toman'))['total'] or 0
    card = CardPaymentRequest.objects.filter(
        status=CardPaymentRequest.Status.APPROVED, created_at__gte=start, created_at__lt=end
    ).aggregate(total=Sum('amount_toman'))['total'] or 0
    report.wallet_topup_toman = int(gateway) + int(card)

    now = timezone.now()
    report.active_subscriptions = Order.objects.filter(
        status=Order.Status.PROVISIONED, expires_at__gt=now
    ).count()
    report.expiring_soon = Order.objects.filter(
        status=Order.Status.PROVISIONED,
        expires_at__gt=now,
        expires_at__lte=now + timedelta(days=7),
    ).count()

    source_labels = dict(Order.Source.choices)
    report.by_source = [
        {
            'key': row['source'],
            'title': source_labels.get(row['source'], row['source']),
            'count': row['count'],
            'revenue': int(row['revenue'] or 0),
        }
        for row in orders.values('source').annotate(count=Count('id'), revenue=Sum('amount_toman')).order_by('-revenue')
    ]

    report.by_plan = [
        {
            'title': f'{row["service__name"]} / {row["plan__name"]}',
            'count': row['count'],
            'revenue': int(row['revenue'] or 0),
        }
        for row in orders.values('plan__name', 'service__name')
        .annotate(count=Count('id'), revenue=Sum('amount_toman'))
        .order_by('-revenue')
    ]

    report.by_service = [
        {
            'title': row['service__name'],
            'count': row['count'],
            'revenue': int(row['revenue'] or 0),
        }
        for row in orders.values('service__name')
        .annotate(count=Count('id'), revenue=Sum('amount_toman'))
        .order_by('-revenue')
    ]

    report.by_discount = [
        {
            'title': row['code__code'],
            'count': row['count'],
            'discount': int(row['given'] or 0),
        }
        for row in DiscountRedemption.objects.filter(created_at__gte=start, created_at__lt=end)
        .values('code__code')
        .annotate(count=Count('id'), given=Sum('amount_toman'))
        .order_by('-given')
    ]

    _add_partner_figures(report, start, end)
    report.daily = _daily_rows(start, end)
    return report


def _add_partner_figures(report: Report, start: datetime, end: datetime) -> None:
    """Partner money, in the three shapes it comes in.

    What partners still owe is a running total, not a figure for the period —
    an invoice opened last month and still unpaid is debt today — so it is
    measured now rather than inside the range.

    The per-partner breakdown is deliberately ranged on when the order was
    *sold*, not on when it was paid for. "How much did Ali sell in Mordad" is a
    question about Mordad's orders; ranging it on revenue would move a sale into
    the month its invoice happened to settle, and would drop unpaid ones
    entirely.
    """
    outstanding = PartnerInvoice.objects.filter(
        status__in=[PartnerInvoice.Status.OPEN, PartnerInvoice.Status.OVERDUE]
    ).aggregate(total=Sum('total_toman'))['total'] or 0
    overdue = PartnerInvoice.objects.filter(
        status=PartnerInvoice.Status.OVERDUE
    ).aggregate(total=Sum('total_toman'))['total'] or 0
    report.partner_owed_toman = int(outstanding)
    report.partner_overdue_toman = int(overdue)

    # Credit sales become income when their invoice is paid, so they are counted
    # off the invoice line at the moment of settlement.
    settled = PartnerInvoiceItem.objects.filter(
        is_cancelled=False,
        invoice__status=PartnerInvoice.Status.PAID,
        invoice__paid_at__gte=start,
        invoice__paid_at__lt=end,
    ).aggregate(total=Sum('amount_toman'))['total'] or 0
    report.partner_settled_toman = int(settled)

    sold = Order.objects.filter(
        partner__isnull=False,
        status__in=EARNED_STATES,
        created_at__gte=start,
        created_at__lt=end,
    )

    # What a walk-in would have paid for the same orders, less what the partner
    # did. Covers both kinds of partner, because both record the retail price.
    margin = sold.aggregate(base=Sum('partner_base_toman'), paid=Sum('amount_toman'))
    report.partner_margin_toman = int((margin['base'] or 0) - (margin['paid'] or 0))

    report.by_partner = [
        {
            'title': row['partner__display_name'],
            'count': row['count'],
            'revenue': int(row['revenue'] or 0),
        }
        for row in sold.values('partner__display_name')
        .annotate(count=Count('id'), revenue=Sum('amount_toman'))
        .order_by('-revenue')
    ]


def _daily_rows(start: datetime, end: datetime) -> list[dict]:
    """Per-day revenue, capped so a long range cannot produce a huge table."""
    span_days = max(1, (end - start).days)
    if span_days > 92:
        return []

    rows = []
    cursor = timezone.localtime(start).date()
    last = timezone.localtime(end - timedelta(seconds=1)).date()
    while cursor <= last:
        day_start, day_end = day_bounds(cursor)
        totals = Order.objects.filter(
            status__in=EARNED_STATES, revenue_at__gte=day_start, revenue_at__lt=day_end
        ).aggregate(revenue=Sum('amount_toman'), count=Count('id'))
        rows.append({
            'date': cursor,
            'count': int(totals['count'] or 0),
            'revenue': int(totals['revenue'] or 0),
        })
        cursor += timedelta(days=1)
    return rows


def to_csv(report: Report) -> bytes:
    """The same report as a spreadsheet, for handing to an accountant.

    Written with a BOM because Excel otherwise reads the Persian headers as
    mojibake.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['گزارش فروش', report.label])
    writer.writerow(['از', timezone.localtime(report.start).strftime('%Y-%m-%d %H:%M')])
    writer.writerow(['تا', timezone.localtime(report.end).strftime('%Y-%m-%d %H:%M')])
    writer.writerow([])

    writer.writerow(['شاخص', 'مقدار'])
    writer.writerow(['درآمد فروش / تومان', report.revenue_toman])
    writer.writerow(['تخفیف داده‌شده / تومان', report.discount_toman])
    writer.writerow(['فروش قبل از تخفیف / تومان', report.gross_toman])
    writer.writerow(['تعداد سفارش', report.order_count])
    writer.writerow(['میانگین هر سفارش / تومان', report.average_order_toman])
    writer.writerow(['کاربر جدید', report.new_users])
    writer.writerow(['شارژ کیف پول / تومان', report.wallet_topup_toman])
    writer.writerow(['اشتراک فعال', report.active_subscriptions])
    writer.writerow(['منقضی تا ۷ روز آینده', report.expiring_soon])
    writer.writerow(['طلب از همکاران / تومان', report.partner_owed_toman])
    writer.writerow(['از این مقدار معوق / تومان', report.partner_overdue_toman])
    writer.writerow(['فاکتور همکاری تسویه‌شده در این بازه / تومان', report.partner_settled_toman])
    writer.writerow(['تخفیف داده‌شده به همکاران / تومان', report.partner_margin_toman])

    for title, rows, value_key, value_title in (
        ('روش پرداخت', report.by_source, 'revenue', 'درآمد'),
        ('سرویس', report.by_service, 'revenue', 'درآمد'),
        ('پلن', report.by_plan, 'revenue', 'درآمد'),
        ('همکار', report.by_partner, 'revenue', 'درآمد'),
        ('کد تخفیف', report.by_discount, 'discount', 'تخفیف داده‌شده'),
    ):
        writer.writerow([])
        writer.writerow([title, 'تعداد', value_title])
        for row in rows:
            writer.writerow([row['title'], row['count'], row[value_key]])

    if report.daily:
        writer.writerow([])
        writer.writerow(['تاریخ', 'تعداد سفارش', 'درآمد'])
        for row in report.daily:
            writer.writerow([row['date'].strftime('%Y-%m-%d'), row['count'], row['revenue']])

    return '﻿'.encode('utf-8') + buffer.getvalue().encode('utf-8')


def telegram_summary(report: Report) -> str:
    """The short version the operator gets pushed in Telegram."""
    from sales.services.formatting import fa_digits, toman

    lines = [
        f'📊 <b>گزارش فروش — {report.label}</b>',
        f'{fa_digits(timezone.localtime(report.start).strftime("%Y-%m-%d"))}'
        f' تا {fa_digits(timezone.localtime(report.end - timedelta(seconds=1)).strftime("%Y-%m-%d"))}',
        '',
        f'💰 درآمد: <b>{toman(report.revenue_toman)}</b>',
        f'🛒 سفارش: {fa_digits(report.order_count)}',
    ]
    if report.order_count:
        lines.append(f'📈 میانگین هر سفارش: {toman(report.average_order_toman)}')
    if report.discount_toman:
        lines.append(f'🎟 تخفیف داده‌شده: {toman(report.discount_toman)}')
    lines += [
        f'👥 کاربر جدید: {fa_digits(report.new_users)}',
        f'👛 شارژ کیف پول: {toman(report.wallet_topup_toman)}',
        '',
        f'✅ اشتراک فعال: {fa_digits(report.active_subscriptions)}',
        f'⏳ منقضی تا ۷ روز آینده: {fa_digits(report.expiring_soon)}',
    ]

    # Shown only when there is something to show, so a shop with no partners
    # keeps the report it had.
    if report.partner_owed_toman or report.partner_settled_toman or report.by_partner:
        lines += ['', '<b>🤝 همکاری در فروش</b>']
        if report.partner_settled_toman:
            lines.append(f'✅ فاکتور تسویه‌شده در این بازه: {toman(report.partner_settled_toman)}')
        if report.partner_owed_toman:
            lines.append(f'🧾 طلب از همکاران: {toman(report.partner_owed_toman)}')
        if report.partner_overdue_toman:
            lines.append(f'⛔️ از این مقدار معوق: <b>{toman(report.partner_overdue_toman)}</b>')
        if report.partner_margin_toman:
            lines.append(f'📉 تخفیف داده‌شده به همکاران: {toman(report.partner_margin_toman)}')

    if report.by_plan:
        lines.append('')
        lines.append('<b>پرفروش‌ترین پلن‌ها:</b>')
        for row in report.by_plan[:5]:
            lines.append(f'• {row["title"]} — {fa_digits(row["count"])} فروش، {toman(row["revenue"])}')

    if report.by_discount:
        lines.append('')
        lines.append('<b>کدهای تخفیف:</b>')
        for row in report.by_discount[:5]:
            lines.append(f'• {row["title"]} — {fa_digits(row["count"])} بار، {toman(row["discount"])}')

    return '\n'.join(lines)
