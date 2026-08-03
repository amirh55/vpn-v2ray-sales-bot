# آپدیت v0.2.0 - نصب یک‌دستوری، مسیر مخفی پنل و دستور مدیریتی

## چه چیزی عوض شد

۱. **نصب فقط با یک دستور**؛ دیگر لازم نیست venv بسازید، pip بزنید، migrate کنید و pm2 تنظیم کنید.

۲. **همیشه روشن ماندن**؛ به‌جای pm2 از systemd استفاده می‌شود. سرویس‌ها با `Restart=always` اجرا می‌شوند و بعد از ریبوت سرور هم خودکار بالا می‌آیند.

۳. **مسیر مخفی برای پنل**؛ پنل دیگر روی `/admin/` نیست و یک مسیر تصادفی ۲۰ کاراکتری می‌گیرد.

۴. **دستور `vpnshop`**؛ برای مدیریت سرور و مهم‌تر از همه، پیدا کردن دوباره آدرس پنل اگر آن را گم کردید.

---

## نصب روی سرور جدید

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/amirh55/vpn-v2ray-sales-bot/main/scripts/install.sh)
```

با دامنه و HTTPS رایگان:

```bash
DOMAIN=shop.example.com bash <(curl -fsSL https://raw.githubusercontent.com/amirh55/vpn-v2ray-sales-bot/main/scripts/install.sh)
```

در پایان، آدرس پنل و نام کاربری و رمز عبور نمایش داده می‌شود.

---

## اگر از قبل نصب pm2 دارید

نصب قدیمی معمولا در `/root/vpn-v2ray-sales-bot` است. دیتابیس و تنظیمات شما در `/var/lib/vpnshop` می‌ماند و دست‌نخورده باقی می‌ماند، پس اسکریپت نصب جدید همان داده‌ها را برمی‌دارد.

```bash
# ۱) توقف نسخه قدیمی
pm2 stop vpn-bot vpn-panel
pm2 delete vpn-bot vpn-panel
pm2 save

# ۲) نصب نسخه جدید
bash <(curl -fsSL https://raw.githubusercontent.com/amirh55/vpn-v2ray-sales-bot/main/scripts/install.sh)
```

چون کاربر مدیر از قبل وجود دارد، اسکریپت کاربر جدید نمی‌سازد و رمز قبلی شما معتبر می‌ماند. برای دیدن آدرس جدید پنل:

```bash
vpnshop info
```

---

## دستور مدیریتی

```bash
vpnshop info              # آدرس پنل، کاربران مدیر، وضعیت سرویس‌ها
vpnshop restart           # ری‌استارت پنل و ربات
vpnshop logs bot          # لاگ زنده ربات
vpnshop update            # دریافت آخرین نسخه و ری‌استارت
vpnshop newpath           # ساخت مسیر مخفی جدید
vpnshop passwd admin      # تغییر رمز مدیر
```

---

## فایل‌های تغییرکرده

- `scripts/install.sh` (جدید)
- `scripts/vpnshop` (جدید)
- `sales/management/commands/panelinfo.py` (جدید)
- `vpnshop/settings.py` — افزودن `ADMIN_PATH`
- `vpnshop/urls.py` — استفاده از مسیر مخفی
- `README_FA.md`

## محل فایل‌ها بعد از نصب

```text
/opt/vpnshop                 سورس پروژه
/var/lib/vpnshop             دیتابیس و فایل‌های آپلودی
/etc/vpnshop/vpnshop.env     تنظیمات و کلیدها (دسترسی 600)
/usr/local/bin/vpnshop       دستور مدیریتی
```
