"""Notice when a subscription has finished.

A subscription ends in one of two ways. The expiry date passing is visible from
the database. The traffic quota running out is only visible in the panel, so
something has to go and look — that is what this does.

One request per panel covers every client on it, which is what makes running
this on a timer affordable. It also un-marks a client that is no longer over
quota, so a renewal or a manual reset in the panel heals the flag by itself.
"""

from __future__ import annotations

from django.utils import timezone

from sales.models import Order, XUIPanel
from sales.services.xui import XUIClient, XUIError


def is_depleted(stats: dict[str, int]) -> bool:
    """Whether a client has used everything it was given.

    A zero total means unlimited in 3x-ui, which is never depleted.
    """
    total = int(stats.get('total') or 0)
    if total <= 0:
        return False
    return int(stats.get('up') or 0) + int(stats.get('down') or 0) >= total


def sweep_panel(panel: XUIPanel) -> dict[str, int]:
    """Update the traffic flag for every order living on one panel."""
    orders = list(
        Order.objects.filter(
            status=Order.Status.PROVISIONED,
            service__panel=panel,
        ).exclude(xui_client_email='')
    )
    if not orders:
        return {'checked': 0, 'ended': 0, 'revived': 0}

    usage = XUIClient(panel).usage_by_email()
    now = timezone.now()
    ended = revived = 0

    for order in orders:
        stats = usage.get((order.xui_client_email or '').strip().lower())
        if stats is None:
            # The client is not on the panel any more. That is the operator's
            # doing, not something to be inferred as "out of traffic".
            continue
        depleted = is_depleted(stats)
        if depleted and order.traffic_ended_at is None:
            Order.objects.filter(pk=order.pk).update(traffic_ended_at=now, updated_at=now)
            ended += 1
        elif not depleted and order.traffic_ended_at is not None:
            Order.objects.filter(pk=order.pk).update(traffic_ended_at=None, updated_at=now)
            revived += 1

    return {'checked': len(orders), 'ended': ended, 'revived': revived}


def sweep_all() -> dict[str, int]:
    """Run the sweep across every active panel, tolerating unreachable ones."""
    totals = {'checked': 0, 'ended': 0, 'revived': 0, 'panels': 0, 'failed': 0}
    for panel in XUIPanel.objects.filter(is_active=True):
        try:
            result = sweep_panel(panel)
        except XUIError:
            # An unreachable panel must not mark its customers as finished.
            totals['failed'] += 1
            continue
        totals['panels'] += 1
        for key in ('checked', 'ended', 'revived'):
            totals[key] += result[key]
    return totals
