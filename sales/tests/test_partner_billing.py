"""The billing cycle, and everything that hangs off missing a deadline.

The panel is stubbed out throughout: what is under test is which orders get
switched off, which come back, and what happens to the money — not whether an
HTTP call was formatted right.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from sales.models import (
    Order,
    Partner,
    PartnerInvoice,
    PartnerInvoiceItem,
    SiteSetting,
    TelegramUser,
)
from sales.services import partner_billing
from sales.services.partner_billing import PartnerBillingError
from sales.tests.test_partner import make_partner, make_plan

PANEL = 'sales.services.partner_billing.set_order_client_enabled'


class BillingTestCase(TestCase):
    """Shared fixtures: one credit partner, one plan, and a panel that no-ops."""

    def setUp(self):
        self.plan = make_plan()
        self.partner = make_partner(
            billing_mode=Partner.BillingMode.CREDIT,
            billing_cycle_days=7,
            credit_limit_toman=Decimal('0'),
        )
        site = SiteSetting.get_solo()
        site.is_shop_active = True
        site.partner_delete_refund_hours = 24
        site.partner_invoice_warn_hours = 24
        site.save()

    def make_order(self, **kwargs):
        defaults = {
            'user': self.partner.user,
            'service': self.plan.service,
            'plan': self.plan,
            'partner': self.partner,
            'source': Order.Source.PARTNER_CREDIT,
            'status': Order.Status.PROVISIONED,
            'amount_usd': Decimal('10'),
            'amount_toman': Decimal('350000'),
            'xui_client_uuid': 'uuid-1',
            'xui_client_email': 'cfg-1',
            'expires_at': timezone.now() + timezone.timedelta(days=30),
        }
        defaults.update(kwargs)
        return Order.objects.create(**defaults)

    def charge(self, amount='350000', order=None):
        return partner_billing.charge_to_invoice(
            self.partner,
            kind=PartnerInvoiceItem.Kind.NEW,
            title='x',
            amount_toman=Decimal(amount),
            order=order,
        )


class CycleTests(BillingTestCase):
    def test_the_first_charge_opens_a_cycle_dated_from_now(self):
        before = timezone.now()
        item = self.charge()
        invoice = item.invoice
        self.assertEqual(invoice.status, PartnerInvoice.Status.OPEN)
        self.assertGreaterEqual(invoice.opened_at, before)
        self.assertEqual((invoice.due_at - invoice.opened_at).days, 7)

    def test_three_charges_in_one_cycle_make_one_invoice(self):
        """The brief's worked example: orders on the 10th, 12th and 15th."""
        first = self.charge('100000')
        second = self.charge('200000')
        third = self.charge('300000')
        self.assertEqual(PartnerInvoice.objects.count(), 1)
        self.assertEqual({first.invoice_id, second.invoice_id, third.invoice_id}, {first.invoice_id})
        first.invoice.refresh_from_db()
        self.assertEqual(first.invoice.total_toman, Decimal('600000'))

    def test_no_invoice_exists_before_the_first_order(self):
        self.assertEqual(PartnerInvoice.objects.count(), 0)
        self.assertIsNone(self.partner.open_invoice())

    def test_paying_does_not_open_the_next_cycle(self):
        """A partner who stops selling must not receive an invoice for nothing."""
        item = self.charge()
        partner_billing.settle_invoice(item.invoice, via=PartnerInvoice.SettledBy.ADMIN)
        self.assertIsNone(self.partner.open_invoice())
        self.assertEqual(PartnerInvoice.objects.count(), 1)

    def test_the_next_order_after_payment_opens_a_fresh_cycle(self):
        first = self.charge()
        partner_billing.settle_invoice(first.invoice, via=PartnerInvoice.SettledBy.ADMIN)
        second = self.charge()
        self.assertNotEqual(first.invoice_id, second.invoice_id)
        self.assertEqual(PartnerInvoice.objects.count(), 2)


