"""The partner models have to be reachable in the admin, not merely registered.

This panel builds its sidebar from a hand-written list in settings and its
settings form from explicit fieldsets, so a model can be registered correctly
and still be invisible to the operator. That is exactly what happened the first
time these were added, which is why it is now tested rather than assumed.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from sales.models import Partner, PartnerInvoice, SiteSetting
from sales.tests.test_partner import make_partner, make_plan


class AdminReachabilityTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('boss', 'b@x.com', 'pw')
        self.client.force_login(self.admin)

    def test_every_partner_page_loads(self):
        for name in (
            'admin:sales_partner_changelist',
            'admin:sales_partner_add',
            'admin:sales_partnerrequest_changelist',
            'admin:sales_partnerinvoice_changelist',
            'admin:sales_partnerplanprice_changelist',
            'admin:sales_partnerplanprice_add',
        ):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_the_sidebar_links_to_the_partner_section(self):
        """A registered model that the sidebar does not name is invisible."""
        page = self.client.get(reverse('admin:sales_order_changelist')).content.decode()
        self.assertIn(reverse('admin:sales_partner_changelist'), page)
        self.assertIn(reverse('admin:sales_partnerrequest_changelist'), page)
        self.assertIn(reverse('admin:sales_partnerinvoice_changelist'), page)
        self.assertIn('همکاری در فروش', page)

    def test_the_enable_switch_is_on_the_settings_form(self):
        """Without this the operator cannot turn the program on at all."""
        site = SiteSetting.get_solo()
        page = self.client.get(
            reverse('admin:sales_sitesetting_change', args=[site.pk])
        ).content.decode()
        self.assertIn('name="partner_program_enabled"', page)
        self.assertIn('name="partner_delete_refund_hours"', page)

    def test_the_partner_price_is_on_the_plan_form(self):
        plan = make_plan()
        page = self.client.get(
            reverse('admin:sales_plan_change', args=[plan.pk])
        ).content.decode()
        self.assertIn('name="partner_price_toman"', page)

    def test_a_partner_can_be_created_through_the_form(self):
        """The operator's actual job: turn a Telegram user into a partner."""
        plan = make_plan()
        from sales.models import TelegramUser

        user = TelegramUser.objects.create(chat_id=8001, first_name='Ali')
        response = self.client.post(
            reverse('admin:sales_partner_add'),
            {
                'user': user.pk,
                'display_name': 'Ali',
                'is_active': 'on',
                'billing_mode': Partner.BillingMode.CREDIT,
                'billing_cycle_days': 7,
                'credit_limit_toman': '5000000',
                'discount_percent': 10,
                'note': '',
                'plan_prices-TOTAL_FORMS': '0',
                'plan_prices-INITIAL_FORMS': '0',
                'plan_prices-MIN_NUM_FORMS': '0',
                'plan_prices-MAX_NUM_FORMS': '1000',
            },
        )
        self.assertEqual(response.status_code, 302, response.context['errors'] if response.context else '')
        partner = Partner.objects.get()
        self.assertEqual(partner.display_name, 'Ali')
        self.assertEqual(partner.final_price_toman(plan), Decimal('450000'))

    def test_the_order_list_shows_who_sold_it(self):
        partner = make_partner()
        page = self.client.get(reverse('admin:sales_order_changelist')).content.decode()
        self.assertIn('فروشنده', page)
        self.assertTrue(partner.pk)

    def test_an_invoice_page_opens(self):
        from django.utils import timezone

        partner = make_partner()
        invoice = PartnerInvoice.objects.create(
            partner=partner,
            opened_at=timezone.now(),
            due_at=timezone.now() + timezone.timedelta(days=7),
        )
        response = self.client.get(reverse('admin:sales_partnerinvoice_change', args=[invoice.pk]))
        self.assertEqual(response.status_code, 200)
