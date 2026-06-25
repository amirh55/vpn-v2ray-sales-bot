# ربات فروش کانفیگ V2Ray / 3x-ui + پنل مدیریت Django

این پروژه یک نسخه پایه عملیاتی برای فروش اشتراک VPN/V2Ray است و شامل این بخش‌هاست:

- ربات تلگرام با منوی فارسی
- خرید اشتراک جدید
- تمدید اشتراک
- کیف پول و شارژ با OxaPay
- سرویس‌های من و ارسال مجدد لینک‌ها
- آموزش اتصال
- ارتباط با پشتیبانی بدون نمایش اطلاعات مدیر به کاربر
- پنل مدیریت Django برای تنظیم ربات، OxaPay، پنل 3x-ui، سرویس‌ها و پلن‌ها
- قیمت‌گذاری دلاری با تبدیل خودکار به تومان بر اساس نرخ دلار قابل تنظیم در پنل
- اتصال به 3x-ui با API Token و انتخاب Inbound برای هر سرویس
- ارسال لینک کانفیگ، لینک Subscription و QR Code
- امکان فعال/غیرفعال کردن کارت‌به‌کارت
- ارسال پیام همگانی یا به یک Chat ID خاص از پنل

---

## 1) نصب سریع روی سرور یا مک

```bash
cd vpn_v2ray_sales_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 0.0.0.0:8000
```

پنل مدیریت:

```text
http://YOUR_SERVER:8000/admin/
```

برای اجرای ربات در یک ترمینال جدا:

```bash
source .venv/bin/activate
python manage.py bot
```

در سرور واقعی بهتر است `runserver` استفاده نشود و پروژه با `gunicorn + nginx + systemd` اجرا شود.

---

## 2) تنظیمات اصلی در پنل

بعد از ورود به `/admin/`:

1. وارد بخش **تنظیمات اصلی ربات** شوید.
2. این موارد را وارد کنید:
   - توکن ربات تلگرام
   - Chat ID پشتیبانی
   - Merchant API Key درگاه OxaPay
   - نرخ دلار به تومان
   - متن آموزش اتصال
   - متن ارتباط با ما
   - وضعیت کارت‌به‌کارت: فعال یا غیرفعال

نکته مهم: اگر کارت‌به‌کارت غیرفعال باشد، هیچ متن یا اطلاعات بانکی به کاربر نمایش داده نمی‌شود.

---

## 3) تنظیم OxaPay

درگاه OxaPay از مسیر زیر Webhook می‌گیرد:

```text
https://YOUR_DOMAIN.com/api/payments/oxapay/webhook/
```

در فایل `.env` مقدار `PUBLIC_BASE_URL` را روی دامنه واقعی HTTPS بگذارید:

```env
PUBLIC_BASE_URL=https://YOUR_DOMAIN.com
```

Webhook با HMAC بررسی می‌شود و فقط وضعیت `Paid` باعث شارژ کیف پول و تحویل خودکار می‌شود. وضعیت‌های موقت مثل `Paying` فقط ثبت می‌شوند و تحویل انجام نمی‌شود.

---

## 4) تنظیم 3x-ui / پنل سنایی

در پنل Django بخش **پنل‌های سنایی / 3x-ui** را بسازید:

- نام پنل
- آدرس پنل، مثل:

```text
https://panel.example.com/YOUR_PANEL_PATH
```

- API Token پنل 3x-ui
- مسیر API پیش‌فرض:

```text
/panel/api
```

3x-ui در نسخه‌های جدید OpenAPI را از این مسیر می‌دهد:

```text
https://panel.example.com/YOUR_PANEL_PATH/panel/api/openapi.json
```

اگر نسخه پنل شما مسیرهای متفاوتی داشته باشد، فایل زیر را تغییر دهید:

```text
sales/services/xui.py
```

این فایل چند مسیر رایج را امتحان می‌کند:

- `/panel/api/inbounds/addClient`
- `/panel/api/inbounds/client/add`
- `/panel/api/clients`
- `/panel/api/clients/add`

---

## 5) تعریف سرویس و پلن

### سرویس

در بخش **سرویس‌ها** مشخص کنید:

- نام سرویس، مثل: `VLESS Reality Germany`
- پنل 3x-ui مربوطه
- `Inbound ID`
- قالب لینک کانفیگ
- قالب لینک Subscription

چون ساختار لینک کانفیگ به تنظیمات Inbound شما وابسته است، قالب لینک‌ها قابل تنظیم گذاشته شده‌اند.

متغیرهای قابل استفاده در قالب:

```text
{uuid}
{client_id}
{email}
{inbound_id}
{panel_base_url}
{subscription_base_url}
{service_name}
{plan_name}
{telegram_id}
{duration_days}
{traffic_gb}
{traffic_bytes}
```

مثال قالب Subscription:

```text
https://sub.example.com/sub/{email}
```

مثال قالب کانفیگ، فقط نمونه است و باید با Inbound خودتان تنظیم شود:

```text
vless://{uuid}@your-domain.com:443?type=tcp&security=reality&fp=chrome&sni=google.com#{email}
```

