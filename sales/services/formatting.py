from __future__ import annotations

from decimal import Decimal, InvalidOperation

PERSIAN_DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')
ARABIC_PERSIAN_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


def fa_digits(value) -> str:
    return str(value).translate(PERSIAN_DIGITS)


def toman(value) -> str:
    try:
        number = int(Decimal(value))
    except (InvalidOperation, ValueError):
        number = 0
    return fa_digits(f'{number:,}') + ' تومان'


def usd(value) -> str:
    try:
        number = Decimal(value)
    except InvalidOperation:
        number = Decimal('0')
    return fa_digits(f'{number:,.2f}') + ' دلار'


def parse_toman(text: str) -> int:
    cleaned = ''.join(ch for ch in text.translate(ARABIC_PERSIAN_DIGITS) if ch.isdigit())
    return int(cleaned or '0')


def traffic_text(gb) -> str:
    try:
        val = Decimal(gb)
    except InvalidOperation:
        val = Decimal('0')
    if val == 0:
        return 'نامحدود'
    return fa_digits(f'{val.normalize()} گیگابایت')


def days_text(days: int) -> str:
    return fa_digits(days) + ' روز'
