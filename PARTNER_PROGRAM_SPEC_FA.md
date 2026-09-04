# سیستم همکاری در فروش — سند فنی پیاده‌سازی

این سند طراحی «قسمت همکاری» را روی کد فعلی `vpn-v2ray-sales-bot` مشخص می‌کند.
اصل حاکم بر کل سند، بند ۱۵ خواسته است: **سیستم همکاری یک سیستم موازی نیست.**
همکار از همان `Order`، همان `Payment`، همان `provision_order` و همان
`Subscription/Config` استفاده می‌کند و فقط چند فیلد و چند مدل جانبی اضافه می‌شود.
هر جا این اصل نقض شود، تمدید و حذف و مشاهده و sweep حجم که امروز کار می‌کنند،
فردا برای همکار کار نخواهند کرد.

---

## ۰. تصمیم‌های قطعی

| # | موضوع | تصمیم |
|---|---|---|
| ۱ | مالک سفارش در دیتابیس | `Order.user` همان همکار می‌ماند + فیلدهای `partner` و `customer_label` |
| ۲ | معنی «بدهی» که سفارش را قفل می‌کند | فقط فاکتور **معوق** (سررسید گذشته و پرداخت‌نشده). فاکتور باز در طول دوره مانع سفارش نیست |
| ۳ | لایه‌های قیمت | سه لایه: قیمت همکاری پیش‌فرض روی پلن ← قیمت اختصاصی همکار روی پلن ← درصد تخفیف همکار |
| ۴ | حذف کانفیگ | حذف واقعی از 3x-ui + پنجره برگشت وجه (پیش‌فرض ۲۴ ساعت، فقط تا وقتی فاکتور پرداخت نشده) |
| ۵ | شروع دوره بعدی | از **اولین سفارش بعدی**. فاکتور خالی هرگز ساخته نمی‌شود |
| ۶ | درآمد | سفارش اعتباری تا پرداخت فاکتور درآمد نیست؛ در ستون جدا «طلب از همکاران» دیده می‌شود |
| ۷ | روش پرداخت فاکتور | کارت‌به‌کارت، OxaPay، کیف پول همکار، تسویه دستی از Django — هر چهار مورد |
| ۸ | پلن‌های مجاز | قابل تعیین برای هر همکار؛ خالی گذاشتن یعنی همه |

---

## ۱. وضعیت فعلی کد — چه چیزی هست و چه چیزی نیست

### هست و بازاستفاده می‌شود

| قابلیت | محل |
|---|---|
| ساخت کلاینت روی چند اینباند، خواندن `sub_id` واقعی از پنل | `sales/services/provisioning.py:174` `provision_order` |
| تمدید با افزودن به انقضای باقی‌مانده + ریست حجم | `sales/services/provisioning.py:211` `renew_order_from_wallet` |
| اعتبارسنجی و یکتایی نام کانفیگ | `sales/services/clientname.py` |
| کارت‌به‌کارت با تایید دستی و تایید خودکار پیامکی | `CardPaymentRequest` + `sales/services/cardpay.py` + `banksms.py` |
| OxaPay | `sales/services/oxapay.py` + `Payment` |
| کیف پول | `TelegramUser.wallet_balance_toman` + `WalletTransaction` |
| ارسال مجدد کانفیگ | `botcore.py` callback `resend:` |
| sweep حجم مصرفی از پنل | `sales/services/lifecycle.py` |
| ورکرهای پس‌زمینه (thread + sleep) | `botcore.py:803` `start_background_workers` |
| گزارش فروش | `sales/services/reports.py` |
| فیلد `comment` در payload نسخه v3 پنل | `sales/services/xui.py:624` |

### نیست و باید ساخته شود

| کمبود | اثر |
|---|---|
| **`xui.py` هیچ متد `delete_client` ندارد** | بند «حذف کانفیگ» بدون آن ممکن نیست |
| **`xui.py` هیچ متد enable/disable ندارد** | بند ۹ و ۱۰ (`Active → Disabled بسبب بدهی`) بدون آن ممکن نیست |
| `build_client_payload` فیلد `comment` را پر نمی‌کند | بند ۱۲ (ثبت نام همکار در Comment پنل) |
| `Payment.Purpose` فقط `wallet_topup` و `direct_order` دارد | پرداخت فاکتور همکار جا ندارد |
| `reports.py` `EARNED_STATES = [PAID, PROVISIONED]` را درآمد می‌شمارد | سفارش اعتباری پرداخت‌نشده درآمد را متورم می‌کند |
| `Order.is_active` فقط انقضا و حجم را می‌بیند | کانفیگ غیرفعال‌شده به‌خاطر بدهی همچنان «فعال» گزارش می‌شود |

---

## ۲. مدل‌های جدید

همه در `sales/models.py`، ذیل `TimeStampedModel`، با `verbose_name` فارسی مطابق سبک فعلی فایل.

### ۲.۱ `Partner`

```
user                OneToOneField(TelegramUser, related_name='partner')
display_name        CharField(120)              نام نمایشی همکار، همان که در Comment پنل می‌رود
is_active           BooleanField(default=True)  غیرفعال = /work فقط پیام «دسترسی ندارید»
billing_mode        CharField(choices=BillingMode)   CREDIT='credit' | PREPAID='prepaid'
billing_cycle_days  PositiveSmallIntegerField(default=7)
credit_limit_toman  DecimalField(18,0, default=0)    ۰ = نامحدود
discount_percent    PositiveSmallIntegerField(default=0)   ۰ تا ۱۰۰
allowed_services    M2M(Service, blank=True)
allowed_plans       M2M(Plan, blank=True)
note                TextField(blank=True)
```

**قانون پلن‌های مجاز** (متد `available_plans()`):

1. اگر `allowed_plans` خالی نباشد ← همان‌ها (فقط `is_active=True`).
2. وگرنه اگر `allowed_services` خالی نباشد ← همه پلن‌های فعال آن سرویس‌ها.
3. وگرنه ← همه پلن‌های فعال.

دو فیلد به‌جای یکی، چون در عمل مدیر معمولاً می‌خواهد بگوید «این همکار فقط سرویس
آلمان را بفروشد»، نه اینکه ۵ پلن را تک‌تک تیک بزند. `allowed_plans` برای وقتی است
که واقعاً استثنای پلنی لازم است، و چون دقیق‌تر است بر سرویس مقدم است.