### پلن

در بخش **پلن‌های اشتراک** یا داخل همان سرویس، پلن‌ها را تعریف کنید:

- قیمت دلاری
- مدت زمان روز
- حجم گیگابایت؛ عدد `0` یعنی نامحدود
- تعداد کاربر / IP Limit

قیمت تومان در ربات از ضرب `قیمت دلاری × نرخ دلار` محاسبه می‌شود.

---

## 6) جریان خرید در ربات

1. کاربر روی **خرید اشتراک جدید** می‌زند.
2. سرویس‌ها نمایش داده می‌شوند.
3. بعد از انتخاب سرویس، پلن‌های همان سرویس نمایش داده می‌شوند.
4. اگر کیف پول کاربر موجودی کافی داشته باشد:
   - مبلغ از کیف پول کم می‌شود.
   - کلاینت در 3x-ui ساخته می‌شود.
   - لینک کانفیگ، Subscription و QR Code ارسال می‌شود.
5. اگر موجودی کافی نباشد:
   - گزینه شارژ و خرید خودکار نمایش داده می‌شود.
   - بعد از پرداخت موفق OxaPay، کیف پول شارژ و خرید به صورت خودکار انجام می‌شود.

---

## 7) ارتباط با ما

کاربر پیام را داخل ربات ارسال می‌کند. ربات پیام را به Chat ID پشتیبانی می‌فرستد و فقط این اطلاعات را برای مدیر ارسال می‌کند:

- Chat ID کاربر
- Username اگر وجود داشته باشد
- متن پیام

اطلاعات مدیر یا پشتیبانی به کاربر نمایش داده نمی‌شود.

---

## 8) ارسال پیام به کاربران

در بخش **ارسال پیام گروهی/تکی**:

- اگر `target_chat_id` خالی باشد، پیام برای همه کاربران ارسال می‌شود.
- اگر Chat ID خاص وارد شود، فقط برای همان کاربر ارسال می‌شود.
- پیام را ذخیره کنید و از اکشن پنل، گزینه **قرار دادن در صف ارسال** را بزنید.
- تا وقتی دستور `python manage.py bot` در حال اجرا باشد، پیام‌ها ارسال می‌شوند.

---

## 9) کارت‌به‌کارت

کارت‌به‌کارت به صورت پیش‌فرض غیرفعال است.

برای فعال‌سازی:

1. وارد تنظیمات اصلی ربات شوید.
2. گزینه کارت‌به‌کارت را فعال کنید.
3. متن پرداخت کارت‌به‌کارت را وارد کنید.

اگر فعال باشد، کاربر رسید یا شماره پیگیری را داخل ربات ارسال می‌کند و مدیر از پنل می‌تواند درخواست را تایید کند. با تایید، کیف پول کاربر شارژ می‌شود.

---

## 10) اجرای دائمی روی سرور با systemd، نمونه ساده

فایل سرویس ربات:

```ini
[Unit]
Description=VPN Sales Telegram Bot
After=network.target

[Service]
WorkingDirectory=/opt/vpn_v2ray_sales_bot
ExecStart=/opt/vpn_v2ray_sales_bot/.venv/bin/python manage.py bot
Restart=always
User=www-data
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

فایل سرویس وب بهتر است با gunicorn اجرا شود.

---

## 11) نکات امنیتی مهم

- از HTTPS استفاده کنید؛ OxaPay برای Webhook روی localhost کار عملیاتی ندارد.
- `SECRET_KEY` و API Token ها را عمومی نکنید.
- فایل دیتابیس SQLite برای شروع مناسب است؛ برای فروش واقعی بهتر است PostgreSQL استفاده شود.
- پنل Django را پشت رمز قوی و ترجیحاً IP Whitelist قرار دهید.
- در صورت فعال کردن کارت‌به‌کارت، هر متنی که در `card_to_card_text` بنویسید به کاربر نمایش داده می‌شود.
- استفاده از VPN/V2Ray باید مطابق قوانین محل فعالیت شما باشد.

---

## 12) فایل‌های مهم برای توسعه بعدی

```text
sales/management/commands/bot.py       منطق ربات تلگرام
sales/models.py                        مدل‌های دیتابیس
sales/admin.py                         پنل مدیریت
sales/services/oxapay.py               اتصال به OxaPay
sales/services/xui.py                  اتصال به 3x-ui
sales/services/provisioning.py         ساخت و تمدید سرویس
sales/views.py                         Webhook پرداخت
```

---

## 13) مرحله بعدی پیشنهادی

بعد از تست اولیه، بهتر است این موارد اضافه شود:

- Webhook تلگرام به جای Polling
- اتصال مستقیم به OpenAPI هر پنل 3x-ui و ساخت خودکار Payload بر اساس نسخه پنل
- گزارش فروش روزانه/ماهانه
- کد تخفیف
- چند درگاه پرداخت
- پاسخ پشتیبانی از داخل پنل به کاربر
- قفل IP و Rate Limit برای پنل مدیریت
