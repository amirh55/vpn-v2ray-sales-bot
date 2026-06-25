# آپدیت v0.1.5 - اصلاح دقیق 3x-ui API و ابزار عیب‌یابی

این آپدیت بر اساس API Docs پنل شما ساخته شده است.

## تغییرات

- حذف مسیر اشتباه `/panel/api/clients` از تلاش ساخت کلاینت.
- استفاده مستقیم از مسیر مستندشده `/panel/api/clients/add`.
- اضافه شدن fallback به `/panel/api/clients/bulkCreate`.
- نمایش خطای کامل‌تر شامل HTTP status، آدرس دقیق و خلاصه body پاسخ.
- اضافه شدن دستور عیب‌یابی:

```bash
python manage.py xui_probe
```

و برای تست ساخت واقعی کلاینت:

```bash
python manage.py xui_probe --create-test --inbound-id 16
```

> تست ساخت واقعی یک کلاینت آزمایشی در 3x-ui می‌سازد. بعداً می‌توانید از پنل حذفش کنید.

## نصب روی سرور

```bash
cd /root/vpn-v2ray-sales-bot
unzip -o vpn_v2ray_sales_bot_update_v0_1_5_3xui_docs_probe_fix.zip
source .venv/bin/activate
python -m py_compile sales/services/xui.py sales/management/commands/xui_probe.py
python manage.py check
pm2 restart vpn-bot vpn-panel
pm2 flush
```

## تست

```bash
python manage.py xui_probe
python manage.py xui_probe --create-test --inbound-id 16
```

اگر تست ساخت موفق شد، خرید از داخل ربات هم باید موفق شود.
