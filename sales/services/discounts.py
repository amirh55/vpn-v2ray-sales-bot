"""Validate and consume discount codes.

A code is checked twice: once when the customer types it, so they get a clear
reason when it is refused, and once again under a row lock at the moment the
purchase is made. Without the second check two people redeeming the last use of
a code at the same time would both succeed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from sales.models import DiscountCode, DiscountRedemption, Order, Plan, SiteSetting, TelegramUser


class DiscountError(ValueError):
    """A code the customer cannot use, with a message meant for them."""


@dataclass
class DiscountQuote:
    """What a code does to one plan's two prices."""

    code: DiscountCode
    price_toman: int
    price_usd: Decimal
    off_toman: int
    off_usd: Decimal

    @property
    def percent(self) -> int:
        base = self.price_toman + self.off_toman
        if base <= 0:
            return 0
        return int(round(self.off_toman * 100 / base))


def normalize(raw: str) -> str:
    return (raw or '').strip().upper().replace(' ', '')


def quote(code: DiscountCode, plan: Plan) -> DiscountQuote:
    """Work out both discounted prices without touching the database.

    The toman and dollar prices are set independently on the plan, so the
    discount is applied to each on its own terms rather than converting one
    into the other and drifting with the exchange rate.
    """
    base_toman = int(plan.price_toman)
    base_usd = Decimal(plan.price_usd)

    if code.kind == DiscountCode.Kind.PERCENT:
        percent = max(0, min(100, int(code.percent)))
        off_toman = base_toman * percent // 100
        if code.max_discount_toman and off_toman > int(code.max_discount_toman):
            off_toman = int(code.max_discount_toman)
        off_usd = (base_usd * Decimal(percent) / Decimal(100)).quantize(Decimal('0.01'))
        if code.max_discount_toman:
            # Keep the dollar side under the same ceiling, expressed in dollars.
            rate = Decimal(SiteSetting.get_solo().dollar_rate_toman or 0)
            if rate > 0:
                cap_usd = (Decimal(code.max_discount_toman) / rate).quantize(Decimal('0.01'))
                off_usd = min(off_usd, cap_usd)
    else:
        off_toman = min(base_toman, int(code.amount_toman))
        rate = Decimal(SiteSetting.get_solo().dollar_rate_toman or 0)
        off_usd = (Decimal(off_toman) / rate).quantize(Decimal('0.01')) if rate > 0 else Decimal('0')

    off_toman = max(0, min(base_toman, off_toman))
    off_usd = max(Decimal('0'), min(base_usd, off_usd))
    return DiscountQuote(
        code=code,
        price_toman=base_toman - off_toman,
        price_usd=base_usd - off_usd,
        off_toman=off_toman,
        off_usd=off_usd,
    )


def _check_rules(code: DiscountCode, user: TelegramUser, plan: Plan) -> None:
    """Raise DiscountError with the reason this customer cannot use the code."""
    now = timezone.now()
    if not code.is_active:
        raise DiscountError('این کد تخفیف فعال نیست.')
    if code.valid_from and now < code.valid_from:
        raise DiscountError('زمان استفاده از این کد هنوز شروع نشده است.')
    if code.valid_until and now > code.valid_until:
        raise DiscountError('اعتبار این کد تخفیف تمام شده است.')
    if code.max_uses and code.used_count >= code.max_uses:
        raise DiscountError('ظرفیت استفاده از این کد تکمیل شده است.')

    if code.services.exists() and not code.services.filter(pk=plan.service_id).exists():
        raise DiscountError('این کد برای این سرویس نیست.')
    if code.plans.exists() and not code.plans.filter(pk=plan.pk).exists():
        raise DiscountError('این کد برای این پلن نیست.')

    if code.min_order_toman and Decimal(plan.price_toman) < Decimal(code.min_order_toman):
        raise DiscountError(
            f'این کد فقط برای خرید بالای {int(code.min_order_toman):,} تومان است.'
        )

    if code.max_uses_per_user:
        used = DiscountRedemption.objects.filter(code=code, user=user).count()
        if used >= code.max_uses_per_user:
            raise DiscountError('شما قبلاً از این کد استفاده کرده‌اید.')


def validate(raw_code: str, user: TelegramUser, plan: Plan) -> DiscountQuote:
    """Check a typed code against one plan and return what it is worth."""
    cleaned = normalize(raw_code)
    if not cleaned:
        raise DiscountError('کد تخفیف را وارد کنید.')
    code = DiscountCode.objects.filter(code=cleaned).first()
    if code is None:
        raise DiscountError('چنین کد تخفیفی وجود ندارد.')
    _check_rules(code, user, plan)

    result = quote(code, plan)
    if result.off_toman <= 0 and result.off_usd <= 0:
        raise DiscountError('این کد روی این پلن تخفیفی ایجاد نمی‌کند.')
    return result


def resolve(code_id: int | None, user: TelegramUser, plan: Plan) -> DiscountQuote | None:
    """Re-validate a code the customer picked earlier, returning None if it lapsed.

    Time passes between typing the code and paying, so a code can expire or run
    out in between. Returning None means the purchase goes ahead at full price
    rather than failing.
    """
    if not code_id:
        return None
    code = DiscountCode.objects.filter(pk=code_id).first()
    if code is None:
        return None
    try:
        _check_rules(code, user, plan)
    except DiscountError:
        return None
    result = quote(code, plan)
    return result if result.off_toman > 0 or result.off_usd > 0 else None


@transaction.atomic
def redeem(
    code: DiscountCode,
    user: TelegramUser,
    *,
    order: Order | None = None,
    off_toman: int = 0,
    off_usd: Decimal | None = None,
) -> DiscountRedemption | None:
    """Record one use of the code, refusing to go past its limits.

    Returns None when the last use was taken by someone else in the meantime;
    the caller has already charged the discounted price at that point, which is
    a better outcome than failing a paid purchase over one extra redemption.
    """
    locked = DiscountCode.objects.select_for_update().filter(pk=code.pk).first()
    if locked is None:
        return None
    if locked.max_uses and locked.used_count >= locked.max_uses:
        return None

    locked.used_count += 1
    locked.save(update_fields=['used_count', 'updated_at'])
    return DiscountRedemption.objects.create(
        code=locked,
        user=user,
        order=order,
        amount_toman=Decimal(int(off_toman)),
        amount_usd=off_usd if off_usd is not None else Decimal('0'),
    )