**متدهای لازم:**

- `open_invoice()` — فاکتور با وضعیت `OPEN` یا `OVERDUE`، یا `None`.
- `overdue_invoice()` — فاکتور `OVERDUE`، یا `None`. **این تنها چیزی است که سفارش را قفل می‌کند.**
- `current_debt_toman()` — جمع `total_toman` فاکتورهای `OPEN` و `OVERDUE`.
- `remaining_credit_toman()` — اگر `credit_limit_toman == 0` بی‌نهایت، وگرنه `credit_limit - current_debt`.
- `price_for(plan)` — طبق بخش ۳.

### ۲.۲ `PartnerPlanPrice`

```
partner       FK(Partner, related_name='plan_prices')
plan          FK(Plan,    related_name='partner_prices')
price_toman   DecimalField(18,0)
price_usd     DecimalField(10,2, default=0)   ۰ یعنی از نرخ دلار سایت حساب شود
unique_together = ('partner', 'plan')
```

### ۲.۳ `PartnerInvoice`

```
partner        FK(Partner, related_name='invoices')
number         CharField(32, unique=True)      مثل INV-1404-000137
status         CharField(choices=Status)       OPEN | OVERDUE | PAID | CANCELLED
opened_at      DateTimeField                   لحظه اولین سفارش دوره
due_at         DateTimeField                   opened_at + partner.billing_cycle_days
total_toman    DecimalField(18,0, default=0)   جمع آیتم‌های غیرلغوشده، هر بار محاسبه و ذخیره
total_usd      DecimalField(12,2, default=0)
warned_at      DateTimeField(null=True)        هشدار ۲۴ ساعته — پر بودن یعنی رفته، تضمین یک‌بار
suspended_at   DateTimeField(null=True)        لحظه غیرفعال شدن کانفیگ‌ها
paid_at        DateTimeField(null=True)
payment        FK(Payment, null=True, SET_NULL)
card_request   FK(CardPaymentRequest, null=True, SET_NULL)
settled_by     CharField(20)                   wallet | card | oxapay | admin
admin_note     TextField(blank=True)
```

`OPEN → OVERDUE` را ورکر انجام می‌دهد وقتی `due_at` گذشت.
`OVERDUE → PAID` را پرداخت انجام می‌دهد.
`CANCELLED` فقط دست مدیر است (مثلاً بخشش بدهی).

**هیچ فاکتوری بدون آیتم ساخته نمی‌شود.** فاکتور دقیقاً در لحظه‌ای زاده می‌شود که
اولین سفارش دوره ثبت شود — این همان چیزی است که تصمیم ۵ می‌خواهد.

### ۲.۴ `PartnerInvoiceItem`

```
invoice        FK(PartnerInvoice, related_name='items')
order          FK(Order, null=True, SET_NULL)
kind           CharField(choices)   NEW='new' | RENEW='renew' | ADJUST='adjust'
title          CharField(200)       متن خوانا: «آلمان / یک‌ماهه ۵۰ گیگ — مشتری: رضا»
amount_toman   DecimalField(18,0)
amount_usd     DecimalField(10,2, default=0)
is_cancelled   BooleanField(default=False)
cancelled_at   DateTimeField(null=True)
cancel_reason  CharField(200, blank=True)
```

**چرا آیتم جدا و نه فقط `Order.invoice`؟** چون تمدید در کد فعلی سفارش جدید نمی‌سازد؛
`renew_order_from_wallet` همان `Order` را جابه‌جا می‌کند. اگر ردیف فاکتور را روی
`Order` سوار کنیم، دو تمدید در یک دوره فقط یک ردیف می‌شوند و پول یکی از آن‌ها گم
می‌شود. آیتم جدا این را حل می‌کند و ضمناً برای «حذف با برگشت وجه» جای درستی
می‌دهد که ردیف را لغو کنیم بدون اینکه سفارش نابود شود.

**مبلغ در آیتم snapshot است.** اگر قیمت پلن وسط دوره عوض شود، فاکتور باز دست
نمی‌خورد. این عمداً است.

### ۲.۵ `PartnerRequest`

```
user             FK(TelegramUser, related_name='partner_requests')
full_name        CharField(120)
phone            CharField(20)
sales_channel    CharField(200)    کانال/گروه/سایت/حضوری
monthly_volume   CharField(100)    «حدوداً چند کاربر در ماه» — متن آزاد
note             TextField(blank=True)
status           CharField          PENDING | APPROVED | REJECTED
reviewed_at      DateTimeField(null=True)
admin_note       TextField(blank=True)
```

اکشن ادمین «✅ تایید و ساخت همکار» یک `Partner` با مقادیر پیش‌فرض
(`billing_mode=PREPAID`، `cycle=SiteSetting.partner_default_cycle_days`،
`credit_limit=0`، `discount_percent=0`) می‌سازد و به همکار در تلگرام خبر می‌دهد.
پیش‌فرض عمداً «پرداخت فوری» است: اعتبار دادن باید یک تصمیم آگاهانه باشد، نه پیش‌فرض.

---

## ۳. فیلدهای اضافه به مدل‌های موجود

### `Order`

```
partner              FK(Partner, null=True, blank=True, SET_NULL, related_name='orders')
customer_label       CharField(120, blank=True)   نام مشتریِ همکار (بند ۱۳)
partner_base_toman   DecimalField(18,0, default=0)  قیمت عادی در لحظه فروش، برای گزارش حاشیه
revenue_at           DateTimeField(null=True, db_index=True)   لحظه‌ای که پول واقعاً درآمد شد
suspended_at         DateTimeField(null=True)
suspended_by_invoice FK(PartnerInvoice, null=True, SET_NULL, related_name='suspended_orders')
```

`Order.Source` یک گزینه می‌گیرد:

```
PARTNER_CREDIT = 'partner_credit', 'اعتباری همکار'
```

`Order.is_active` باید `suspended_at` را هم ببیند:

```python
@property
def is_active(self) -> bool:
    if self.status != self.Status.PROVISIONED or self.traffic_ended_at is not None:
        return False
    if self.suspended_at is not None:
        return False
    return self.expires_at is None or self.expires_at > timezone.now()
```

