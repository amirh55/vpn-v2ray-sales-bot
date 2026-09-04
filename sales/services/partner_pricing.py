"""What a plan costs one particular partner.

Three layers, most specific winning: a price agreed with this partner for this
plan, then the plan's general partner price, then the ordinary price. The
partner's discount percentage then comes off whatever survived.

The arithmetic itself lives on the Partner model, because the admin needs it to
show an effective price without importing a service. This module wraps it into
the shape the bot wants — both prices, the saving, and the retail figure to
strike through — mirroring how `discounts.DiscountQuote` is used elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sales.models import Partner, Plan


@dataclass(frozen=True)
class PartnerQuote:
    plan: Plan
    # What a walk-in customer would pay today. Frozen onto the order so the
    # margin can be reported even after the plan's price moves.
    list_toman: Decimal
    # The partner price before the partner's own discount percentage.
    partner_toman: Decimal
    final_toman: Decimal
    final_usd: Decimal
    discount_percent: int

    @property
    def saving_toman(self) -> Decimal:
        return max(Decimal('0'), self.list_toman - self.final_toman)

    @property
    def has_partner_price(self) -> bool:
        """Whether this partner is actually getting a break on this plan."""
        return self.final_toman < self.list_toman


def quote(partner: Partner, plan: Plan) -> PartnerQuote:
    return PartnerQuote(
        plan=plan,
        list_toman=Decimal(plan.price_toman),
        partner_toman=partner.base_price_toman(plan),
        final_toman=partner.final_price_toman(plan),
        final_usd=partner.final_price_usd(plan),
        discount_percent=int(partner.discount_percent or 0),
    )


def price_lines(quote: PartnerQuote) -> str:
    """The price block shown to a partner before they commit."""
    from sales.services.formatting import toman

    if not quote.has_partner_price:
        return f'💰 قیمت: <b>{toman(quote.final_toman)}</b>'

    lines = [
        f'💰 قیمت عادی: <s>{toman(quote.list_toman)}</s>',
        f'🤝 قیمت شما: <b>{toman(quote.final_toman)}</b>',
    ]
    if quote.discount_percent:
        lines.insert(1, f'   قیمت همکاری: {toman(quote.partner_toman)}')
        lines.append(f'   (شامل {quote.discount_percent}٪ تخفیف همکاری شما)')
    lines.append(f'📉 سود شما در هر فروش: {toman(quote.saving_toman)}')
    return '\n'.join(lines)
