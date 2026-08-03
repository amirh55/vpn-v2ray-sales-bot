from decimal import Decimal

from django.db import migrations


def fill_price_toman(apps, schema_editor):
    """Carry existing prices over to the new manual toman field.

    Before this, the toman price was computed as price_usd * dollar_rate on
    every read. Leaving the new field at its 0 default would put every existing
    plan on sale for nothing.
    """
    SiteSetting = apps.get_model('sales', 'SiteSetting')
    Plan = apps.get_model('sales', 'Plan')

    site = SiteSetting.objects.first()
    rate = Decimal(site.dollar_rate_toman) if site and site.dollar_rate_toman else Decimal('60000')

    for plan in Plan.objects.filter(price_toman=0):
        plan.price_toman = (Decimal(plan.price_usd) * rate).quantize(Decimal('1'))
        plan.save(update_fields=['price_toman'])


def noop(apps, schema_editor):
    """The old behaviour recomputed the price, so there is nothing to undo."""


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0005_cardpaymentrequest_auto_purchase_after_paid_and_more'),
    ]

    operations = [
        migrations.RunPython(fill_price_toman, noop),
    ]