### `Payment`

```
Purpose.PARTNER_INVOICE = 'partner_invoice', 'پرداخت فاکتور همکاری'
partner_invoice = FK(PartnerInvoice, null=True, blank=True, SET_NULL)
```

### `CardPaymentRequest`

```
partner_invoice = FK(PartnerInvoice, null=True, blank=True, SET_NULL)
```

### `SiteSetting`

```
partner_program_enabled       BooleanField(default=False)
partner_request_enabled       BooleanField(default=True)
partner_default_cycle_days    PositiveSmallIntegerField(default=7)
partner_invoice_warn_hours    PositiveSmallIntegerField(default=24)
partner_delete_refund_hours   PositiveSmallIntegerField(default=24)
partner_request_intro_text    TextField
partner_panel_welcome_text    TextField
```

---

## ۴. `revenue_at` — چرا و چطور

امروز `reports.py` درآمد را از سفارش‌های `PAID/PROVISIONED` در بازه `created_at`
حساب می‌کند. سفارش اعتباری همکار در همان لحظه `PROVISIONED` می‌شود ولی هیچ پولی
نیامده. اگر دست نزنیم، گزارش روزانه دروغ می‌گوید.

**راه‌حل:** یک فیلد `Order.revenue_at` که «لحظه‌ای که پول شهر شد» را نگه می‌دارد.

- سفارش عادی (کیف پول / کارت / OxaPay): `revenue_at = created_at` در همان لحظه.
- سفارش اعتباری همکار: `revenue_at = None` تا وقتی فاکتورش پرداخت شود؛ آن‌وقت
  همه سفارش‌های آن فاکتور `revenue_at = invoice.paid_at` می‌گیرند.
- سفارش همکارِ پرداخت‌فوری: مثل سفارش عادی، چون پول همان‌جا می‌آید.

**مهاجرت:** یک data migration که برای همه سفارش‌های موجود با وضعیت `PAID` یا
`PROVISIONED` مقدار `revenue_at = created_at` بگذارد. بعد از این، گزارش‌های
تاریخی عیناً همان عددهای قبلی را می‌دهند.

**تغییر در `reports.py`:** فیلتر بازه از `created_at__range` به
`revenue_at__range` عوض می‌شود و شرط `revenue_at__isnull=False` اضافه می‌شود.
`EARNED_STATES` سر جایش می‌ماند.

**بلوک جدید در گزارش:**

```
طلب از همکاران:      جمع total_toman فاکتورهای OPEN + OVERDUE
از این مقدار معوق:    جمع total_toman فاکتورهای OVERDUE
فروش همکاران (تسویه‌شده):  درآمد سفارش‌هایی که partner دارند
حاشیه:               جمع (partner_base_toman - amount_toman)
```

---

## ۵. قیمت‌گذاری سه‌لایه

`Plan` یک فیلد می‌گیرد:

```
partner_price_toman  DecimalField(18,0, default=0)   ۰ یعنی تعریف نشده
partner_price_usd    DecimalField(10,2, default=0)
```

`Partner.price_for(plan)` این ترتیب را طی می‌کند:

```
۱) PartnerPlanPrice(partner, plan).price_toman        اختصاصی همکار
۲) وگرنه Plan.partner_price_toman   اگر > 0            پیش‌فرض همکاری
۳) وگرنه Plan.price_toman                              قیمت عادی

سپس:
final = base × (100 − partner.discount_percent) ÷ 100
final = رند به پایین تا نزدیک‌ترین ۱۰۰۰ تومان
```

مثال بند ۳ سند: `base = 350_000`، `discount = 10` ← `final = 315_000`. ✅

**دلاری (برای OxaPay):** به همان ترتیب `price_usd`ها؛ اگر همه صفر بودند،
`final_usd = final_toman ÷ SiteSetting.dollar_rate_toman` گرد شده به دو رقم.

**رند به پایین** انتخابی است: بالا رند کردن یعنی از همکار بیشتر از عدد توافقی
گرفتن، و آن مکالمه‌ای است که ارزشش را ندارد.

خروجی یک dataclass به سبک `DiscountQuote` موجود:

```python
@dataclass
class PartnerQuote:
    plan: Plan
    base_toman: Decimal      # قیمت عادی، برای نمایش خط‌خورده و گزارش حاشیه
    partner_toman: Decimal   # قیمت همکاری قبل از درصد
    final_toman: Decimal
    final_usd: Decimal
    off_percent: int
```

جای فایل: `sales/services/partner_pricing.py`.

---

## ۶. جریان‌ها

### ۶.۱ `/work` — ورود به پنل

```
/work
 ├── SiteSetting.partner_program_enabled = False → پیام «این بخش فعال نیست»
 ├── Partner وجود دارد و is_active → پنل همکار
 ├── Partner وجود دارد و is_active = False → «حساب همکاری شما غیرفعال است، با پشتیبانی تماس بگیرید»
 ├── PartnerRequest با status=PENDING دارد → «درخواست شما در حال بررسی است»
 └── هیچ‌کدام → دکمه «📝 درخواست همکاری»
```

`/work` به `publish_commands` **اضافه نمی‌شود** — منوی دستورات تلگرام برای همه
کاربران یکسان است و نمایش `/work` به مشتری عادی فقط سؤال می‌سازد. امنیت هم به
مخفی بودن دستور وابسته نیست (بند ۱)؛ گارد روی `Chat ID` است و در هر
callback همکاری دوباره چک می‌شود، نه فقط در لحظه ورود.

**گارد مشترک:** یک دکوراتور یا تابع `require_partner(call) -> Partner | None` که
در ابتدای هر هندلر با پیشوند `pw:` صدا زده شود. بدون این، یک کاربر عادی که
`callback_data` را حدس بزند وارد می‌شود.

### ۶.۲ سفارش جدید — همکار اعتباری

ترتیب گاردها مهم است؛ گران‌ترین چک آخر باشد:

