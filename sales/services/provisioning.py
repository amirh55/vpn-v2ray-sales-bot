from __future__ import annotations

import re
import uuid
from decimal import Decimal
from django.utils import timezone
from django.db import transaction

from sales.models import Order, Plan, Service, TelegramUser, WalletTransaction
from sales.services.discounts import redeem
from sales.services.qrcode_util import make_qr_content_file
from sales.services.xui import XUIClient, XUIError


def gb_to_bytes(gb: Decimal) -> int:
    if Decimal(gb) == 0:
        return 0
    return int(Decimal(gb) * Decimal(1024 ** 3))


def render_template(template: str, *, order: Order, client_uuid: str, client_email: str, sub_id: str = '') -> str:
    if not template:
        return ''
    panel = order.service.panel
    context = {
        'uuid': client_uuid,
        'client_id': client_uuid,
        'email': client_email,
        # The subscription URL is keyed on this, not on the client name, even
        # though provisioning asks the panel to make them the same.
        'sub_id': sub_id or client_email,
        'inbound_id': order.service.inbound_id,
        'inbound_ids': ','.join(str(i) for i in order.service.inbound_id_list()),
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
    return safe[:64] or f'u{client_uuid.replace("-", "")[:12]}'


def partner_comment(order: Order) -> str:
    """What to write in the panel's Comment field for this client.

    So that months later, looking at a config in 3x-ui alone, it is clear who
    sold it and to whom. Empty for a direct sale, which leaves the field as it
    was before partners existed.
    """
    parts = []
    if order.customer_label:
        parts.append(f'Customer: {order.customer_label}')
    if order.partner_id:
        parts.append(f'Partner: {order.partner.display_name}')
    return ' | '.join(parts)


def build_client_payload(order: Order, client_uuid: str, client_email: str, expires_at) -> dict:
    if not client_email or not str(client_email).strip():
        raise XUIError('شناسه client email برای 3x-ui خالی است.')
    expiry_ms = int(expires_at.timestamp() * 1000) if expires_at else 0
    return {
        'comment': partner_comment(order),
        'id': str(client_uuid),
        'alterId': 0,
        'email': str(client_email).strip(),
        'enable': True,
        'totalGB': int(order.traffic_bytes or 0),
        'expiryTime': int(expiry_ms),
        'limitIp': int(order.user_limit or 0),
        # 3x-ui v3 Go model requires tgId as int64, not string.
        'tgId': int(order.user.chat_id or 0),
        'subId': str(client_email).strip(),
        'flow': '',
        'reset': 0,
        'up': 0,
        'down': 0,
    }


def build_links(
    order: Order, xui: 'XUIClient', *, client_uuid: str, client_email: str, sub_id: str = ''
) -> tuple[str, str]:
    """The config and subscription links to hand the customer.

    The panel is asked first. It is the only party that knows the inbound's
    protocol, TLS and Reality settings, so its answer is right by construction —
    a hand-written template has to reproduce all of that and silently produces a
    broken link the moment an inbound is reconfigured.

    A template set on the service still wins, so an operator who needs a
    specific link shape keeps control. Templates are otherwise optional now.
    """
    config_link = render_template(
        order.service.config_link_template,
        order=order, client_uuid=client_uuid, client_email=client_email, sub_id=sub_id,
    )
    subscription_link = render_template(
        order.service.subscription_link_template,
        order=order, client_uuid=client_uuid, client_email=client_email, sub_id=sub_id,
    )

    if not config_link:
        try:
            links = xui.get_client_links(client_email)
        except XUIError:
            links = []
        # One client can sit on several inbounds; the customer gets all of them
        # so their app can fall back when one route is blocked.
        config_link = '\n'.join(links)

    if not subscription_link:
        subscription_link = order.service.panel.subscription_url(sub_id or client_email)

    return config_link, subscription_link


@transaction.atomic
def create_order_from_wallet(
    user: TelegramUser,
    plan: Plan,
    *,
    discount=None,
    client_name: str = '',
) -> Order:
    """Charge the wallet and open the order.

    `discount` is a DiscountQuote. Passing one charges its discounted price and
    records the redemption in the same transaction as the wallet debit, so a
    code can never be counted as used without the customer being charged the
    lower price.
    """
    price = Decimal(discount.price_toman) if discount else Decimal(plan.price_toman)
    price_usd = discount.price_usd if discount else plan.price_usd
    user = TelegramUser.objects.select_for_update().get(pk=user.pk)
    if user.wallet_balance_toman < price:
        raise ValueError('موجودی کیف پول کافی نیست.')
    order = Order.objects.create(
        user=user,
        service=plan.service,
        plan=plan,
        source=Order.Source.WALLET,
        status=Order.Status.PAID,
        amount_usd=price_usd,
        amount_toman=price,
        traffic_bytes=gb_to_bytes(plan.traffic_gb),
        user_limit=plan.user_limit,
        discount_code=discount.code if discount else None,
        discount_toman=Decimal(discount.off_toman) if discount else Decimal('0'),
        # The name the customer picked before paying. Empty means they were not
        # asked, and provisioning falls back to a generated one.
        xui_client_email=(client_name or '').strip(),
    )
    user.wallet_balance_toman -= price
    user.save(update_fields=['wallet_balance_toman', 'updated_at'])
    description = f'خرید پلن {plan.name}'
    if discount:
        description += f' با کد {discount.code.code}'
    WalletTransaction.objects.create(
        user=user,
        kind=WalletTransaction.Kind.DEBIT,
        amount_toman=price,
        balance_after_toman=user.wallet_balance_toman,
        order=order,
        description=description,
    )
    if discount:
        redeem(
            discount.code,
            user,
            order=order,
            off_toman=discount.off_toman,
            off_usd=discount.off_usd,
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
    xui_result = xui.add_client(order.service.inbound_id_list(), payload)
    actual_uuid = str(xui_result.get('client_uuid') or '').strip()
    if actual_uuid:
        client_uuid = actual_uuid

    # Ask the panel what it actually keyed the subscription on. It normally
    # honours the subId we sent, but a build that mints its own would otherwise
    # leave the customer with a subscription link pointing at nothing.
    sub_id = xui.get_client_sub_id(client_email) or client_email

    order.xui_client_uuid = client_uuid
    order.xui_client_email = client_email
    order.xui_sub_id = sub_id
    order.expires_at = expires_at
    order.config_link, order.subscription_link = build_links(
        order, xui, client_uuid=client_uuid, client_email=client_email, sub_id=sub_id
    )
    qr_data = order.subscription_link or order.config_link or f'{client_email}'
    order.qr_image.save(f'order_{order.pk}_qr.png', make_qr_content_file(qr_data, f'order_{order.pk}_qr.png'), save=False)
    order.status = Order.Status.PROVISIONED
    order.save()
    return order


def extend_order(order: Order, plan: Plan, price_toman: Decimal, price_usd: Decimal) -> Order:
    """Move a subscription onto a new period. The caller handles the money.

    Shared by the wallet renewal and the partner renewal so the two can never
    drift apart on what "renewed" means to the dates and the quota.
    """
    # Renewing a subscription that still has time left adds to it; renewing one
    # that already ran out starts the new period from now, so the customer does
    # not pay for days that already passed.
    base_expiry = order.expires_at if order.expires_at and order.expires_at > timezone.now() else timezone.now()
    order.expires_at = base_expiry + timezone.timedelta(days=plan.duration_days)
    order.traffic_bytes = gb_to_bytes(plan.traffic_gb)
    # The new period comes with its own quota, so whatever ended the old one no
    # longer applies.
    order.traffic_ended_at = None
    order.user_limit = plan.user_limit
    order.plan = plan
    order.amount_usd = price_usd
    order.amount_toman = price_toman
    order.status = Order.Status.PROVISIONED
    order.save()
    return order


def push_renewal_to_panel(order: Order) -> None:
    """Tell the panel about the new period, and clear the old usage."""
    if not (order.xui_client_email and order.xui_client_uuid):
        return
    client = XUIClient(order.service.panel)
    payload = build_client_payload(order, order.xui_client_uuid, order.xui_client_email, order.expires_at)
    client.update_client(order.xui_client_email, payload)
    # Without this the panel keeps the old usage against the new quota, and a
    # customer who just renewed a used-up plan stays disconnected.
    client.reset_client_traffic(order.xui_client_email)


def renew_order_from_wallet(order: Order, plan: Plan) -> Order:
    with transaction.atomic():
        order = Order.objects.select_for_update().select_related('user', 'service', 'service__panel').get(pk=order.pk)
        user = TelegramUser.objects.select_for_update().get(pk=order.user.pk)
        price = Decimal(plan.price_toman)
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
        extend_order(order, plan, price, Decimal(plan.price_usd))

    push_renewal_to_panel(order)
    return order


# ─────────────────────────────────────────────────────────────────────────────
# سفارش همکار
# ─────────────────────────────────────────────────────────────────────────────


def create_partner_order(
    partner,
    plan: Plan,
    *,
    quote,
    customer_label: str = '',
    client_name: str = '',
    on_credit: bool,
) -> Order:
    """Open an order a partner is selling on.

    An ordinary Order in every way that matters — the same provisioner, the same
    renewal, the same sweep — carrying who sold it and at what price. A credit
    order is left unstamped for revenue: it is provisioned before any money has
    arrived, and the money arrives on an invoice later.
    """
    return Order.objects.create(
        user=partner.user,
        service=plan.service,
        plan=plan,
        partner=partner,
        customer_label=(customer_label or '').strip(),
        source=Order.Source.PARTNER_CREDIT if on_credit else Order.Source.WALLET,
        # PAID rather than PENDING so the provisioner will take it. For a credit
        # order that means "the shop has agreed to supply it", not "paid".
        status=Order.Status.PAID,
        amount_usd=quote.final_usd,
        amount_toman=quote.final_toman,
        partner_base_toman=quote.list_toman,
        traffic_bytes=gb_to_bytes(plan.traffic_gb),
        user_limit=plan.user_limit,
        xui_client_email=(client_name or '').strip(),
    )


def set_order_client_enabled(order: Order, enabled: bool) -> None:
    """Switch a delivered config on or off in the panel.

    The payload is rebuilt from the order rather than read back from the panel,
    so re-enabling restores what was actually sold even if somebody edited the
    client by hand in the meantime.
    """
    if not (order.xui_client_email and order.xui_client_uuid):
        return
    payload = build_client_payload(order, order.xui_client_uuid, order.xui_client_email, order.expires_at)
    XUIClient(order.service.panel).set_client_enabled(order.xui_client_email, payload, enabled)


def delete_order_client(order: Order) -> bool:
    """Remove the config from the panel. The order row stays as the record."""
    if not order.xui_client_email:
        return True
    return XUIClient(order.service.panel).delete_client(order.xui_client_email, order.xui_client_uuid)
