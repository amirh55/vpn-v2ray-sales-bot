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
    Payment,
    TelegramUser,
)
from sales.services import jalali

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

    @property
    def start_local(self) -> datetime:
        return timezone.localtime(self.start)

    @property
    def end_local(self) -> datetime:
        """The last moment inside the range, so a label never reads as the next day."""
        return timezone.localtime(self.end) - timedelta(seconds=1)

    @property
    def range_title(self) -> str:
        """The period spelled out in Shamsi, for a heading."""
        start_text = jalali.format_long(self.start_local)
        end_text = jalali.format_long(self.end_local)
        return start_text if start_text == end_text else f'{start_text} تا {end_text}'

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
    """The Jalali month this date falls in.

    Shamsi months, not Gregorian ones: an Iranian shop's "this month" is Mordad,
    and a Gregorian month boundary would cut the figures in the middle of it.
    """
    return (
        day_bounds(jalali.month_first_day(day))[0],
        day_bounds(jalali.next_month_first_day(day))[0],
    )


def named_range(name: str) -> tuple[datetime, datetime, str]:
    """Resolve a quick-pick range into bounds and a label for the heading."""
    today = timezone.localdate()
    if name == 'today':
        start, end = day_bounds(today)
        return start, end, f'امروز، {jalali.format_long(today)}'
    if name == 'yesterday':
        yesterday = today - timedelta(days=1)
        start, end = day_bounds(yesterday)
        return start, end, f'دیروز، {jalali.format_long(yesterday)}'
    if name == 'week':
        start = day_bounds(today - timedelta(days=6))[0]
        return start, day_bounds(today)[1], '۷ روز گذشته'
    if name == 'last_month':
        previous = jalali.previous_month_day(today)
        start, end = month_bounds(previous)
        return start, end, jalali.month_title(previous)
    if name == 'year':
        start = day_bounds(today - timedelta(days=364))[0]
        return start, day_bounds(today)[1], '۱۲ ماه گذشته'
    start, end = month_bounds(today)
    return start, end, jalali.month_title(today)


def build(start: datetime, end: datetime, label: str = '') -> Report:
    """Every figure for one half-open period [start, end)."""
    orders = Order.objects.filter(status__in=EARNED_STATES, created_at__gte=start, created_at__lt=end)

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

    report.daily = _daily_rows(start, end)
    return report


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
            status__in=EARNED_STATES, created_at__gte=day_start, created_at__lt=day_end
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
    # Latin digits in the file: a spreadsheet cannot sort or filter Persian ones.
    writer.writerow(['از', jalali.format_date(report.start_local, latin_digits=True)])
    writer.writerow(['تا', jalali.format_date(report.end_local, latin_digits=True)])
    writer.writerow(['بازه میلادی', f'{report.start_local:%Y-%m-%d} تا {report.end_local:%Y-%m-%d}'])
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

    for title, rows, value_key, value_title in (
        ('روش پرداخت', report.by_source, 'revenue', 'درآمد'),
        ('سرویس', report.by_service, 'revenue', 'درآمد'),
        ('پلن', report.by_plan, 'revenue', 'درآمد'),
        ('کد تخفیف', report.by_discount, 'discount', 'تخفیف داده‌شده'),
    ):
        writer.writerow([])
        writer.writerow([title, 'تعداد', value_title])
        for row in rows:
            writer.writerow([row['title'], row['count'], row[value_key]])

    if report.daily:
        writer.writerow([])
        writer.writerow(['تاریخ شمسی', 'تاریخ میلادی', 'تعداد سفارش', 'درآمد'])
        for row in report.daily:
            writer.writerow([
                jalali.format_date(row['date'], latin_digits=True),
                row['date'].strftime('%Y-%m-%d'),
                row['count'],
                row['revenue'],
            ])

    return '﻿'.encode('utf-8') + buffer.getvalue().encode('utf-8')


def telegram_summary(report: Report) -> str:
    """The short version the operator gets pushed in Telegram."""
    from sales.services.formatting import fa_digits, toman

    lines = [
        f'📊 <b>گزارش فروش — {report.label}</b>',
        report.range_title,
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