```
۱. partner.is_active                    → وگرنه رد
۲. SiteSetting.is_shop_active           → وگرنه رد
۳. partner.overdue_invoice() is None    → وگرنه «فاکتور معوق دارید» + دکمه پرداخت
۴. plan در partner.available_plans()    → وگرنه رد
۵. quote = partner.price_for(plan)
۶. سقف اعتبار: credit_limit == 0  یا  current_debt + quote.final ≤ credit_limit
                                         → وگرنه «از سقف اعتبار عبور می‌کند»
۷. نام مشتری (بند ۱۳): رندوم یا دستی، از clientname.resolve()
۸. ساخت Order + آیتم فاکتور + provision
```

گام ۶ و ۸ باید در یک `transaction.atomic` با
`Partner.objects.select_for_update()` باشند، وگرنه دو سفارش هم‌زمان هر دو
سقف را چک می‌کنند و هر دو رد نمی‌شوند.

**ساخت سفارش** — تابع جدید `create_partner_order()` در `provisioning.py`،
هم‌خانواده با `create_order_from_wallet`:

```python
Order.objects.create(
    user=partner.user,              # بند ۱ — مالک همان همکار است
    service=plan.service,
    plan=plan,
    partner=partner,
    customer_label=customer_name,
    source=Order.Source.PARTNER_CREDIT,
    status=Order.Status.PAID,       # تا provision_order قبولش کند
    amount_toman=quote.final_toman,
    amount_usd=quote.final_usd,
    partner_base_toman=quote.base_toman,
    revenue_at=None,                # ← هنوز پولی نیامده
    traffic_bytes=gb_to_bytes(plan.traffic_gb),
    user_limit=plan.user_limit,
    xui_client_email=client_name,
)
```

سپس فاکتور باز را می‌گیرد یا می‌سازد، آیتم `NEW` اضافه می‌کند، `total` را
به‌روز می‌کند، و بعد `provision_order(order)` را صدا می‌زند.

**اگر provisioning شکست خورد:** سفارش `FAILED` می‌شود و آیتم فاکتور همان لحظه
`is_cancelled=True` با دلیل «ساخت کانفیگ ناموفق». همکار نباید بابت کانفیگی که
ساخته نشد پول بدهد.

**باز کردن فاکتور (`get_or_open_invoice`)**:

```python
inv = partner.open_invoice()
if inv is None:
    now = timezone.now()
    inv = PartnerInvoice.objects.create(
        partner=partner,
        number=next_invoice_number(),
        status=PartnerInvoice.Status.OPEN,
        opened_at=now,
        due_at=now + timedelta(days=partner.billing_cycle_days),
    )
```

دقیقاً بند ۴: دوره از **اولین سفارش** شروع می‌شود و هر سفارش داخل بازه به همان
فاکتور می‌چسبد. سفارش‌های ۱۰، ۱۲ و ۱۵ شهریور سه فاکتور نمی‌شوند.

### ۶.۳ سفارش جدید — همکار پرداخت فوری

هیچ فاکتوری در کار نیست. عیناً مسیر مشتری عادی، فقط با قیمت همکاری:

```
انتخاب پلن → نام مشتری → انتخاب روش پرداخت (کیف پول / کارت / OxaPay)
          → پرداخت موفق → provision_order → تحویل
```

کد فعلی از قبل این را رعایت می‌کند: در کارت و OxaPay کانفیگ فقط بعد از تایید
پرداخت ساخته می‌شود. برای کیف پول هم `create_order_from_wallet` اول کسر می‌کند.
پس بند ۵ خواسته، با بازاستفاده تأمین می‌شود و کد جدیدی نمی‌خواهد جز پاس دادن
`partner` و `customer_label` و قیمت همکاری به همان توابع.

`revenue_at` این سفارش‌ها در همان لحظه پر می‌شود.

### ۶.۴ تمدید توسط همکار

- همکار اعتباری: `renew_partner_order()` که مثل `renew_order_from_wallet` عمل
  می‌کند ولی به‌جای کسر از کیف پول، یک آیتم `RENEW` به فاکتور باز اضافه می‌کند
  (و در صورت نبود فاکتور باز، فاکتور جدید باز می‌کند — یعنی تمدید هم می‌تواند
  شروع‌کننده دوره باشد).
- همکار پرداخت فوری: همان `renew_order_from_wallet` با قیمت همکاری.
- گاردهای ۱ تا ۶ بخش ۶.۲ اینجا هم اجرا می‌شوند.

### ۶.۵ حذف کانفیگ + پنجره برگشت وجه

```
همکار → «حذف کانفیگ» → تأیید دوباره («این کار برگشت‌پذیر نیست»)
    ├── xui.delete_client(order.xui_client_email)
    ├── order.status = CANCELLED
    ├── item = آیتم فاکتوریِ این سفارش
    └── اگر  item موجود و not item.is_cancelled
             و item.invoice.status in (OPEN, OVERDUE)      ← فاکتور هنوز پرداخت نشده
             و now − item.created_at ≤ partner_delete_refund_hours
        → item.is_cancelled = True، total فاکتور دوباره حساب شود
        → پیام: «کانفیگ حذف شد و مبلغ از فاکتور برداشته شد»
        وگرنه
        → پیام: «کانفیگ حذف شد. مهلت برگشت وجه گذشته و مبلغ در فاکتور می‌ماند»
```

اگر بعد از لغو آیتم، فاکتور هیچ آیتم فعالی نداشت، فاکتور `CANCELLED` می‌شود و
دوره بسته می‌شود — همکار نباید با یک فاکتور صفرتومانیِ باز گیر کند.

پنجره زمانی از **زمان ساخت آیتم** حساب می‌شود نه از ابتدای دوره، چون هدف پوشش
اشتباه همکار در لحظه ثبت است.

### ۶.۶ هشدار ۲۴ ساعته (بند ۸)

ورکر جدید در `botcore.py`، کنار `watch_finished_services`، با فاصله ۵ دقیقه:

```python
def watch_partner_invoices(bot):
    while True:
        try:
            partner_billing.warn_due_soon(bot)   # هشدار ۲۴ ساعته
            partner_billing.mark_overdue(bot)    # OPEN → OVERDUE + غیرفعال‌سازی
        except Exception:
            pass
        time.sleep(300)
```

`warn_due_soon`:

