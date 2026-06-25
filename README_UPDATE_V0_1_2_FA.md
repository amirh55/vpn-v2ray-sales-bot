# Patch v0.1.2 ربات فروش V2Ray

این Patch برای این موارد است:

- رفع خطای `ModuleNotFoundError: No module named 'sales.urls'`
- رفع مشکل `Unknown command: 'bot'` با اضافه شدن `__init__.py`های لازم برای commandهای Django
- انتقال محل پیش‌فرض دیتابیس، media و فایل تنظیمات پایدار به `/var/lib/vpnshop`
- اضافه شدن اسکریپت‌های پایدار برای PM2
- نمایش دکمه‌های اصلی ربات در منوی پایین تلگرام به جای دکمه‌های inline زیر پیام خوش‌آمد
- حفظ اصلاح قبلی 3x-ui برای ساخت `client email` امن و غیرخالی

## نصب Patch روی سرور

فایل ZIP را داخل مسیر پروژه قرار بده و اجرا کن:

```bash
cd ~/vpn-v2ray-sales-bot
unzip -o vpn_v2ray_sales_bot_update_v0_1_2_server_persistent_menu.zip
chmod +x scripts/run_panel.sh scripts/run_bot.sh
source .venv/bin/activate
pip install -r requirements.txt
```

## انتقال تنظیمات و دیتابیس به مسیر پایدار سرور

این دستور را فقط بار اول بعد از نصب این Patch بزن:

```bash
python manage.py runtime_prepare --copy-existing
```

این دستور اگر فایل‌های زیر در پروژه قبلی وجود داشته باشند، آن‌ها را به مسیر پایدار کپی می‌کند:

- `.env` → `/var/lib/vpnshop/.env`
- `db.sqlite3` → `/var/lib/vpnshop/db.sqlite3`
- `media/` → `/var/lib/vpnshop/media/`

از این به بعد اگر فایل‌های پروژه را از GitHub آپدیت کنی، تنظیمات، دیتابیس، کیف پول کاربران، سفارش‌ها و تنظیمات پنل از بین نمی‌رود؛ چون مسیر اصلی ذخیره‌سازی خارج از Git است.

## چک کردن کد

```bash
python -m py_compile \
  vpnshop/settings.py \
  sales/urls.py \
  sales/services/provisioning.py \
  sales/services/xui.py \
  sales/management/commands/bot.py \
  sales/management/commands/runtime_prepare.py

python manage.py check
python manage.py migrate
python manage.py collectstatic --noinput
```

## اجرای درست با PM2

اگر سرویس‌های قبلی مشکل دارند، حذفشان کن:

```bash
pm2 delete vpn-panel || true
pm2 delete vpn-bot || true
```

سپس با فایل آماده PM2 اجرا کن:

```bash
pm2 start ecosystem.config.js
pm2 save
```

وضعیت:

```bash
pm2 status
pm2 logs vpn-panel
pm2 logs vpn-bot
```

## نکته مهم درباره .env

از این به بعد فایل اصلی تنظیمات سرور این است:

```text
/var/lib/vpnshop/.env
```

برای مثال اگر IP سرورت این است، داخل همین فایل بگذار:

```env
ALLOWED_HOSTS=127.0.0.1,localhost,185.73.113.162
PUBLIC_BASE_URL=http://185.73.113.162:8000
DEBUG=1
```

بعد ری‌استارت:

```bash
pm2 restart vpn-panel vpn-bot
```
