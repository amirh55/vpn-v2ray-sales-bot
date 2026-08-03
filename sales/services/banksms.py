"""Match forwarded bank SMS against pending card-to-card invoices.

Each invoice asks for a slightly different figure (the price plus a random
tail), so an incoming deposit amount identifies exactly one invoice. That is
what makes unattended confirmation possible without a payment gateway.

Iranian bank messages almost always state the amount in rial while the shop
prices in toman, so both readings are considered.
"""

from __future__ import annotations

import random
import re
from decimal import Decimal

from django.utils import timezone

from sales.models import CardPaymentRequest, SiteSetting

# Tail added to the price so two open invoices never ask for the same figure.
TAIL_STEP = 10
TAIL_MIN_UNITS = 1
TAIL_MAX_UNITS = 99

PERSIAN_ARABIC_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')

# A deposit, not a purchase or withdrawal. Without this a card payment made
# *from* the shop account could be mistaken for an incoming transfer.
CREDIT_WORDS = ('واریز', 'افزایش', 'بستانکار', 'دریافت', 'وصول', 'credit', 'deposit')
DEBIT_WORDS = ('برداشت', 'خرید', 'کاهش', 'بدهکار', 'انتقال از', 'debit', 'withdraw')

RIAL_WORDS = ('ریال', 'rial', 'rls', 'ir')
TOMAN_WORDS = ('تومان', 'تومن', 'toman', 'tmn')

# Thousands separators seen in Iranian SMS: ASCII comma, Arabic comma and the
# Arabic thousands separator. Whitespace is deliberately excluded, or a balance
# on the next line would be glued onto the amount.
AMOUNT_RE = re.compile(r"\d[\d,،٬']*\d|\d+")

# Numbers on these lines are the account balance, not the transfer.
BALANCE_WORDS = ('مانده', 'موجودی', 'balance')


def normalize_digits(text: str) -> str:
    return (text or '').translate(PERSIAN_ARABIC_DIGITS)


def generate_unique_amount(base_amount: int) -> int:
    """Return the exact figure to ask for, unused by any live invoice."""
    base = int(base_amount)
    taken = set(
        CardPaymentRequest.objects.filter(
            status=CardPaymentRequest.Status.PENDING,
            expires_at__gt=timezone.now(),
        ).values_list('amount_toman', flat=True)
    )
    taken = {int(value) for value in taken}

    options = [base + TAIL_STEP * n for n in range(TAIL_MIN_UNITS, TAIL_MAX_UNITS + 1)]
    free = [value for value in options if value not in taken]
    # Every tail in use is vanishingly unlikely, but never hand back a
    # duplicate: an ambiguous amount could credit the wrong customer.
    return random.choice(free) if free else random.choice(options)


def _numbers_in(text: str) -> list[int]:
    numbers = []
    for chunk in AMOUNT_RE.findall(text):
        digits = re.sub(r'\D', '', chunk)
        if digits:
            numbers.append(int(digits))
    return numbers


def _candidate_numbers(text: str) -> list[int]:
    """Numbers that could be the transferred amount.

    Balance lines are set aside so a balance that happens to equal an open
    invoice cannot confirm someone else's payment.
    """
    amount_lines, balance_lines = [], []
    for line in text.splitlines():
        target = balance_lines if any(w in line.lower() for w in BALANCE_WORDS) else amount_lines
        target.append(line)

    numbers = _numbers_in('\n'.join(amount_lines))
    return numbers or _numbers_in('\n'.join(balance_lines))


def extract_amounts_toman(raw_text: str) -> list[int]:
    """Return every plausible toman reading of the amounts in the message.

    Both the literal figure and figure/10 are returned when the currency is
    unstated, because banks differ and a wrong guess means a missed payment.
    """
    text = normalize_digits(raw_text or '')
    lowered = text.lower()
    numbers = _candidate_numbers(text)
    if not numbers:
        return []

    says_rial = any(word in lowered for word in RIAL_WORDS)
    says_toman = any(word in lowered for word in TOMAN_WORDS)

    readings: list[int] = []
    for number in numbers:
        if says_toman and not says_rial:
            readings.append(number)
        elif says_rial and not says_toman:
            if number % 10 == 0:
                readings.append(number // 10)
        else:
            readings.append(number)
            if number % 10 == 0:
                readings.append(number // 10)

    # Preserve order while dropping repeats, so the first match wins.
    seen = set()
    unique = []
    for value in readings:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def looks_like_credit(raw_text: str) -> bool:
    lowered = normalize_digits(raw_text or '').lower()
    if any(word in lowered for word in DEBIT_WORDS):
        return False
    return any(word in lowered for word in CREDIT_WORDS)


def sender_allowed(sender: str) -> bool:
    site = SiteSetting.get_solo()
    allowed = [s.strip() for s in (site.sms_allowed_senders or '').split(',') if s.strip()]
    if not allowed:
        return True
    value = normalize_digits(sender or '').strip()
    # Bank sender ids arrive with assorted prefixes, so compare loosely.
    return any(entry in value or value in entry for entry in allowed)


def find_matching_request(amounts: list[int]) -> CardPaymentRequest | None:
    """Find the live invoice asking for one of these amounts."""
    if not amounts:
        return None
    return (
        CardPaymentRequest.objects.filter(
            status=CardPaymentRequest.Status.PENDING,
            expires_at__gt=timezone.now(),
            amount_toman__in=[Decimal(a) for a in amounts],
        )
        .select_related('user')
        .order_by('created_at')
        .first()
    )