```python
threshold = now + timedelta(hours=site.partner_invoice_warn_hours)
qs = PartnerInvoice.objects.filter(
    status=OPEN, warned_at__isnull=True, due_at__lte=threshold, due_at__gt=now,
)
```

`warned_at` بلافاصله و در همان تراکنش پر می‌شود، **قبل** از ارسال پیام، تا اگر
دو ورکر هم‌زمان بالا آمدند (حالت webhook با چند worker) پیام دو بار نرود.
`warned_at__isnull=True` تضمین «فقط یک بار» بند ۸ است.

متن پیام دقیقاً همان چیزی است که خواسته شده، با تاریخ جلالی از
`sales/services/jalali.py`:

> ⏰ فاکتور شماره INV-… به مبلغ ۳٬۱۵۰٬۰۰۰ تومان
> در تاریخ ۱۷ شهریور ساعت ۱۲:۰۰ سررسید می‌شود.
> ۲۴ ساعت فرصت دارید فاکتور را پرداخت کنید.
> در صورت عدم پرداخت، کانفیگ‌های مربوط به این فاکتور غیرفعال خواهند شد.

### ۶.۷ سررسید و غیرفعال‌سازی (بند ۹)

```python
def mark_overdue(bot):
    for inv in PartnerInvoice.objects.filter(status=OPEN, due_at__lte=now):
        inv.status = OVERDUE
        inv.suspended_at = now
        inv.save()
        for order in orders_of(inv):
            if order.status != PROVISIONED:   continue   # قبلاً حذف/لغو شده
            if order.suspended_at is not None: continue   # قبلاً غیرفعال
            xui.set_client_enabled(order, False)
            order.suspended_at = now
            order.suspended_by_invoice = inv
            order.save(update_fields=[...])
```

**کانفیگ حذف نمی‌شود.** `xui_client_uuid`، `xui_client_email`، `xui_sub_id`،
`expires_at` و لینک‌ها همه سر جایشان می‌مانند. فقط `enable=false` در پنل
و `suspended_at` در دیتابیس. این دقیقاً بند ۹ است.

`orders_of(inv)` یعنی سفارش‌های آیتم‌های غیرلغوشده آن فاکتور. تمدیدها روی همان
سفارش قبلی هستند، پس یک سفارش می‌تواند از دو فاکتور بیاید — اولین فاکتوری که
غیرفعالش می‌کند، `suspended_by_invoice` را مالک می‌شود و دومی از رویش رد می‌شود.

### ۶.۸ پرداخت فاکتور و فعال‌سازی مجدد (بند ۱۰)

چهار مسیر ورودی، یک تابع خروجی مشترک `settle_invoice(invoice, *, via, payment=None)`:

| مسیر | چطور |
|---|---|
| کیف پول | کسر اتمیک از `wallet_balance_toman` + `WalletTransaction(DEBIT)` — مثل `create_order_from_wallet` |
| کارت‌به‌کارت | `CardPaymentRequest` با `partner_invoice` پر؛ اکشن تایید فعلی ادمین و تایید خودکار پیامکی هر دو به `settle_invoice` می‌رسند |
| OxaPay | `Payment` با `purpose=PARTNER_INVOICE` و `partner_invoice` پر؛ `oxapay_check` بعد از `PAID` شدن `settle_invoice` می‌زند |
| دستی | اکشن ادمین «💰 تسویه دستی فاکتور» |

`settle_invoice` — همه در یک تراکنش:

```python
inv.status  = PAID
inv.paid_at = now
inv.settled_by = via

for order in orders_of(inv):
    order.revenue_at = now                       # ← درآمد اینجا شناسایی می‌شود
    if order.suspended_by_invoice_id == inv.pk and order.suspended_at is not None:
        if (order.status == PROVISIONED
                and order.traffic_ended_at is None
                and (order.expires_at is None or order.expires_at > now)):
            xui.set_client_enabled(order, True)
        order.suspended_at = None
        order.suspended_by_invoice = None
    order.save()
```

سه شرط داخل `if` دقیقاً همان چیزی است که بند ۱۰ می‌خواهد: کانفیگی که مدیر دستی
در پنل خاموش کرده هرگز `suspended_by_invoice` نگرفته، پس دست نمی‌خورد؛ کانفیگ
منقضی یا تمام‌حجم هم به اشتباه زنده نمی‌شود — فقط پرچم `suspended_at` پاک
می‌شود تا حسابداری تمیز بماند.

دوره بعدی اینجا باز **نمی‌شود** (تصمیم ۵). اولین سفارش بعدی آن را باز می‌کند.

---

## ۷. متدهای جدید در `sales/services/xui.py`

هر سه باید سبک فعلی فایل را رعایت کنند: چند مسیر و چند payload کاندید امتحان
شود و اولین جوابِ موفق برگردد، چون نسخه‌های 3x-ui با هم فرق دارند.

### ۷.۱ `set_client_enabled(order, enabled: bool)`

`update_client` کل payload را جایگزین می‌کند، پس نمی‌شود فقط `{'enable': False}`
فرستاد — حجم و انقضا و limitIp پاک می‌شوند. راه درست: payload را از روی `Order`
دوباره بساز و فقط `enable` را عوض کن.

```python
payload = build_client_payload(order, order.xui_client_uuid,
                               order.xui_client_email, order.expires_at)
payload['enable'] = enabled
payload['comment'] = partner_comment(order)
XUIClient(order.service.panel).update_client(order.xui_client_email, payload)
```

ساخت payload از روی سفارش (نه از روی جواب پنل) عمدی است: دیتابیس منبع حقیقت
است و اگر کسی دستی در پنل حجم را دستکاری کرده باشد، فعال‌سازی مجدد آن را به
چیزی که فروخته شده برمی‌گرداند.

### ۷.۲ `delete_client(email)`

⚠️ **این متد باید روی پنل واقعی تست شود.** مسیر حذف در 3x-ui به `inbound_id` و
شناسه کلاینت وابسته است و بین نسخه‌ها فرق دارد. `client_inbound_ids(email)`
از قبل وجود دارد و اینباندها را می‌دهد. مسیرهای کاندید، به همان سبک
`_attach_client_to_inbound`:

