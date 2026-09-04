"""Give every existing order the revenue timestamp it always implicitly had.

Reports used to read `created_at` and trust that a PAID or PROVISIONED order
meant money had arrived. That stops being true once a partner can buy on credit,
so revenue moved to its own field. For every order that already exists the two
are the same instant, which is why this backfill makes historical reports come
out identical rather than merely close.

Orders that never reached PAID are left null: they never were revenue.
"""

from django.db import migrations, models


def stamp_existing_orders(apps, schema_editor):
    Order = apps.get_model('sales', 'Order')
    Order.objects.filter(
        revenue_at__isnull=True,
        status__in=['paid', 'provisioned'],
    ).update(revenue_at=models.F('created_at'))


def clear_stamps(apps, schema_editor):
    Order = apps.get_model('sales', 'Order')
    Order.objects.update(revenue_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0017_order_customer_label_order_partner_base_toman_and_more'),
    ]

    operations = [
        migrations.RunPython(stamp_existing_orders, clear_stamps),
    ]