class OrderGuardTests(BillingTestCase):
    def test_an_open_invoice_does_not_stop_ordering(self):
        self.charge()
        partner_billing.check_can_order(self.partner, Decimal('100000'))

    def test_an_overdue_invoice_stops_ordering(self):
        item = self.charge()
        PartnerInvoice.objects.filter(pk=item.invoice_id).update(status=PartnerInvoice.Status.OVERDUE)
        with self.assertRaises(PartnerBillingError) as caught:
            partner_billing.check_can_order(self.partner, Decimal('1'))
        self.assertIn('سررسید', str(caught.exception))

    def test_an_inactive_partner_cannot_order(self):
        self.partner.is_active = False
        with self.assertRaises(PartnerBillingError):
            partner_billing.check_can_order(self.partner, Decimal('1'))

    def test_a_closed_shop_stops_ordering(self):
        site = SiteSetting.get_solo()
        site.is_shop_active = False
        site.save()
        with self.assertRaises(PartnerBillingError):
            partner_billing.check_can_order(self.partner, Decimal('1'))

    def test_a_prepaid_partner_is_never_blocked_by_debt(self):
        """Prepaid partners have no invoices, so none of this applies to them."""
        self.partner.billing_mode = Partner.BillingMode.PREPAID
        self.partner.save()
        item = self.charge()
        PartnerInvoice.objects.filter(pk=item.invoice_id).update(status=PartnerInvoice.Status.OVERDUE)
        partner_billing.check_can_order(self.partner, Decimal('1'))

    def test_a_charge_over_the_credit_limit_is_refused(self):
        self.partner.credit_limit_toman = Decimal('500000')
        self.partner.save()
        self.charge('400000')
        with self.assertRaises(PartnerBillingError) as caught:
            self.charge('200000')
        self.assertIn('سقف اعتبار', str(caught.exception))

    def test_a_refused_charge_does_not_land_on_the_invoice(self):
        self.partner.credit_limit_toman = Decimal('500000')
        self.partner.save()
        item = self.charge('400000')
        with self.assertRaises(PartnerBillingError):
            self.charge('200000')
        item.invoice.refresh_from_db()
        self.assertEqual(item.invoice.total_toman, Decimal('400000'))


class SuspensionTests(BillingTestCase):
    def test_overdue_switches_configs_off_without_deleting_them(self):
        order = self.make_order()
        item = self.charge(order=order)
        invoice = item.invoice
        with mock.patch(PANEL) as panel:
            count = partner_billing.suspend_invoice_orders(invoice)
        self.assertEqual(count, 1)
        panel.assert_called_once()
        self.assertFalse(panel.call_args[0][1])

        order.refresh_from_db()
        self.assertIsNotNone(order.suspended_at)
        self.assertEqual(order.suspended_by_invoice_id, invoice.pk)
        # Point 9: the config's identity and dates survive untouched.
        self.assertEqual(order.status, Order.Status.PROVISIONED)
        self.assertEqual(order.xui_client_uuid, 'uuid-1')
        self.assertIsNotNone(order.expires_at)

    def test_a_cancelled_line_does_not_get_its_config_suspended(self):
        order = self.make_order()
        item = self.charge(order=order)
        partner_billing.cancel_item(item, 'حذف شد')
        with mock.patch(PANEL):
            count = partner_billing.suspend_invoice_orders(item.invoice)
        self.assertEqual(count, 0)

    def test_suspension_is_recorded_even_when_the_panel_is_unreachable(self):
        """The flag is what re-enabling reads, so it must not depend on the panel."""
        order = self.make_order()
        item = self.charge(order=order)
        with mock.patch(PANEL, side_effect=RuntimeError('panel down')):
            partner_billing.suspend_invoice_orders(item.invoice)
        order.refresh_from_db()
        self.assertIsNotNone(order.suspended_at)

    def test_suspending_twice_does_not_double_count(self):
        order = self.make_order()
        item = self.charge(order=order)
        with mock.patch(PANEL):
            partner_billing.suspend_invoice_orders(item.invoice)
            again = partner_billing.suspend_invoice_orders(item.invoice)
        self.assertEqual(again, 0)