```
POST  {api}/clients/del/{email}
POST  {api}/clients/delete/{email}
DELETE {api}/clients/{email}
POST  {panel}/inbounds/{inbound_id}/delClient/{client_uuid}
POST  {panel}/inbounds/delClient/{inbound_id}/{client_uuid}
```

اگر روی چند اینباند نشسته، روی همه حلقه بزن و موفقیت روی حداقل یکی را قبول کن،
ولی خطای بقیه را در متن خطا بیاور.

**قبل از پیاده‌سازی، مسیر واقعی را با `python manage.py xui_probe` پیدا کن** —
همان ابزاری که برای `add_client` استفاده شد. اگر پنل هدف مسیر حذف ندارد، تصمیم
۴ به «فقط غیرفعال‌سازی» تنزل می‌کند و باید به من بگویی.

### ۷.۳ پر کردن `comment` (بند ۱۲)

در `provisioning.build_client_payload` یک کلید اضافه شود:

```python
'comment': partner_comment(order),
```

```python
def partner_comment(order) -> str:
    parts = []
    if order.customer_label:
        parts.append(f'Customer: {order.customer_label}')
    if order.partner_id:
        parts.append(f'Partner: {order.partner.display_name}')
    return ' | '.join(parts)
```

خروجی دقیقاً `Customer: Reza | Partner: Ali` می‌شود. برای سفارش مشتری عادی رشته
خالی است و رفتار فعلی عوض نمی‌شود. `_client_v3_payloads` از قبل این کلید را
منتقل می‌کند (`xui.py:624`)، پس در سمت پنل کاری لازم نیست.

---

## ۸. پنل همکار در ربات

پیشوند همه callbackها `pw:` تا با فضای نام فعلی (`resend:`, `reneword:`,
`lusage:`) تداخل نکند. فایل جدید `sales/services/partner_bot.py` که در
`register_handlers` رجیستر می‌شود؛ `botcore.py` بزرگ‌تر از این نشود.

```
/work  → پنل همکار
│
├── 🛒 سفارش جدید            pw:new
│    → انتخاب سرویس  pw:svc:<id>
│    → انتخاب پلن     pw:plan:<id>        [نمایش: قیمت عادی خط‌خورده، قیمت شما]
│    → نام مشتری      pw:name:rand | pw:name:type
│    → تأیید نهایی    pw:confirm          [اعتباری: «به فاکتور جاری اضافه می‌شود»]
│                                          [فوری: انتخاب روش پرداخت]
├── 📦 کانفیگ‌های من          pw:list
│    → هر ردیف: نام مشتری، پلن، انقضا، وضعیت (فعال / ⛔ غیرفعال بابت بدهی / منقضی)
│    → pw:cfg:<order_id>
│         ├── 🔁 تمدید            pw:renew:<id>
│         ├── 📤 ارسال مجدد        pw:resend:<id>
│         └── 🗑 حذف               pw:del:<id> → pw:delok:<id>
├── 🧾 فاکتورها               pw:inv
│    → pw:invd:<id>  جزئیات: ردیف‌ها، جمع، سررسید جلالی، وضعیت
│         └── 💳 پرداخت فاکتور     pw:pay:<id>
│              ├── کیف پول         pw:payw:<id>
│              ├── کارت‌به‌کارت     pw:payc:<id>
│              └── OxaPay          pw:payo:<id>
├── 💰 بدهی و اعتبار           pw:debt
│    → بدهی جاری، از این مقدار معوق، سقف اعتبار، اعتبار باقی‌مانده
├── 📊 گزارش فروش              pw:report
│    → این ماه / ماه قبل / کل: تعداد سفارش، مبلغ، تفکیک پلن
└── 👤 اطلاعات حساب همکاری     pw:me
     → نام، نوع همکاری، دوره تسویه، درصد تخفیف، سقف اعتبار، تاریخ عضویت
```

**حالت چندمرحله‌ای سفارش** با همان الگوی فعلی: `TelegramUser.state` و
`temp_data` (مثل `pending_client_name` در `botcore.py:231`). کلید `temp_data`
را `partner_order` بگذار تا با `pending_discount` قاطی نشود.

**قابلیت‌های فعلی ربات دست نمی‌خورند.** همکار همچنان `/start` و منوی مشتری را
دارد و می‌تواند برای خودش هم خرید کند. سفارش شخصی‌اش `partner=None` می‌گیرد و
از فاکتور همکاری بیرون می‌ماند — گارد این است که فقط مسیر `pw:` فیلد `partner`
را پر می‌کند.

---

## ۹. Django Admin

| مدل | list_display | list_filter | اکشن‌ها |
|---|---|---|---|
| `Partner` | نام، chat_id، نوع همکاری، دوره، سقف، درصد، بدهی جاری، فعال | نوع، فعال | فعال‌سازی، غیرفعال‌سازی |
| `PartnerPlanPrice` | همکار، پلن، قیمت | همکار، سرویس | — |
| `PartnerInvoice` | شماره، همکار، جمع، سررسید (جلالی)، وضعیت، پرداخت | وضعیت، همکار | 💰 تسویه دستی، 🔓 فعال‌سازی مجدد کانفیگ‌ها، ❌ لغو فاکتور |
| `PartnerInvoiceItem` | inline زیر فاکتور | — | — |
| `PartnerRequest` | نام، موبایل، کانال، وضعیت، تاریخ | وضعیت | ✅ تایید و ساخت همکار، ❌ رد |

**افزوده‌ها به `OrderAdmin` موجود:** ستون `partner` و `customer_label` و
وضعیت تعلیق در `list_display`، فیلتر `partner`، و
`list_filter` روی `suspended_at__isnull`.

`TelegramUserAdmin` یک ستون «همکار؟» بگیرد تا از روی کاربر بشود سریع رسید.

بند ۱۴ خواسته با همین جدول کامل پوشش داده می‌شود: افزودن/حذف/فعال/غیرفعال همکار،
دیدن Chat ID، مهلت پرداخت، سقف اعتبار، درصد تخفیف، قیمت اختصاصی هر پلن،
درخواست‌های همکاری، فاکتورها، بدهی‌ها، فروش هر همکار، و کانفیگ‌های هر همکار
(از راه فیلتر `partner` در `OrderAdmin`).

---

