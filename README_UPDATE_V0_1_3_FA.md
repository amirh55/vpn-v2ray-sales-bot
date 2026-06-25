# آپدیت v0.1.3 - سازگاری با API جدید 3x-ui 3.x

این آپدیت خطای زیر را رفع می‌کند:

`client email is required`

علت: نسخه‌های جدید 3x-ui، مخصوصاً 3.x، API ساخت کاربر را به مسیر client-level یعنی `/panel/api/clients` منتقل کرده‌اند و کلاینت را به inbound متصل می‌کنند. این Patch اول API جدید را امتحان می‌کند و بعد فقط در صورت نیاز سراغ API قدیمی `inbounds/addClient` می‌رود.

## نصب روی سرور

```bash
cd /root/vpn-v2ray-sales-bot
unzip -o vpn_v2ray_sales_bot_update_v0_1_3_3xui_v3_clients_api_fix.zip
source .venv/bin/activate
python -m py_compile sales/services/xui.py sales/services/provisioning.py
python manage.py check
pm2 restart vpn-bot vpn-panel
pm2 logs vpn-bot
```

## بعد از نصب

یک خرید تست انجام بده. اگر باز خطا گرفتی، لاگ جدید حالا مشخص می‌کند کدام مسیر API جواب نداده است.

