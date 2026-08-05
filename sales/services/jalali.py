"""Jalali (Persian) calendar dates.

Written out here rather than pulled from a package: the conversion is a few
lines of integer arithmetic, and the project already pins two different Django
stacks for two Python ranges, so every extra dependency is another pin that can
break an install on somebody's server.

The customer and the shop owner both think in Shamsi dates, so everything they
read — the bot, the report page, the CSV, the Telegram summary — goes through
here. The database keeps storing real timezone-aware datetimes; this is only a
display layer, plus the month boundaries the reports count by.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from django.utils import timezone

MONTH_NAMES = (
    'فروردین', 'اردیبهشت', 'خرداد', 'تیر', 'مرداد', 'شهریور',
    'مهر', 'آبان', 'آذر', 'دی', 'بهمن', 'اسفند',
)

WEEKDAY_NAMES = ('دوشنبه', 'سه‌شنبه', 'چهارشنبه', 'پنجشنبه', 'جمعه', 'شنبه', 'یکشنبه')

_GREGORIAN_MONTH_DAYS = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)

PERSIAN_DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')


def to_jalali(value: date | datetime) -> tuple[int, int, int]:
    """Gregorian date to (year, month, day) in the Jalali calendar."""
    if isinstance(value, datetime):
        value = value.date()
    gy, gm, gd = value.year, value.month, value.day

    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621

    gy2 = gy + 1 if gm > 2 else gy
    days = (
        365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        - 80
        + gd
        + _GREGORIAN_MONTH_DAYS[gm - 1]
    )

    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365

    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def from_jalali(jy: int, jm: int, jd: int) -> date:
    """(year, month, day) in the Jalali calendar back to a Gregorian date."""
    if jy > 979:
        gy = 1600
        jy -= 979
    else:
        gy = 621

    days = (
        365 * jy
        + (jy // 33) * 8
        + (jy % 33 + 3) // 4
        + 78
        + jd
        + ((jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186)
    )

    gy += 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365

    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    month_lengths = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    for length in month_lengths:
        if gd <= length:
            break
        gd -= length
        gm += 1
    return date(gy, gm + 1, gd)


def month_length(jy: int, jm: int) -> int:
    """Days in one Jalali month, esfand included."""
    if jm <= 6:
        return 31
    if jm <= 11:
        return 30
    # Rather than encode a leap rule, ask the calendar: if 30 esfand converts
    # back to itself, the year is long.
    return 30 if to_jalali(from_jalali(jy, 12, 30)) == (jy, 12, 30) else 29


def fa_digits(text) -> str:
    return str(text).translate(PERSIAN_DIGITS)


def format_date(value: date | datetime | None, *, latin_digits: bool = False) -> str:
    """A Jalali date as ۱۴۰۵/۰۵/۱۴."""
    if value is None:
        return ''
    jy, jm, jd = to_jalali(value)
    text = f'{jy:04d}/{jm:02d}/{jd:02d}'
    return text if latin_digits else fa_digits(text)


def format_datetime(value: datetime | None, *, latin_digits: bool = False) -> str:
    """A Jalali date and a 24-hour clock, in the project's timezone."""
    if value is None:
        return ''
    local = timezone.localtime(value) if timezone.is_aware(value) else value
    jy, jm, jd = to_jalali(local)
    text = f'{jy:04d}/{jm:02d}/{jd:02d} {local.hour:02d}:{local.minute:02d}'
    return text if latin_digits else fa_digits(text)


def format_long(value: date | datetime | None) -> str:
    """A Jalali date spelled out, as ۱۴ مرداد ۱۴۰۵."""
    if value is None:
        return ''
    jy, jm, jd = to_jalali(value)
    return fa_digits(f'{jd} {MONTH_NAMES[jm - 1]} {jy}')


def month_title(value: date | datetime) -> str:
    """The month a date falls in, as مرداد ۱۴۰۵."""
    jy, jm, _ = to_jalali(value)
    return fa_digits(f'{MONTH_NAMES[jm - 1]} {jy}')


def month_first_day(value: date | datetime) -> date:
    """The Gregorian date on which this date's Jalali month starts."""
    jy, jm, _ = to_jalali(value)
    return from_jalali(jy, jm, 1)


def next_month_first_day(value: date | datetime) -> date:
    jy, jm, _ = to_jalali(value)
    return from_jalali(jy + 1, 1, 1) if jm == 12 else from_jalali(jy, jm + 1, 1)


def previous_month_day(value: date | datetime) -> date:
    """Any day inside the Jalali month before this one."""
    return month_first_day(value) - timedelta(days=1)