## ۱۰. ترتیب پیاده‌سازی

هر فاز باید مستقلاً قابل تست و قابل rollback باشد.

**فاز ۱ — پایه داده ✅ انجام شد**
هر پنج مدل (`Partner`، `PartnerPlanPrice`، `PartnerInvoice`،
`PartnerInvoiceItem`، `PartnerRequest`) + فیلدهای `Order`، `Payment`،
`CardPaymentRequest`، `SiteSetting`، `Plan` + مهاجرت `revenue_at` با backfill.
ادمین همه این‌ها. هیچ تغییری در رفتار ربات. **قابل دیپلوی به‌تنهایی.**

> انحراف عمدی از طرح اولیه: `PartnerInvoice` و `PartnerInvoiceItem` که قرار بود
> در فاز ۵ ساخته شوند، به فاز ۱ آمدند. دلیل: `Payment.partner_invoice`،
> `CardPaymentRequest.partner_invoice` و `Order.suspended_by_invoice` به
> `PartnerInvoice` ارجاع می‌دهند و بدون آن نمی‌شد این فیلدها را در فاز ۱ اضافه
> کرد. جدول خالی هیچ رفتاری ندارد، ولی شکستن FKها بین دو فاز یعنی یک مهاجرت
> اضافه روی جدول `sales_order` که در SQLite بازسازی کامل جدول است.

فایل‌های فاز ۱: `sales/models.py`، `sales/admin.py`،
`sales/migrations/0017_…`، `sales/migrations/0018_backfill_revenue_at.py`،
`sales/tests/test_partner.py`.

**فاز ۲ تا ۶ ✅ انجام شد.** جزئیات انحراف‌ها در انتهای همین بخش.

**فاز ۲ — پنل خواندنی**
`/work`، تشخیص همکار، درخواست همکاری، «اطلاعات حساب»، «کانفیگ‌های من» (فقط
نمایش). هنوز نه سفارشی، نه فاکتوری.

**فاز ۳ — قیمت‌گذاری و سفارش پرداخت‌فوری**
`partner_pricing.py`، مسیر سفارش، `comment` در پنل. همکار `PREPAID` کامل کار
می‌کند. این فاز به‌تنهایی برای خیلی از همکارها کافی است.

**فاز ۴ — متدهای پنل**
`set_client_enabled` و `delete_client` + probe روی پنل واقعی + تمدید و حذف و
ارسال مجدد در پنل همکار.

**فاز ۵ — فاکتور و اعتبار**
`PartnerInvoice`، `PartnerInvoiceItem`، سفارش اعتباری، سقف اعتبار، قفل معوق،
صفحه فاکتورها و بدهی.

**فاز ۶ — ورکر و تسویه**
هشدار ۲۴ ساعته، `mark_overdue` و غیرفعال‌سازی، چهار مسیر `settle_invoice`،
فعال‌سازی مجدد، گزارش فروش همکار و بلوک «طلب از همکاران».

---

### انحراف‌های عمدی از طرح اولیه

**۱. پرداخت فوری از کیف پول انجام می‌شود، نه با کارت/کریپتوی مستقیم.**
همکار پرداخت‌فوری کیف پولش را با همان مسیرهای فعلی (کارت‌به‌کارت و OxaPay) شارژ
می‌کند و سفارش از کیف پول کسر می‌شود. بند ۵ رعایت می‌شود — کانفیگ فقط بعد از
حضور پول ساخته می‌شود — و در عوض `Payment` و `CardPaymentRequest` نیازی به
فیلدهای `partner` و `customer_label` پیدا نکردند.

**۲. پرداخت فاکتور هم از راه کیف پول تسویه می‌شود.**
کارت‌به‌کارت و OxaPay پول را طبق روال فعلی به کیف پول می‌ریزند و بلافاصله
`pay_invoice_from_wallet` صدا زده می‌شود. نتیجه: یک مسیر تسویه به‌جای سه تا، و
واریزی که بعد از تسویه شدن فاکتور برسد به‌جای گم شدن، اعتبار همکار می‌ماند.

**۳. درآمد سفارش اعتباری از ردیف فاکتور خوانده می‌شود، نه از `Order.revenue_at`.**
`revenue_at` برای سفارش اعتباری هرگز پر نمی‌شود. دلیل: تمدید سفارش جدید نمی‌سازد
و `amount_toman` سفارش را بازنویسی می‌کند، پس اگر درآمد را از سفارش می‌خواندیم،
دو تمدید در یک دوره به یک عدد تبدیل می‌شد و مبلغ یکی گم می‌شد. سه منبع مجزا:
سفارش مستقیم و سفارش همکارِ پرداخت‌فوری از `revenue_at`، و سفارش اعتباری از
`PartnerInvoiceItem` فاکتورهای پرداخت‌شده.

**۴. نام مشتری و نام کانفیگ در یک مرحله پرسیده می‌شوند.**
همکار یک بار «نام مشتری» را می‌فرستد. اگر آن نام قواعد نام کانفیگ را داشته باشد و
آزاد باشد، همان می‌شود نام کانفیگ؛ وگرنه نام تایپ‌شده برچسب مشتری می‌ماند و نام
کانفیگ خودکار ساخته می‌شود. جلوی بن‌بست ناشی از قواعدی را می‌گیرد که همکار
ندیده بود.

**۵. `/work` در منوی دستورات تلگرام ثبت نمی‌شود.**
آن منو برای همه کاربران یکسان است و نمایش `/work` به مشتری عادی فقط سؤال می‌سازد.
امنیت هم به آن وابسته نیست: هر callback با پیشوند `pw:` دوباره Chat ID را
بررسی می‌کند.

---

## ۱۱. تست‌ها

فایل‌های تست کنار الگوی فعلی پروژه. موارد بحرانی:

**قیمت**
- سه لایه به ترتیب درست اعمال می‌شوند؛ نبود لایه اختصاصی به پیش‌فرض و بعد به قیمت عادی می‌افتد.
- `350_000` با `10%` می‌شود `315_000`.
- `discount_percent = 0` قیمت را دست نمی‌زند.

