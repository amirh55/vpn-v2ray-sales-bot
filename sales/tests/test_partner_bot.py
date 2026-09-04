"""The partner panel: who gets in, and what an order actually does.

Telegram and the 3x-ui panel are both stubbed. What is under test is the part
that costs money if it is wrong — access control, whether a failed provision
leaves a charge behind, and whether deleting a config takes its charge off the
invoice.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
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
    WalletTransaction,
)
from sales.services import partner_bot
from sales.tests.test_partner import make_partner, make_plan

PROVISION = 'sales.services.partner_bot.provision_order'
DELETE = 'sales.services.partner_bot.delete_order_client'
DELIVER = 'sales.services.partner_bot.send_delivery'
NOTIFY = 'sales.services.partner_bot.notify_operator'
UNIQUE_NAME = 'sales.services.partner_bot.unique_client_name'


class FakeBot:
    """Captures what would have been sent, so assertions can read it."""

    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)

    def edit_message_text(self, *args, **kwargs):
        self.messages.append(kwargs.get('text') or (args[0] if args else ''))

    @property
    def last(self) -> str:
        return self.messages[-1] if self.messages else ''

    @property
    def all_text(self) -> str:
        return '\n'.join(self.messages)


def fake_provision(order):
    """What the real provisioner leaves behind, minus the network call."""
    order.expires_at = timezone.now() + timezone.timedelta(days=order.plan.duration_days)
    order.xui_client_uuid = order.xui_client_uuid or 'uuid-test'
    order.status = Order.Status.PROVISIONED
    order.save()
    return order


def fake_call(chat_id: int, data: str):
    return SimpleNamespace(
        id='1', data=data,
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=1),
    )


def fake_message(chat_id: int, text: str):
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=1, text=text)


class PanelTestCase(TestCase):
    def setUp(self):
        self.bot = FakeBot()
        self.plan = make_plan(partner_price_toman=Decimal('350000'))
        site = SiteSetting.get_solo()
        site.partner_program_enabled = True
        site.is_shop_active = True
        site.partner_delete_refund_hours = 24
        site.save()

    def credit_partner(self, **kwargs):
        defaults = {'billing_mode': Partner.BillingMode.CREDIT, 'billing_cycle_days': 7}
        defaults.update(kwargs)
        return make_partner(**defaults)

    def stage_order(self, partner, label='Reza', client='reza-1'):
        """Put a partner at the confirm step, as the flow would."""
        user = partner.user
        user.temp_data = {'pw_plan': self.plan.pk, 'pw_label': label, 'pw_client': client}
        user.save(update_fields=['temp_data', 'updated_at'])
        return user


class AccessTests(PanelTestCase):
    def test_a_stranger_is_offered_the_application_form(self):
        user = TelegramUser.objects.create(chat_id=900)
        partner_bot.handle_work(self.bot, user, 900)
        self.assertIn('همکاری در فروش', self.bot.last)
        self.assertNotIn('سفارش جدید', self.bot.last)

    def test_a_guessed_callback_gets_no_panel(self):
        """Security is the chat id, not the command being hard to find."""
        user = TelegramUser.objects.create(chat_id=901)
        handled = partner_bot.handle_callback(self.bot, user, fake_call(901, 'pw:new'))
        self.assertTrue(handled)
        self.assertNotIn('سرویس مورد نظر', self.bot.last)

    def test_a_guessed_order_callback_creates_nothing(self):
        user = TelegramUser.objects.create(chat_id=902)
        user.temp_data = {'pw_plan': self.plan.pk, 'pw_label': 'x', 'pw_client': 'x1'}
        user.save()
        partner_bot.handle_callback(self.bot, user, fake_call(902, 'pw:ok'))
        self.assertEqual(Order.objects.count(), 0)

    def test_a_deactivated_partner_is_told_why(self):
        partner = self.credit_partner(is_active=False)
        partner_bot.handle_work(self.bot, partner.user, partner.user.chat_id)
        self.assertIn('غیرفعال', self.bot.last)
        self.assertIsNone(partner_bot.get_partner(partner.user))

    def test_the_panel_is_closed_when_the_program_is_off(self):
        site = SiteSetting.get_solo()
        site.partner_program_enabled = False
        site.save()
        partner = self.credit_partner()
        partner_bot.handle_work(self.bot, partner.user, partner.user.chat_id)
        self.assertIn('فعال نیست', self.bot.last)

    def test_an_active_partner_sees_the_panel(self):
        partner = self.credit_partner()
        partner_bot.handle_work(self.bot, partner.user, partner.user.chat_id)
        self.assertIn('پنل همکاری', self.bot.last)


class NameResolutionTests(PanelTestCase):
    def test_an_english_name_becomes_both_the_label_and_the_config(self):
        partner = self.credit_partner()
        label, client = partner_bot.resolve_names(partner.user, None, 'reza2')
        self.assertEqual(label, 'reza2')
        self.assertEqual(client, 'reza2')

    def test_a_persian_name_is_kept_as_the_label_only(self):
        """A partner must not hit a dead end over naming rules they never saw."""
        partner = self.credit_partner()
        label, client = partner_bot.resolve_names(partner.user, None, 'رضا محمدی')
        self.assertEqual(label, 'رضا محمدی')
        self.assertTrue(client.isascii())
        self.assertTrue(client)

    def test_a_taken_name_falls_back_to_a_generated_one(self):
        partner = self.credit_partner()
        Order.objects.create(
            user=partner.user, service=self.plan.service, plan=self.plan,
            amount_usd=Decimal('1'), amount_toman=Decimal('1'), xui_client_email='reza2',
        )
        label, client = partner_bot.resolve_names(partner.user, None, 'reza2')
        self.assertEqual(label, 'reza2')
        self.assertNotEqual(client, 'reza2')


class CreditOrderTests(PanelTestCase):
    def test_placing_an_order_charges_the_open_invoice(self):
        partner = self.credit_partner()
        user = self.stage_order(partner)
        with mock.patch(PROVISION, side_effect=fake_provision), mock.patch(DELIVER), mock.patch(NOTIFY):
            partner_bot.place_order(self.bot, partner, user, user.chat_id)

        order = Order.objects.get()
        self.assertEqual(order.partner_id, partner.pk)
        self.assertEqual(order.customer_label, 'Reza')
        self.assertEqual(order.amount_toman, Decimal('350000'))
        self.assertIsNone(order.revenue_at)

        invoice = partner.open_invoice()
        self.assertEqual(invoice.total_toman, Decimal('350000'))
        self.assertEqual(invoice.items.count(), 1)

    def test_three_orders_land_on_one_invoice(self):
        partner = self.credit_partner()
        for index in range(3):
            user = self.stage_order(partner, label=f'c{index}', client=f'cfg-{index}')
            with mock.patch(PROVISION, side_effect=fake_provision), mock.patch(DELIVER), mock.patch(NOTIFY):
                partner_bot.place_order(self.bot, partner, user, user.chat_id)
        self.assertEqual(PartnerInvoice.objects.count(), 1)
        self.assertEqual(partner.open_invoice().total_toman, Decimal('1050000'))

    def test_a_failed_provision_leaves_no_charge_behind(self):
        """The partner must never be billed for a config that does not exist."""
        partner = self.credit_partner()
        user = self.stage_order(partner)
        with mock.patch(PROVISION, side_effect=RuntimeError('panel down')), mock.patch(NOTIFY):
            partner_bot.place_order(self.bot, partner, user, user.chat_id)

        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.FAILED)
        item = PartnerInvoiceItem.objects.get()
        self.assertTrue(item.is_cancelled)
        item.invoice.refresh_from_db()
        self.assertEqual(item.invoice.total_toman, Decimal('0'))
        self.assertIn('مبلغی از شما گرفته نشد', self.bot.all_text)

    def test_an_overdue_invoice_blocks_a_new_order(self):
        partner = self.credit_partner()
        user = self.stage_order(partner)
        with mock.patch(PROVISION, side_effect=fake_provision), mock.patch(DELIVER), mock.patch(NOTIFY):
            partner_bot.place_order(self.bot, partner, user, user.chat_id)
        PartnerInvoice.objects.update(status=PartnerInvoice.Status.OVERDUE)

        user = self.stage_order(partner, label='Ali', client='ali-1')
        with mock.patch(PROVISION) as provision, mock.patch(NOTIFY):
            partner_bot.place_order(self.bot, partner, user, user.chat_id)
        provision.assert_not_called()
        self.assertEqual(Order.objects.count(), 1)
        self.assertIn('سررسید', self.bot.all_text)

    def test_the_credit_ceiling_refuses_the_order_that_crosses_it(self):
        partner = self.credit_partner(credit_limit_toman=Decimal('500000'))
        user = self.stage_order(partner)
        with mock.patch(PROVISION, side_effect=fake_provision), mock.patch(DELIVER), mock.patch(NOTIFY):
            partner_bot.place_order(self.bot, partner, user, user.chat_id)

        user = self.stage_order(partner, label='Ali', client='ali-1')
        with mock.patch(PROVISION) as provision, mock.patch(NOTIFY):
            partner_bot.place_order(self.bot, partner, user, user.chat_id)
        provision.assert_not_called()
        self.assertIn('سقف اعتبار', self.bot.all_text)

    def test_a_plan_outside_the_partners_allowance_is_refused(self):
        partner = self.credit_partner()
        other = make_plan(name='forbidden')
        partner.allowed_plans.add(other)
        user = self.stage_order(partner)
        with mock.patch(PROVISION) as provision, mock.patch(NOTIFY):
            partner_bot.place_order(self.bot, partner, user, user.chat_id)
        provision.assert_not_called()
        self.assertEqual(Order.objects.count(), 0)


class PrepaidOrderTests(PanelTestCase):
    def test_the_wallet_is_debited_and_no_invoice_is_made(self):
        partner = make_partner(billing_mode=Partner.BillingMode.PREPAID)
        partner.user.wallet_balance_toman = Decimal('400000')
        partner.user.save()
        user = self.stage_order(partner)
        with mock.patch(PROVISION, side_effect=fake_provision), mock.patch(DELIVER), mock.patch(NOTIFY):
            partner_bot.place_order(self.bot, partner, user, user.chat_id)

        partner.user.refresh_from_db()
        self.assertEqual(partner.user.wallet_balance_toman, Decimal('50000'))
        self.assertEqual(PartnerInvoice.objects.count(), 0)
        self.assertIsNotNone(Order.objects.get().revenue_at)

    def test_an_underfunded_wallet_creates_nothing(self):
        partner = make_partner(billing_mode=Partner.BillingMode.PREPAID)
        user = self.stage_order(partner)
        with mock.patch(PROVISION) as provision, mock.patch(NOTIFY):
            partner_bot.place_order(self.bot, partner, user, user.chat_id)
        provision.assert_not_called()
        self.assertEqual(Order.objects.count(), 0)
        partner.user.refresh_from_db()
        self.assertEqual(partner.user.wallet_balance_toman, Decimal('0'))

    def test_a_failed_provision_refunds_the_wallet(self):
        partner = make_partner(billing_mode=Partner.BillingMode.PREPAID)
        partner.user.wallet_balance_toman = Decimal('400000')
        partner.user.save()
        user = self.stage_order(partner)
        with mock.patch(PROVISION, side_effect=RuntimeError('panel down')), mock.patch(NOTIFY):
            partner_bot.place_order(self.bot, partner, user, user.chat_id)

        partner.user.refresh_from_db()
        self.assertEqual(partner.user.wallet_balance_toman, Decimal('400000'))
        self.assertTrue(
            WalletTransaction.objects.filter(kind=WalletTransaction.Kind.REFUND).exists()
        )


class DeleteTests(PanelTestCase):
    def place(self, partner):
        user = self.stage_order(partner)
        with mock.patch(PROVISION, side_effect=fake_provision), mock.patch(DELIVER), mock.patch(NOTIFY):
            partner_bot.place_order(self.bot, partner, user, user.chat_id)
        return Order.objects.get()

    def test_deleting_inside_the_window_takes_the_charge_off(self):
        partner = self.credit_partner()
        order = self.place(partner)
        with mock.patch(DELETE, return_value=True), mock.patch(NOTIFY):
            partner_bot.do_delete(self.bot, partner, partner.user.chat_id, order.pk)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        item = PartnerInvoiceItem.objects.get()
        self.assertTrue(item.is_cancelled)
        self.assertIn('از فاکتور برداشته شد', self.bot.all_text)

    def test_deleting_outside_the_window_keeps_the_charge(self):
        partner = self.credit_partner()
        order = self.place(partner)
        PartnerInvoiceItem.objects.update(created_at=timezone.now() - timezone.timedelta(hours=25))
        with mock.patch(DELETE, return_value=True), mock.patch(NOTIFY):
            partner_bot.do_delete(self.bot, partner, partner.user.chat_id, order.pk)

        item = PartnerInvoiceItem.objects.get()
        self.assertFalse(item.is_cancelled)
        item.invoice.refresh_from_db()
        self.assertEqual(item.invoice.total_toman, Decimal('350000'))
        self.assertIn('مهلت برگشت وجه گذشته', self.bot.all_text)

    def test_a_paid_invoice_is_never_reduced_by_a_delete(self):
        partner = self.credit_partner()
        order = self.place(partner)
        invoice = partner.open_invoice()
        with mock.patch('sales.services.partner_billing.set_order_client_enabled'):
            from sales.services.partner_billing import settle_invoice

            settle_invoice(invoice, via=PartnerInvoice.SettledBy.ADMIN)
        with mock.patch(DELETE, return_value=True), mock.patch(NOTIFY):
            partner_bot.do_delete(self.bot, partner, partner.user.chat_id, order.pk)

        item = PartnerInvoiceItem.objects.get()
        self.assertFalse(item.is_cancelled)
        invoice.refresh_from_db()
        self.assertEqual(invoice.total_toman, Decimal('350000'))

    def test_a_panel_failure_changes_nothing(self):
        """A config that is still live must not have its charge written off."""
        partner = self.credit_partner()
        order = self.place(partner)
        with mock.patch(DELETE, side_effect=RuntimeError('panel down')), mock.patch(NOTIFY):
            partner_bot.do_delete(self.bot, partner, partner.user.chat_id, order.pk)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PROVISIONED)
        self.assertFalse(PartnerInvoiceItem.objects.get().is_cancelled)

    def test_a_partner_cannot_delete_another_partners_config(self):
        mine = self.credit_partner(chat_id=111)
        theirs = self.credit_partner(chat_id=222)
        order = self.place(theirs)
        with mock.patch(DELETE) as delete, mock.patch(NOTIFY):
            partner_bot.do_delete(self.bot, mine, mine.user.chat_id, order.pk)
        delete.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PROVISIONED)


class RequestTests(PanelTestCase):
    def test_a_submitted_form_becomes_a_pending_request(self):
        user = TelegramUser.objects.create(chat_id=950)
        with mock.patch(NOTIFY):
            partner_bot.submit_request(
                self.bot, user, 950, 'رضا محمدی\n09121234567\nکانال تلگرام\n۵۰ کاربر'
            )
        request = user.partner_requests.get()
        self.assertEqual(request.full_name, 'رضا محمدی')
        self.assertEqual(request.phone, '09121234567')
        self.assertEqual(request.status, 'pending')

    def test_a_half_filled_form_is_refused(self):
        user = TelegramUser.objects.create(chat_id=951)
        with mock.patch(NOTIFY):
            partner_bot.submit_request(self.bot, user, 951, 'رضا')
        self.assertEqual(user.partner_requests.count(), 0)

    def test_a_pending_request_is_shown_instead_of_the_form(self):
        user = TelegramUser.objects.create(chat_id=952)
        user.partner_requests.create(full_name='x', phone='1')
        partner_bot.handle_work(self.bot, user, 952)
        self.assertIn('در حال بررسی', self.bot.last)
