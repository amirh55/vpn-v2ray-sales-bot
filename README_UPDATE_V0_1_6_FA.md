# آپدیت v0.1.6 - رفع خطای tgId در 3x-ui

این Patch خطای زیر را رفع می‌کند:

```text
json: cannot unmarshal string into Go struct field Client.client.tgId of type int64
```

علت خطا این بود که 3x-ui نسخه جدید فیلد `tgId` را عددی (`int64`) می‌خواهد، اما ربات آن را به شکل رشته ارسال می‌کرد.

## فایل‌های تغییرکرده

- `sales/services/xui.py`
- `sales/services/provisioning.py`
- `sales/management/commands/xui_probe.py`

## نصب روی سرور

```bash
cd /root/vpn-v2ray-sales-bot
unzip -o vpn_v2ray_sales_bot_update_v0_1_6_3xui_tgid_int_fix.zip
source .venv/bin/activate
python -m py_compile sales/services/xui.py sales/services/provisioning.py sales/management/commands/xui_probe.py
python manage.py check
pm2 restart vpn-bot vpn-panel
pm2 flush
```

## تست

```bash
python manage.py xui_probe --create-test --inbound-id 16
```

اگر تست موفق بود، خرید از داخل ربات هم باید انجام شود.
