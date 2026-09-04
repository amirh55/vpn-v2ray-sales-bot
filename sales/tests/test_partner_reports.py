"""Revenue must not count money that has not arrived.

The one thing a partner program can quietly break is the shop's own books: a
credit order is provisioned before payment, and if the reports keep counting a
provisioned order as income the operator is told they earned money they are
still owed.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from sales.models import Order, Partner, PartnerInvoice, PartnerInvoiceItem, TelegramUser
from sales.services import partner_billing, reports
from sales.services.provisioning import create_partner_order, partner_comment
from sales.services.partner_pricing import quote as price_quote
from sales.tests.test_partner import make_partner, make_plan

PANEL = 'sales.services.partner_billing.set_order_client_enabled'


def whole_period():
    now = timezone.now()
    return now - timezone.timedelta(days=1), now + timezone.timedelta(days=1)


class RevenueSeparationTests(TestCase):
    def setUp(self):
        self.plan = make_plan(partner_price_toman=Decimal('350000'))
        self.partner = make_partner(billing_mode=Partner.BillingMode.CREDIT)
        self.quote = price_quote(self.partner, self.plan)

    def credit_order(self):
        order = create_partner_order(
            self.partner, self.plan, quote=self.quote,
            customer_label='Reza', client_name='reza-1', on_credit=True,
        )
        order.status = Order.Status.PROVISIONED
        order.save()
        return order

    def test_a_credit_order_carries_no_revenue_stamp(self):
        order = self.credit_order()
        self.assertIsNone(order.revenue_at)

    def test_an_unpaid_credit_order_is_not_revenue(self):
        order = self.credit_order()
        partner_billing.charge_to_invoice(
            self.partner, kind=PartnerInvoiceItem.Kind.NEW, title='x',
            amount_toman=self.quote.final_toman, order=order,
        )
        start, end = whole_period()
        report = reports.build(start, end, 'test')
        self.assertEqual(report.revenue_toman, 0)
        self.assertEqual(report.partner_owed_toman, 350000)

    def test_settling_the_invoice_turns_it_into_revenue(self):
        order = self.credit_order()
        item = partner_billing.charge_to_invoice(
            self.partner, kind=PartnerInvoiceItem.Kind.NEW, title='x',
            amount_toman=self.quote.final_toman, order=order,
        )
        with mock.patch(PANEL):
            partner_billing.settle_invoice(item.invoice, via=PartnerInvoice.SettledBy.ADMIN)
        start, end = whole_period()
        report = reports.build(start, end, 'test')
        self.assertEqual(report.partner_settled_toman, 350000)
        self.assertEqual(report.partner_owed_toman, 0)

    def test_an_overdue_invoice_shows_separately_from_merely_open(self):
        order = self.credit_order()
        item = partner_billing.charge_to_invoice(
            self.partner, kind=PartnerInvoiceItem.Kind.NEW, title='x',
            amount_toman=self.quote.final_toman, order=order,
        )
        PartnerInvoice.objects.filter(pk=item.invoice_id).update(status=PartnerInvoice.Status.OVERDUE)
        report = reports.build(*whole_period(), 'test')
        self.assertEqual(report.partner_owed_toman, 350000)
        self.assertEqual(report.partner_overdue_toman, 350000)

    def test_a_renewal_is_counted_as_its_own_charge(self):
        """Counting off the order would merge the two, and the shop would eat one."""
        order = self.credit_order()
        for _ in range(2):
            partner_billing.charge_to_invoice(
                self.partner, kind=PartnerInvoiceItem.Kind.RENEW, title='renew',
                amount_toman=Decimal('350000'), order=order,
            )
        invoice = self.partner.open_invoice()
        self.assertEqual(invoice.total_toman, Decimal('700000'))
        with mock.patch(PANEL):
            partner_billing.settle_invoice(invoice, via=PartnerInvoice.SettledBy.ADMIN)
        report = reports.build(*whole_period(), 'test')
        self.assertEqual(report.partner_settled_toman, 700000)

    def test_a_cancelled_line_is_not_owed(self):
        item = partner_billing.charge_to_invoice(
            self.partner, kind=PartnerInvoiceItem.Kind.NEW, title='x',
            amount_toman=Decimal('350000'),
        )
        partner_billing.cancel_item(item, 'حذف')
        report = reports.build(*whole_period(), 'test')
        self.assertEqual(report.partner_owed_toman, 0)

    def test_margin_is_the_gap_between_retail_and_what_the_partner_paid(self):
        self.credit_order()
        report = reports.build(*whole_period(), 'test')
        # Retail 500,000 against a partner price of 350,000.
        self.assertEqual(report.partner_margin_toman, 150000)

    def test_a_partners_sales_are_listed_even_before_the_invoice_is_paid(self):
        self.credit_order()
        report = reports.build(*whole_period(), 'test')
        self.assertEqual(len(report.by_partner), 1)
        self.assertEqual(report.by_partner[0]['count'], 1)


class PrepaidRevenueTests(TestCase):
    def test_a_prepaid_partner_order_is_revenue_immediately(self):
        plan = make_plan(partner_price_toman=Decimal('350000'))
        partner = make_partner(billing_mode=Partner.BillingMode.PREPAID)
        order = create_partner_order(
            partner, plan, quote=price_quote(partner, plan),
            customer_label='Reza', client_name='reza-1', on_credit=False,
        )
        self.assertIsNotNone(order.revenue_at)
        report = reports.build(*whole_period(), 'test')
        self.assertEqual(report.revenue_toman, 350000)


class DirectSaleTests(TestCase):
    def test_ordinary_sales_are_untouched_by_any_of_this(self):
        plan = make_plan()
        user = TelegramUser.objects.create(chat_id=555)
        Order.objects.create(
            user=user, service=plan.service, plan=plan,
            status=Order.Status.PROVISIONED,
            amount_usd=Decimal('10'), amount_toman=Decimal('500000'),
        )
        report = reports.build(*whole_period(), 'test')
        self.assertEqual(report.revenue_toman, 500000)
        self.assertEqual(report.order_count, 1)
        self.assertEqual(report.partner_owed_toman, 0)
        self.assertEqual(report.by_partner, [])


class PanelCommentTests(TestCase):
    def test_the_comment_names_the_customer_and_the_partner(self):
        """Point 12: readable months later from inside 3x-ui alone."""
        plan = make_plan()
        partner = make_partner(display_name='Ali')
        order = create_partner_order(
            partner, plan, quote=price_quote(partner, plan),
            customer_label='Reza', client_name='reza-1', on_credit=True,
        )
        self.assertEqual(partner_comment(order), 'Customer: Reza | Partner: Ali')

    def test_a_direct_sale_gets_an_empty_comment(self):
        plan = make_plan()
        user = TelegramUser.objects.create(chat_id=666)
        order = Order.objects.create(
            user=user, service=plan.service, plan=plan,
            amount_usd=Decimal('10'), amount_toman=Decimal('1'),
        )
        self.assertEqual(partner_comment(order), '')

    def test_the_comment_reaches_the_client_payload(self):
        from sales.services.provisioning import build_client_payload

        plan = make_plan()
        partner = make_partner(display_name='Ali')
        order = create_partner_order(
            partner, plan, quote=price_quote(partner, plan),
            customer_label='Reza', client_name='reza-1', on_credit=True,
        )
        payload = build_client_payload(order, 'uuid', 'reza-1', timezone.now())
        self.assertEqual(payload['comment'], 'Customer: Reza | Partner: Ali')


class PartnerOrderShapeTests(TestCase):
    def test_a_partner_order_belongs_to_the_partner_and_labels_the_customer(self):
        plan = make_plan(partner_price_toman=Decimal('350000'))
        partner = make_partner()
        order = create_partner_order(
            partner, plan, quote=price_quote(partner, plan),
            customer_label='رضا محمدی', client_name='reza-1', on_credit=True,
        )
        self.assertEqual(order.user_id, partner.user_id)
        self.assertEqual(order.partner_id, partner.pk)
        self.assertEqual(order.customer_label, 'رضا محمدی')
        self.assertEqual(order.amount_toman, Decimal('350000'))
        self.assertEqual(order.partner_base_toman, Decimal('500000'))
        self.assertEqual(order.source, Order.Source.PARTNER_CREDIT)
