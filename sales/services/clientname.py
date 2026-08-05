"""Let the customer name their own config.

3x-ui calls this field `email`, but it is really the client's label: it is what
the operator sees in the panel and what the customer sees in their app. It has
to be unique across the panel, so a name is checked against both this database
and the panel itself before the customer pays for it.
"""

from __future__ import annotations

import random
import re
import string
import unicodedata

from sales.models import LinkedService, Order

MIN_LENGTH = 3
MAX_LENGTH = 32

# Letters, digits, underscore and dash, but never on the ends: the examples the
# customer is shown reject `ali_` and `_mahdi` while accepting `ws1_ksdf`.
NAME_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*[A-Za-z0-9]$')

PROMPT_TEXT = (
    'يک نام کاربري دلخواه ارسال کنيد\n\n'
    '⚠️ نام کاربری باید بدون کاراکترهای اضافه مانند @ ، فاصله ، خط تیره باشد.\n'
    '⚠️ نام کاربری باید انگلیسی باشد.\n\n'
    '✅ نام کاربری های صحیح  : ali12  | mahdi  | ws1_ksdf\n'
    '❌  نام کاربری های نادرست :   ali_ |  tele@  |  _mahdi | محسن'
)


class ClientNameError(ValueError):
    """A name the customer cannot use, with a message meant for them."""


def _ascii_slug(value: str) -> str:
    """Strip a display name down to something a config label can hold.

    Persian names leave nothing behind, which is why the caller always has a
    fallback ready rather than trusting this to return something.
    """
    normalized = unicodedata.normalize('NFKD', value or '')
    ascii_only = normalized.encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^A-Za-z0-9]', '', ascii_only).lower()


def random_suffix(length: int = 4) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(random.choice(alphabet) for _ in range(length))


def suggest(user) -> str:
    """A name built from the customer's Telegram name plus four random characters.

    The random tail is what makes it unique; the name part is only there so the
    customer recognises their own config in a list.
    """
    base = _ascii_slug(user.first_name) or _ascii_slug(user.username) or _ascii_slug(user.last_name)
    if not base:
        base = f'user{abs(int(user.chat_id)) % 100000}'
    base = base[:MAX_LENGTH - 5]
    return f'{base}-{random_suffix()}'


def validate(raw: str) -> str:
    """Check a typed name against the rules the customer was shown."""
    name = (raw or '').strip()
    if not name:
        raise ClientNameError('نام کاربری را بنویسید.')
    if ' ' in name or '\n' in name:
        raise ClientNameError('نام کاربری نباید فاصله داشته باشد.')
    if len(name) < MIN_LENGTH:
        raise ClientNameError(f'نام کاربری باید حداقل {MIN_LENGTH} حرف باشد.')
    if len(name) > MAX_LENGTH:
        raise ClientNameError(f'نام کاربری باید حداکثر {MAX_LENGTH} حرف باشد.')
    if not name.isascii():
        raise ClientNameError('نام کاربری باید انگلیسی باشد. از حروف فارسی استفاده نکنید.')
    if not NAME_PATTERN.match(name):
        raise ClientNameError(
            'نام کاربری فقط می‌تواند شامل حروف انگلیسی و عدد باشد، '
            'و نباید با _ یا - شروع یا تمام شود.'
        )
    return name


def is_taken(name: str, panel=None) -> bool:
    """Whether this name already belongs to somebody.

    Checked here as well as in the panel, because an order that has been paid
    for but whose provisioning failed still owns its name, and the panel does
    not know about it yet.
    """
    lowered = name.lower()
    if Order.objects.filter(xui_client_email__iexact=lowered).exists():
        return True
    if LinkedService.objects.filter(client_email__iexact=lowered).exists():
        return True

    if panel is not None:
        # Imported here so the module stays usable in tests that never touch a panel.
        from sales.services.xui import XUIClient, XUIError

        try:
            result = XUIClient(panel).get_client(name)
        except XUIError:
            # The panel says no such client, or is unreachable. Neither is a
            # reason to refuse the name; a real clash is caught at creation.
            return False
        return bool(result) and result.get('success') is not False
    return False


def resolve(raw: str, panel=None) -> str:
    """Validate a typed name and make sure nobody else has it."""
    name = validate(raw)
    if is_taken(name, panel):
        raise ClientNameError(
            'این نام کاربری قبلاً استفاده شده است. نام دیگری بفرستید '
            'یا از دکمه «انتخاب اسم رندوم» استفاده کنید.'
        )
    return name


def unique_suggestion(user, panel=None, attempts: int = 6) -> str:
    """A suggested name that is free, retrying the random tail if it clashes."""
    for _ in range(attempts):
        candidate = suggest(user)
        if not is_taken(candidate, panel):
            return candidate
    # Six collisions on a four-character tail means something is odd; widen it
    # rather than handing back a name that will fail at creation.
    return f'{suggest(user)}{random_suffix(3)}'