**اعتبار**
- `credit_limit = 0` هرگز مانع سفارش نمی‌شود.
- سفارشی که دقیقاً روی سقف بنشیند، قبول می‌شود؛ یک تومان بالاتر، رد.
- **دو سفارش هم‌زمان:** با `select_for_update` فقط یکی رد شود، نه هر دو قبول.

**فاکتور**
- سه سفارش در بازه دوره ← یک فاکتور با سه آیتم، نه سه فاکتور.
- سفارش بعد از `due_at` روی فاکتور معوق ← رد، نه فاکتور جدید.
- بعد از پرداخت، سفارش بعدی فاکتور تازه با `opened_at` برابر لحظه همان سفارش باز می‌کند.
- فاکتور خالی هرگز ساخته نمی‌شود.

**قفل بدهی**
- فاکتور `OPEN` که هنوز سررسید نشده، سفارش را قفل نمی‌کند. ← تناقض بند ۴ و ۷، حل‌شده.
- فاکتور `OVERDUE` سفارش را قفل می‌کند ولی ورود به پنل، دیدن فاکتور، دیدن بدهی، پرداخت و مدیریت کانفیگ‌های قبلی را قفل نمی‌کند (بند ۷).

**هشدار**
- در بازه ۲۴ ساعته دقیقاً یک بار می‌رود، حتی اگر ورکر ۱۰ بار اجرا شود.
- فاکتوری که همان روز پرداخت شد، هشدار نمی‌گیرد.

**تعلیق و فعال‌سازی مجدد**
- سررسید ← `enable=false` در پنل، سفارش حذف نمی‌شود، `uuid`/`sub_id`/`expires_at` دست‌نخورده.
- پرداخت ← فقط سفارش‌هایی که `suspended_by_invoice == این فاکتور` زنده می‌شوند.
- **کانفیگی که مدیر دستی در پنل خاموش کرده، با پرداخت فاکتور روشن نمی‌شود.**
- **کانفیگ منقضی‌شده یا تمام‌حجم، با پرداخت فاکتور روشن نمی‌شود.**

**حذف**
- داخل پنجره + فاکتور پرداخت‌نشده ← آیتم لغو، جمع کم می‌شود.
- بیرون پنجره ← آیتم می‌ماند.
- فاکتور پرداخت‌شده ← آیتم می‌ماند، حتی داخل پنجره.
- لغو آخرین آیتم ← فاکتور `CANCELLED` می‌شود.

**درآمد**
- سفارش اعتباری پرداخت‌نشده در گزارش درآمد نیست، در «طلب از همکاران» هست.
- بعد از پرداخت، در درآمدِ **روز پرداخت** می‌آید، نه روز سفارش.
- گزارش‌های تاریخی قبل از این تغییر، عدد یکسان می‌دهند.

---

## ۱۲. فرض‌هایی که خودم گذاشتم

این‌ها را نپرسیدم و پیش‌فرض معقول گذاشتم. اگر با کدام موافق نیستی بگو، قبل از کد
عوضشان می‌کنم:

1. **کد تخفیف عمومی روی سفارش همکاری کار نمی‌کند.** قیمت همکاری خودش تخفیف است و
   روی هم افتادن این دو باعث فروش زیر قیمت تمام‌شده می‌شود.
2. **قیمت دلاری اختصاصی اختیاری است**؛ اگر تعریف نشود از `dollar_rate_toman` سایت
   حساب می‌شود.
3. **تمدید با قیمت همکاری** انجام می‌شود و برای همکار اعتباری به فاکتور باز
   می‌چسبد.
4. **پنجره برگشت وجه ۲۴ ساعت** و در `SiteSetting` قابل تغییر است (نه per-partner).
5. **رند کردن به پایین** تا نزدیک‌ترین ۱۰۰۰ تومان.
6. **اعلان به مدیر** برای این رویدادها به `admin_chat_id` می‌رود: درخواست همکاری
   جدید، فاکتور معوق‌شده، فاکتور پرداخت‌شده، حذف کانفیگ توسط همکار.
7. **همکار غیرفعال با فاکتور باز:** فاکتور سر جایش می‌ماند و ورکر همان‌طور
   سررسیدش می‌کند و کانفیگ‌ها را می‌بندد؛ فقط سفارش جدید نمی‌تواند بدهد.
8. **همکار می‌تواند برای خودش هم مثل مشتری عادی خرید کند** و آن خرید وارد فاکتور
   همکاری نمی‌شود.

---

## ۱۳. ریسک‌های شناخته‌شده

| ریسک | کاهش |
|---|---|
| **مسیر `delete_client` در نسخه پنل هدف معلوم نیست** | قبل از فاز ۴ با `xui_probe` روی پنل واقعی پیدا شود. اگر نبود، تصمیم ۴ به «فقط غیرفعال‌سازی» تنزل می‌کند |
| دو سفارش هم‌زمان از سقف اعتبار رد شوند | `select_for_update` روی `Partner` در تراکنش سفارش |
| هشدار ۲۴ ساعته در حالت webhook با چند worker دو بار برود | `warned_at` قبل از ارسال و در همان تراکنش پر شود |
| `update_client` کل payload را جایگزین می‌کند و حجم/انقضا پاک شود | payload همیشه از روی `Order` بازسازی شود، نه patch جزئی |
| تغییر قیمت پلن وسط دوره، فاکتور باز را عوض کند | مبلغ در `PartnerInvoiceItem` snapshot است |
| تاریخ‌ها: `due_at` در UTC ذخیره، نمایش جلالی | ذخیره timezone-aware، نمایش با `sales/services/jalali.py` |
| `provision_order` بعد از ساخت آیتم فاکتور شکست بخورد | آیتم فوراً `is_cancelled` شود؛ در تست پوشش داده شود |

---

## ۱۴. نکته امنیتی خارج از موضوع

در `.git/config` این ریپو یک GitHub Personal Access Token زنده داخل آدرس
`origin` ذخیره شده است. هر کسی که به این پوشه دسترسی داشته باشد، دسترسی نوشتن
به ریپو دارد. پیشنهاد: توکن را در GitHub باطل کن و remote را بدون توکن تنظیم کن:

```
git remote set-url origin https://github.com/amirh55/vpn-v2ray-sales-bot.git
```

و اعتبارسنجی را به Git Credential Manager یا `gh auth login` بسپار.