class ReactivationTests(BillingTestCase):
    def suspend(self, order):
        item = self.charge(order=order)
        with mock.patch(PANEL):
            partner_billing.suspend_invoice_orders(item.invoice)
        return item.invoice

    def test_paying_switches_the_configs_back_on(self):
        order = self.make_order()
        invoice = self.suspend(order)
        with mock.patch(PANEL) as panel:
            partner_billing.settle_invoice(invoice, via=PartnerInvoice.SettledBy.WALLET)
        panel.assert_called_once()
        self.assertTrue(panel.call_args[0][1])
        order.refresh_from_db()
        self.assertIsNone(order.suspended_at)
        self.assertIsNone(order.suspended_by_invoice)

    def test_a_config_disabled_by_hand_is_not_revived(self):
        """It never carried this invoice's claim, so paying must not touch it."""
        order = self.make_order()
        invoice = self.suspend(order)
        other = self.make_order(xui_client_email='cfg-2', suspended_at=timezone.now())
        with mock.patch(PANEL) as panel:
            partner_billing.settle_invoice(invoice, via=PartnerInvoice.SettledBy.ADMIN)
        self.assertEqual(panel.call_count, 1)
        other.refresh_from_db()
        self.assertIsNotNone(other.suspended_at)

    def test_an_expired_config_is_not_revived(self):
        order = self.make_order()
        invoice = self.suspend(order)
        Order.objects.filter(pk=order.pk).update(expires_at=timezone.now() - timezone.timedelta(days=1))
        with mock.patch(PANEL) as panel:
            partner_billing.settle_invoice(invoice, via=PartnerInvoice.SettledBy.ADMIN)
        panel.assert_not_called()
        order.refresh_from_db()
        # The claim is released even though the config stays off: the invoice is
        # settled and has no further hold on it.
        self.assertIsNone(order.suspended_at)

    def test_a_config_out_of_traffic_is_not_revived(self):
        order = self.make_order()
        invoice = self.suspend(order)
        Order.objects.filter(pk=order.pk).update(traffic_ended_at=timezone.now())
        with mock.patch(PANEL) as panel:
            partner_billing.settle_invoice(invoice, via=PartnerInvoice.SettledBy.ADMIN)
        panel.assert_not_called()

    def test_a_deleted_config_is_not_revived(self):
        order = self.make_order()
        invoice = self.suspend(order)
        Order.objects.filter(pk=order.pk).update(status=Order.Status.CANCELLED)
        with mock.patch(PANEL) as panel:
            partner_billing.settle_invoice(invoice, via=PartnerInvoice.SettledBy.ADMIN)
        panel.assert_not_called()

    def test_settling_twice_is_refused(self):
        item = self.charge()
        self.assertTrue(partner_billing.settle_invoice(item.invoice, via=PartnerInvoice.SettledBy.ADMIN))
        self.assertFalse(partner_billing.settle_invoice(item.invoice, via=PartnerInvoice.SettledBy.ADMIN))


class WalletSettlementTests(BillingTestCase):
    def test_paying_from_the_wallet_debits_and_settles(self):
        item = self.charge('350000')
        user = TelegramUser.objects.get(pk=self.partner.user_id)
        user.wallet_balance_toman = Decimal('400000')
        user.save()
        with mock.patch(PANEL):
            partner_billing.pay_invoice_from_wallet(item.invoice)
        user.refresh_from_db()
        item.invoice.refresh_from_db()
        self.assertEqual(user.wallet_balance_toman, Decimal('50000'))
        self.assertEqual(item.invoice.status, PartnerInvoice.Status.PAID)

    def test_an_underfunded_wallet_pays_nothing(self):
        item = self.charge('350000')
        user = TelegramUser.objects.get(pk=self.partner.user_id)
        user.wallet_balance_toman = Decimal('100000')
        user.save()
        with self.assertRaises(PartnerBillingError):
            partner_billing.pay_invoice_from_wallet(item.invoice)
        user.refresh_from_db()
        item.invoice.refresh_from_db()
        self.assertEqual(user.wallet_balance_toman, Decimal('100000'))
        self.assertEqual(item.invoice.status, PartnerInvoice.Status.OPEN)


