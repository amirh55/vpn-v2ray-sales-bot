# آپدیت v0.1.4 - رفع قطعی ساخت Client در 3x-ui 3.3.x

این آپدیت خطای زیر را رفع می‌کند:

`client email is required`

در نسخه‌های جدید 3x-ui مسیر ساخت کاربر `POST /panel/api/clients/add` است و بدنه باید به شکل زیر باشد:

```json
{"client": {"email": "..."}, "inboundIds": [16]}
```

Patch قبلی email را در سطح اول JSON می‌فرستاد و پنل 3x-ui آن را به عنوان Client تشخیص نمی‌داد.

## نصب روی سرور

```bash
cd /root/vpn-v2ray-sales-bot
unzip -o vpn_v2ray_sales_bot_update_v0_1_4_3xui_clients_add_payload_fix.zip
source .venv/bin/activate
python -m py_compile sales/services/xui.py sales/management/commands/bot.py
python manage.py check
pm2 restart vpn-bot vpn-panel
pm2 flush
pm2 logs vpn-bot
```

بعد دوباره یک خرید تست انجام بده.
