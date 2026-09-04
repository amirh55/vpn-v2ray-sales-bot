"""Phase 1 of the partner program: pricing, credit, invoices, revenue.

These cover the arithmetic and the guards, not the bot flow — nothing here
touches a panel or Telegram. They exist because a wrong partner price or a
mis-counted debt is invisible until money has already changed hands.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from sales.models import (
    Order,
    Partner,
    PartnerInvoice,
    PartnerInvoiceItem,
    PartnerPlanPrice,
    Plan,
    Service,
    SiteSetting,
    TelegramUser,
    XUIPanel,
)


def make_plan(**kwargs) -> Plan:
    panel = XUIPanel.objects.create(name='p', base_url='https://panel.example.com/x')
    service = Service.objects.create(name='آلمان', panel=panel, inbound_ids='1')
    defaults = {
        'service': service,
        'name': 'یک‌ماهه',
        'price_toman': Decimal('500000'),
        'price_usd': Decimal('10.00'),
        'duration_days': 30,
    }
    defaults.update(kwargs)
    return Plan.objects.create(**defaults)


def make_partner(chat_id: int = 111, **kwargs) -> Partner:
    user = TelegramUser.objects.create(chat_id=chat_id, first_name='Ali')
    defaults = {'user': user, 'display_name': 'Ali'}
    defaults.update(kwargs)
    return Partner.objects.create(**defaults)


class PartnerPricingTests(TestCase):
    """The three layers, most specific winning."""

    def test_falls_back_to_the_ordinary_price(self):
        plan = make_plan()
        partner = make_partner()
        self.assertEqual(partner.final_price_toman(plan), Decimal('500000'))

    def test_plan_partner_price_beats_the_ordinary_price(self):
        plan = make_plan(partner_price_toman=Decimal('350000'))
        partner = make_partner()
        self.assertEqual(partner.final_price_toman(plan), Decimal('350000'))

    def test_partner_specific_price_beats_the_plan_partner_price(self):
        plan = make_plan(partner_price_toman=Decimal('350000'))
        partner = make_partner()
        PartnerPlanPrice.objects.create(partner=partner, plan=plan, price_toman=Decimal('300000'))
        self.assertEqual(partner.final_price_toman(plan), Decimal('300000'))

    def test_discount_percent_applies_to_the_partner_price(self):
        """The worked example from the brief: 350,000 at 10% is 315,000."""
        plan = make_plan(partner_price_toman=Decimal('350000'))
        partner = make_partner(discount_percent=10)
        self.assertEqual(partner.final_price_toman(plan), Decimal('315000'))

    def test_zero_discount_leaves_the_price_alone(self):
        plan = make_plan(partner_price_toman=Decimal('350000'))
        partner = make_partner(discount_percent=0)
        self.assertEqual(partner.final_price_toman(plan), Decimal('350000'))

    def test_price_rounds_down_never_up(self):
        """337,000 × 93% is 313,410 — the partner pays 313,000, not 314,000."""
        plan = make_plan(partner_price_toman=Decimal('337000'))
        partner = make_partner(discount_percent=7)
        self.assertEqual(partner.final_price_toman(plan), Decimal('313000'))

    def test_zero_partner_price_on_plan_is_treated_as_unset(self):
        plan = make_plan(partner_price_toman=Decimal('0'))
        partner = make_partner()
        self.assertEqual(partner.final_price_toman(plan), Decimal('500000'))

    def test_saving_is_measured_against_the_ordinary_price(self):
        plan = make_plan(partner_price_toman=Decimal('350000'))
        partner = make_partner(discount_percent=10)
        self.assertEqual(partner.saving_toman(plan), Decimal('185000'))


class PartnerUsdPricingTests(TestCase):
    def test_dollar_price_is_derived_from_toman_when_unset(self):
        SiteSetting.objects.filter(pk=1).delete()
        site = SiteSetting.get_solo()
        site.dollar_rate_toman = Decimal('100000')
        site.save()
        plan = make_plan(partner_price_toman=Decimal('350000'))
        partner = make_partner(discount_percent=10)
        # 315,000 toman at 100,000 per dollar.
        self.assertEqual(partner.final_price_usd(plan), Decimal('3.15'))

    def test_explicit_dollar_price_wins_and_takes_the_discount(self):
        plan = make_plan(partner_price_usd=Decimal('7.00'))
        partner = make_partner(discount_percent=10)
        self.assertEqual(partner.final_price_usd(plan), Decimal('6.30'))


class PartnerAllowedPlanTests(TestCase):
    def test_empty_lists_mean_every_active_plan(self):
        plan = make_plan()
        partner = make_partner()
        self.assertTrue(partner.may_sell(plan))

    def test_allowed_services_limits_to_that_service(self):
        plan = make_plan()
        other = make_plan(name='دوماهه')
        partner = make_partner()
        partner.allowed_services.add(plan.service)
        self.assertTrue(partner.may_sell(plan))
        self.assertFalse(partner.may_sell(other))

    def test_allowed_plans_beats_allowed_services(self):
        """A carve-out of one plan wins over a whole service being allowed."""
        plan = make_plan()
        sibling = Plan.objects.create(
            service=plan.service, name='سه‌ماهه', price_toman=Decimal('1'),
            price_usd=Decimal('1'), duration_days=90,
        )
        partner = make_partner()
        partner.allowed_services.add(plan.service)
        partner.allowed_plans.add(sibling)
        self.assertFalse(partner.may_sell(plan))
        self.assertTrue(partner.may_sell(sibling))

    def test_inactive_plans_are_never_sellable(self):
        plan = make_plan(is_active=False)
        partner = make_partner()
        self.assertFalse(partner.may_sell(plan))


class PartnerCreditTests(TestCase):
    def setUp(self):
        self.partner = make_partner(
            billing_mode=Partner.BillingMode.CREDIT, credit_limit_toman=Decimal('5000000')
        )

    def open_invoice(self, amount, status=PartnerInvoice.Status.OPEN):
        now = timezone.now()
        invoice = PartnerInvoice.objects.create(
            partner=self.partner, status=status, opened_at=now,
            due_at=now + timezone.timedelta(days=7),
        )
        PartnerInvoiceItem.objects.create(
            invoice=invoice, title='x', amount_toman=Decimal(amount)
        )
        invoice.recalculate_total()
        return invoice

    def test_zero_limit_means_unlimited(self):
        self.partner.credit_limit_toman = Decimal('0')
        self.assertIsNone(self.partner.remaining_credit_toman())
        self.assertTrue(self.partner.is_within_credit(Decimal('999999999')))

    def test_debt_is_the_sum_of_unsettled_invoices(self):
        self.open_invoice('3000000')
        self.assertEqual(self.partner.current_debt_toman(), Decimal('3000000'))
        self.assertEqual(self.partner.remaining_credit_toman(), Decimal('2000000'))

    def test_paid_invoices_do_not_count_as_debt(self):
        invoice = self.open_invoice('3000000')
        invoice.status = PartnerInvoice.Status.PAID
        invoice.save()
        self.assertEqual(self.partner.current_debt_toman(), Decimal('0'))

    def test_an_order_landing_exactly_on_the_limit_is_allowed(self):
        self.open_invoice('3000000')
        self.assertTrue(self.partner.is_within_credit(Decimal('2000000')))

    def test_one_toman_over_the_limit_is_refused(self):
        self.open_invoice('3000000')
        self.assertFalse(self.partner.is_within_credit(Decimal('2000001')))


class PartnerOrderLockTests(TestCase):
    """Only an overdue invoice blocks ordering — point 7 read against point 4."""

    def setUp(self):
        self.partner = make_partner(billing_mode=Partner.BillingMode.CREDIT)
        self.now = timezone.now()

    def make_invoice(self, status):
        return PartnerInvoice.objects.create(
            partner=self.partner, status=status, opened_at=self.now,
            due_at=self.now + timezone.timedelta(days=7),
        )

    def test_an_open_invoice_does_not_block(self):
        self.make_invoice(PartnerInvoice.Status.OPEN)
        self.assertIsNone(self.partner.overdue_invoice())

    def test_an_overdue_invoice_blocks(self):
        self.make_invoice(PartnerInvoice.Status.OVERDUE)
        self.assertIsNotNone(self.partner.overdue_invoice())

    def test_an_overdue_invoice_is_still_the_one_new_orders_join(self):
        invoice = self.make_invoice(PartnerInvoice.Status.OVERDUE)
        self.assertEqual(self.partner.open_invoice(), invoice)

    def test_a_paid_invoice_leaves_no_open_cycle(self):
        self.make_invoice(PartnerInvoice.Status.PAID)
        self.assertIsNone(self.partner.open_invoice())


class PartnerInvoiceTests(TestCase):
    def setUp(self):
        self.partner = make_partner()
        now = timezone.now()
        self.invoice = PartnerInvoice.objects.create(
            partner=self.partner, opened_at=now, due_at=now + timezone.timedelta(days=7)
        )

    def test_number_is_generated_on_creation(self):
        self.assertEqual(self.invoice.number, f'INV-{self.invoice.pk:06d}')

    def test_total_is_the_sum_of_live_lines(self):
        PartnerInvoiceItem.objects.create(invoice=self.invoice, title='a', amount_toman=Decimal('100000'))
        PartnerInvoiceItem.objects.create(invoice=self.invoice, title='b', amount_toman=Decimal('250000'))
        self.assertEqual(self.invoice.recalculate_total(), Decimal('350000'))

    def test_cancelled_lines_drop_out_of_the_total(self):
        PartnerInvoiceItem.objects.create(invoice=self.invoice, title='a', amount_toman=Decimal('100000'))
        PartnerInvoiceItem.objects.create(
            invoice=self.invoice, title='b', amount_toman=Decimal('250000'), is_cancelled=True
        )
        self.assertEqual(self.invoice.recalculate_total(), Decimal('100000'))

    def test_an_empty_invoice_totals_zero_and_is_not_payable(self):
        self.assertEqual(self.invoice.recalculate_total(), Decimal('0'))
        self.assertFalse(self.invoice.is_payable)


class OrderRevenueTests(TestCase):
    """Revenue is stamped once, and never for money that has not arrived."""

    def setUp(self):
        self.plan = make_plan()
        self.user = TelegramUser.objects.create(chat_id=222)

    def make_order(self, **kwargs):
        defaults = {
            'user': self.user, 'service': self.plan.service, 'plan': self.plan,
            'amount_usd': Decimal('10'), 'amount_toman': Decimal('500000'),
        }
        defaults.update(kwargs)
        return Order.objects.create(**defaults)

    def test_a_paid_order_is_stamped_immediately(self):
        order = self.make_order(status=Order.Status.PAID)
        self.assertIsNotNone(order.revenue_at)

    def test_a_pending_order_is_not_stamped(self):
        order = self.make_order(status=Order.Status.PENDING)
        self.assertIsNone(order.revenue_at)

    def test_a_pending_order_is_stamped_when_it_becomes_provisioned(self):
        order = self.make_order(status=Order.Status.PENDING)
        order.status = Order.Status.PROVISIONED
        order.save()
        self.assertIsNotNone(order.revenue_at)

    def test_a_partner_credit_order_is_not_stamped_on_provisioning(self):
        partner = make_partner(chat_id=333)
        order = self.make_order(
            status=Order.Status.PROVISIONED,
            source=Order.Source.PARTNER_CREDIT,
            partner=partner,
        )
        self.assertIsNone(order.revenue_at)

    def test_the_stamp_is_not_moved_by_a_later_save(self):
        order = self.make_order(status=Order.Status.PAID)
        first = order.revenue_at
        order.status = Order.Status.PROVISIONED
        order.save()
        order.refresh_from_db()
        self.assertEqual(order.revenue_at, first)

    def test_stamping_survives_a_narrow_update_fields_save(self):
        """update_fields must not silently drop the new column."""
        order = self.make_order(status=Order.Status.PENDING)
        order.status = Order.Status.PAID
        order.save(update_fields=['status'])
        order.refresh_from_db()
        self.assertIsNotNone(order.revenue_at)


class OrderSuspensionTests(TestCase):
    def setUp(self):
        self.plan = make_plan()
        self.user = TelegramUser.objects.create(chat_id=444)
        self.order = Order.objects.create(
            user=self.user, service=self.plan.service, plan=self.plan,
            amount_usd=Decimal('10'), amount_toman=Decimal('500000'),
            status=Order.Status.PROVISIONED,
            expires_at=timezone.now() + timezone.timedelta(days=30),
        )

    def test_a_normal_provisioned_order_is_active(self):
        self.assertTrue(self.order.is_active)

    def test_a_suspended_order_is_not_active(self):
        self.order.suspended_at = timezone.now()
        self.order.save()
        self.assertFalse(self.order.is_active)

    def test_suspension_does_not_touch_the_expiry_or_the_client(self):
        """Point 9: disabled, not deleted. The config's identity survives."""
        self.order.xui_client_uuid = 'abc'
        self.order.xui_client_email = 'ali-1234'
        self.order.save()
        expiry = self.order.expires_at
        self.order.suspended_at = timezone.now()
        self.order.save()
        self.order.refresh_from_db()
        self.assertEqual(self.order.xui_client_uuid, 'abc')
        self.assertEqual(self.order.xui_client_email, 'ali-1234')
        self.assertEqual(self.order.expires_at, expiry)

    def test_a_suspended_order_stays_in_the_customers_list(self):
        """The partner still has to be able to see and pay for it."""
        self.order.suspended_at = timezone.now()
        self.order.save()
        self.assertIn(self.order, Order.objects.visible_to_customer())