class RefundWindowTests(BillingTestCase):
    def test_a_fresh_charge_on_an_open_invoice_is_refundable(self):
        item = self.charge()
        self.assertTrue(partner_billing.refund_window_open(item))

    def test_an_old_charge_is_not_refundable(self):
        item = self.charge()
        PartnerInvoiceItem.objects.filter(pk=item.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=25)
        )
        item.refresh_from_db()
        self.assertFalse(partner_billing.refund_window_open(item))

    def test_a_settled_invoice_is_never_refundable(self):
        item = self.charge()
        partner_billing.settle_invoice(item.invoice, via=PartnerInvoice.SettledBy.ADMIN)
        item.refresh_from_db()
        self.assertFalse(partner_billing.refund_window_open(item))

    def test_a_zero_hour_window_disables_refunds(self):
        site = SiteSetting.get_solo()
        site.partner_delete_refund_hours = 0
        site.save()
        item = self.charge()
        self.assertFalse(partner_billing.refund_window_open(item))

    def test_cancelling_a_line_takes_it_off_the_total(self):
        first = self.charge('100000')
        self.charge('250000')
        partner_billing.cancel_item(first, 'حذف کانفیگ')
        first.invoice.refresh_from_db()
        self.assertEqual(first.invoice.total_toman, Decimal('250000'))

    def test_cancelling_the_last_line_closes_the_cycle(self):
        """Otherwise the partner is stuck on an open, unpayable, zero invoice."""
        item = self.charge('100000')
        partner_billing.cancel_item(item, 'حذف کانفیگ')
        item.invoice.refresh_from_db()
        self.assertEqual(item.invoice.status, PartnerInvoice.Status.CANCELLED)
        self.assertIsNone(self.partner.open_invoice())


class BillingPassTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.sent = []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    def test_a_reminder_goes_out_once_inside_the_window(self):
        item = self.charge()
        PartnerInvoice.objects.filter(pk=item.invoice_id).update(
            due_at=timezone.now() + timezone.timedelta(hours=12)
        )
        first = partner_billing.run_billing_pass(self.send)
        second = partner_billing.run_billing_pass(self.send)
        self.assertEqual(first['warned'], 1)
        self.assertEqual(second['warned'], 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('سررسید', self.sent[0][1])

    def test_no_reminder_before_the_window_opens(self):
        item = self.charge()
        PartnerInvoice.objects.filter(pk=item.invoice_id).update(
            due_at=timezone.now() + timezone.timedelta(days=5)
        )
        self.assertEqual(partner_billing.run_billing_pass(self.send)['warned'], 0)

    def test_the_deadline_marks_overdue_and_suspends(self):
        order = self.make_order()
        item = self.charge(order=order)
        PartnerInvoice.objects.filter(pk=item.invoice_id).update(
            due_at=timezone.now() - timezone.timedelta(minutes=1)
        )
        with mock.patch(PANEL):
            result = partner_billing.run_billing_pass(self.send)
        self.assertEqual(result['overdue'], 1)
        self.assertEqual(result['suspended'], 1)
        item.invoice.refresh_from_db()
        self.assertEqual(item.invoice.status, PartnerInvoice.Status.OVERDUE)

    def test_the_deadline_only_fires_once(self):
        item = self.charge(order=self.make_order())
        PartnerInvoice.objects.filter(pk=item.invoice_id).update(
            due_at=timezone.now() - timezone.timedelta(minutes=1)
        )
        with mock.patch(PANEL):
            partner_billing.run_billing_pass(self.send)
            second = partner_billing.run_billing_pass(self.send)
        self.assertEqual(second['overdue'], 0)

    def test_a_paid_invoice_is_never_marked_overdue(self):
        item = self.charge()
        partner_billing.settle_invoice(item.invoice, via=PartnerInvoice.SettledBy.ADMIN)
        PartnerInvoice.objects.filter(pk=item.invoice_id).update(
            due_at=timezone.now() - timezone.timedelta(days=1)
        )
        self.assertEqual(partner_billing.run_billing_pass(self.send)['overdue'], 0)
