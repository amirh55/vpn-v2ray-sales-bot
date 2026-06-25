from __future__ import annotations

import re
import uuid
from decimal import Decimal
from django.utils import timezone
from django.db import transaction

from sales.models import Order, Plan, Service, TelegramUser, WalletTransaction
from sales.services.qrcode_util import make_qr_content_file
from sales.services.xui import XUIClient, XUIError


def gb_to_bytes(gb: Decimal) -> int:
    if Decimal(gb) == 0:
        return 0
    return int(Decimal(gb) * Decimal(1024 ** 3))


def render_template(template: str, *, order: Order, client_uuid: str, client_email: str) -> str:
    if not template:
        return ''
    panel = order.service.panel
    context = {
        'uuid': client_uuid,
        'client_id': client_uuid,
        'email': client_email,
        'inbound_id': order.service.inbound_id,
        'panel_base_url': panel.base_url.rstrip('/'),
        'subscription_base_url': (panel.subscription_base_url or panel.base_url).rstrip('/'),
        'service_name': order.service.name,
        'plan_name': order.plan.name,
        'telegram_id': order.user.chat_id,
        'duration_days': order.plan.duration_days,
        'traffic_gb': order.plan.traffic_gb,
        'traffic_bytes': order.traffic_bytes,
    }
    try:
        return template.format(**context)
    except KeyError as exc:
        raise XUIError(f'متغیر قالب لینک اشتباه است: {exc}') from exc


def make_safe_xui_email(order: Order, client_uuid: str) -> str:
    """Build a safe, non-empty 3x-ui client email/remark.

    In 3x-ui this field is called `email`, but it is used as the client
    identifier/remark. Some panel versions are sensitive to special characters
    and may reject values that look empty after validation. Keep it short,
    ASCII-only, and unique.
    """
    raw = f'u{order.user.chat_id}o{order.pk}{client_uuid[:8]}'
    safe = re.sub(r'[^A-Za-z0-9-]', '', raw).lower()
    return safe[:64] or f'u{client_uuid.replace('-', '')[:12]}'


def build_client_payload(order: Order, client_uuid: str, client_email: str, expires_at) -> dict:
    if not client_email or not str(client_email).strip():
        raise XUIError('شناسه client email برای 3x-ui خالی است.')
    expiry_ms = int(expires_at.timestamp() * 1000) if expires_at else 0
    return {
        'id': str(client_uuid),
        'alterId': 0,
        'email': str(client_email).strip(),
        'enable': True,
        'totalGB': int(order.traffic_bytes or 0),
        'expiryTime': int(expiry_ms),
        'limitIp': int(order.user_limit or 0),
        'tgId': str(order.user.chat_id or ''),
        'subId': str(client_email).strip(),
        'flow': '',
        'reset': 0,
        'up': 0,
        'down': 0,
    }


@transaction.atomic
def create_order_from_wallet(user: TelegramUser, plan: Plan) -> Order:
    price = Decimal(plan.price_toman())
    user = TelegramUser.objects.select_for_update().get(pk=user.pk)
    if user.wallet_balance_toman < price:
        raise ValueError('موجودی کیف پول کافی نیست.')
    order = Order.objects.create(
        user=user,
        service=plan.service,
        plan=plan,
        source=Order.Source.WALLET,
        status=Order.Status.PAID,
        amount_usd=plan.price_usd,
        amount_toman=price,
        traffic_bytes=gb_to_bytes(plan.traffic_gb),
        user_limit=plan.user_limit,
    )
    user.wallet_balance_toman -= price
    user.save(update_fields=['wallet_balance_toman', 'updated_at'])
    WalletTransaction.objects.create(
        user=user,
        kind=WalletTransaction.Kind.DEBIT,
        amount_toman=price,
        balance_after_toman=user.wallet_balance_toman,
        order=order,
        description=f'خرید پلن {plan.name}',
    )
    return order


def provision_order(order: Order) -> Order:
    order = Order.objects.select_related('user', 'plan', 'service', 'service__panel').get(pk=order.pk)
    if order.status not in [Order.Status.PAID, Order.Status.PENDING]:
        return order

    client_uuid = order.xui_client_uuid or str(uuid.uuid4())
    client_email = order.xui_client_email or make_safe_xui_email(order, client_uuid)
    expires_at = timezone.now() + timezone.timedelta(days=order.plan.duration_days)
    order.traffic_bytes = gb_to_bytes(order.plan.traffic_gb)
    order.user_limit = order.plan.user_limit

    payload = build_client_payload(order, client_uuid, client_email, expires_at)
    xui = XUIClient(order.service.panel)
    xui_result = xui.add_client(order.service.inbound_id, payload)
    actual_uuid = str(xui_result.get('client_uuid') or '').strip()
    if actual_uuid:
        client_uuid = actual_uuid

    order.xui_client_uuid = client_uuid
    order.xui_client_email = client_email
    order.expires_at = expires_at
    order.config_link = render_template(order.service.config_link_template, order=order, client_uuid=client_uuid, client_email=client_email)
    order.subscription_link = render_template(order.service.subscription_link_template, order=order, client_uuid=client_uuid, client_email=client_email)
    qr_data = order.subscription_link or order.config_link or f'{client_email}'
    order.qr_image.save(f'order_{order.pk}_qr.png', make_qr_content_file(qr_data, f'order_{order.pk}_qr.png'), save=False)
    order.status = Order.Status.PROVISIONED
    order.save()
    return order


def renew_order_from_wallet(order: Order, plan: Plan) -> Order:
    with transaction.atomic():
        order = Order.objects.select_for_update().select_related('user', 'service', 'service__panel').get(pk=order.pk)
        user = TelegramUser.objects.select_for_update().get(pk=order.user.pk)
        price = Decimal(plan.price_toman())
        if user.wallet_balance_toman < price:
            raise ValueError('موجودی کیف پول کافی نیست.')
        user.wallet_balance_toman -= price
        user.save(update_fields=['wallet_balance_toman', 'updated_at'])
        WalletTransaction.objects.create(
            user=user,
            kind=WalletTransaction.Kind.DEBIT,
            amount_toman=price,
            balance_after_toman=user.wallet_balance_toman,
            order=order,
            description=f'تمدید پلن {plan.name}',
        )
        base_expiry = order.expires_at if order.expires_at and order.expires_at > timezone.now() else timezone.now()
        new_expiry = base_expiry + timezone.timedelta(days=plan.duration_days)
        order.expires_at = new_expiry
        order.traffic_bytes = gb_to_bytes(plan.traffic_gb)
        order.user_limit = plan.user_limit
        order.plan = plan
        order.amount_usd = plan.price_usd
        order.amount_toman = price
        order.status = Order.Status.PROVISIONED
        order.save()

    if order.xui_client_email and order.xui_client_uuid:
        payload = build_client_payload(order, order.xui_client_uuid, order.xui_client_email, order.expires_at)
        XUIClient(order.service.panel).update_client(order.xui_client_email, payload)
    return order
