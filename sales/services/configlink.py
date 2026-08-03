"""Parse a user-supplied config link down to the identifier 3x-ui stores.

In 3x-ui every client row carries an identifier that also appears in the share
link the user holds:

    vless / vmess  -> client "id" (a UUID)
    trojan         -> client "password"
    shadowsocks    -> client "password"

Matching on that identifier is the only reliable way to recognise a config the
user bought elsewhere, because remarks and hostnames are freely editable.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from urllib.parse import unquote

UUID_RE = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)

SUPPORTED_SCHEMES = ('vless://', 'vmess://', 'trojan://', 'ss://')


class ConfigLinkError(ValueError):
    """The text the user sent is not a config link we can read."""


def _b64decode_loose(value: str) -> bytes:
    """Decode base64 that may be URL-safe and is usually missing its padding."""
    cleaned = value.strip().replace('-', '+').replace('_', '/')
    cleaned += '=' * (-len(cleaned) % 4)
    return base64.b64decode(cleaned)


def _parse_vmess(link: str) -> str:
    payload = link[len('vmess://'):].strip()
    try:
        data = json.loads(_b64decode_loose(payload).decode('utf-8', 'replace'))
    except (binascii.Error, ValueError):
        # A few clients emit vmess links that are not base64-wrapped JSON.
        match = UUID_RE.search(payload)
        if match:
            return match.group(0)
        raise ConfigLinkError('لینک vmess قابل خواندن نیست.') from None
    if isinstance(data, dict):
        for key in ('id', 'uuid', 'password'):
            value = str(data.get(key) or '').strip()
            if value:
                return value
    raise ConfigLinkError('شناسه کلاینت در لینک vmess پیدا نشد.')


def _shadowsocks_password(userinfo: str) -> str:
    """ss:// carries "method:password", usually base64-wrapped."""
    if ':' in userinfo:
        return userinfo.split(':', 1)[1]
    try:
        decoded = _b64decode_loose(userinfo).decode('utf-8', 'replace')
    except (binascii.Error, ValueError):
        return userinfo
    if ':' in decoded:
        return decoded.split(':', 1)[1]
    return decoded or userinfo


def _parse_userinfo(link: str) -> str:
    """Pull the credential before '@' from vless/trojan/ss style links.

    urlsplit is deliberately not used for the credential: it splits userinfo at
    the first ':', which corrupts shadowsocks "method:password" and any trojan
    password containing a colon.
    """
    body = link.split('://', 1)[1].split('#', 1)[0].split('?', 1)[0]
    if '@' not in body:
        raise ConfigLinkError('شناسه کلاینت در لینک پیدا نشد.')
    userinfo = unquote(body.rsplit('@', 1)[0]).strip()
    if not userinfo:
        raise ConfigLinkError('شناسه کلاینت در لینک پیدا نشد.')

    # Only shadowsocks packs "method:password" here. Decoding unconditionally
    # would mangle vless UUIDs and trojan passwords.
    if link.lower().startswith('ss://'):
        return _shadowsocks_password(userinfo)
    return userinfo


def extract_client_id(text: str) -> str:
    """Return the client id/password encoded in a config link.

    Accepts a full share link or a bare UUID pasted on its own.
    """
    if not text:
        raise ConfigLinkError('چیزی ارسال نشده است.')

    value = text.strip()
    # Users often paste a link wrapped in whitespace or with the remark on a
    # second line; keep only the first token that looks like a link.
    for token in value.split():
        if token.lower().startswith(SUPPORTED_SCHEMES):
            value = token
            break

    lowered = value.lower()
    if lowered.startswith('vmess://'):
        return _parse_vmess(value)
    if lowered.startswith(SUPPORTED_SCHEMES):
        return _parse_userinfo(value)

    match = UUID_RE.search(value)
    if match:
        return match.group(0)

    raise ConfigLinkError(
        'لینک کانفیگ معتبر نیست. لینکی که با vless:// یا vmess:// یا trojan:// شروع می‌شود ارسال کنید.'
    )


def extract_label(text: str) -> str:
    """Return the remark after '#', which users recognise as the config name."""
    if '#' not in text:
        return ''
    label = unquote(text.strip().split('#', 1)[1]).strip()
    return label[:150]
