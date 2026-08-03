#!/usr/bin/env bash
# نصب یک‌دستوری ربات فروش VPN + پنل مدیریت روی سرور
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/amirh55/vpn-v2ray-sales-bot/main/scripts/install.sh)
#
# متغیرهای اختیاری:
#   DOMAIN=shop.example.com   نصب Nginx و گرفتن گواهی HTTPS رایگان
#   PORT=8000                 پورت پنل
#   APP_DIR=/opt/vpnshop      محل نصب
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/amirh55/vpn-v2ray-sales-bot.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/vpnshop}"
RUNTIME_DIR="${RUNTIME_DIR:-/var/lib/vpnshop}"
CONF_DIR="${CONF_DIR:-/etc/vpnshop}"
ENV_FILE="$CONF_DIR/vpnshop.env"
CLI_CONF="$CONF_DIR/cli.conf"
PORT="${PORT:-8000}"
DOMAIN="${DOMAIN:-}"
WEB_SERVICE="vpnshop-web"
BOT_SERVICE="vpnshop-bot"

red() { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
step() { printf '\n\033[0;36m==> %s\033[0m\n' "$*"; }

die() { red "خطا: $*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "لطفا با کاربر root اجرا کنید (یا از sudo استفاده کنید)."

# تولید رشته تصادفی با پایتون، تا لوله tr|head زیر pipefail باعث خطا نشود
random_string() {
  python3 -c "import secrets,string;print(''.join(secrets.choice(string.ascii_lowercase+string.digits) for _ in range(${1:-20})))"
}

random_password() {
  python3 -c "import secrets,string;print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(16)))"
}

step "نصب پیش‌نیازهای سیستم"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip git curl >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 python3-pip git curl >/dev/null
elif command -v yum >/dev/null 2>&1; then
  yum install -y -q python3 python3-pip git curl >/dev/null
else
  die "پکیج‌منیجر پشتیبانی‌شده پیدا نشد. سیستم‌عامل باید Debian/Ubuntu یا CentOS/Rocky باشد."
fi
PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  red "نسخه پایتون سرور شما $PY_VERSION است، ولی حداقل 3.10 لازم است."
  red "سیستم‌عامل سرور را به اوبونتو 22.04 یا بالاتر ارتقا دهید."
  exit 1
fi
green "پیش‌نیازها نصب شد. (Python $PY_VERSION)"

step "دریافت سورس پروژه"
if [ -d "$APP_DIR/.git" ]; then
  git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
  (cd "$APP_DIR" && git fetch --all -q && git reset --hard -q "origin/$BRANCH")
  green "سورس موجود به‌روزرسانی شد: $APP_DIR"
else
  mkdir -p "$(dirname "$APP_DIR")"
  git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
  green "سورس دریافت شد: $APP_DIR"
fi

step "ساخت محیط پایتون و نصب کتابخانه‌ها"
[ -x "$APP_DIR/.venv/bin/python" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
green "کتابخانه‌ها نصب شد."

step "ساخت فایل تنظیمات"
mkdir -p "$CONF_DIR" "$RUNTIME_DIR"
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

env_set_default() {
  grep -q "^$1=" "$ENV_FILE" 2>/dev/null || printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
}

PUBLIC_IP="$(curl -fsS4 --max-time 8 https://api.ipify.org 2>/dev/null || true)"
[ -n "$PUBLIC_IP" ] || PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

if [ -n "$DOMAIN" ]; then
  DEFAULT_BASE_URL="https://$DOMAIN"
  DEFAULT_HOSTS="$DOMAIN,127.0.0.1,localhost"
  BIND_HOST="127.0.0.1"
elif [ -n "$PUBLIC_IP" ]; then
  DEFAULT_BASE_URL="http://$PUBLIC_IP:$PORT"
  DEFAULT_HOSTS="$PUBLIC_IP,127.0.0.1,localhost"
  BIND_HOST="0.0.0.0"
else
  DEFAULT_BASE_URL="http://127.0.0.1:$PORT"
  DEFAULT_HOSTS="*"
  BIND_HOST="0.0.0.0"
fi

env_set_default SECRET_KEY "$(random_string 50)"
env_set_default ADMIN_PATH "$(random_string 20)"
env_set_default DEBUG 0
env_set_default ALLOWED_HOSTS "$DEFAULT_HOSTS"
env_set_default PUBLIC_BASE_URL "$DEFAULT_BASE_URL"
env_set_default TIME_ZONE "Asia/Tehran"
green "تنظیمات در $ENV_FILE ذخیره شد."

cat > "$CLI_CONF" <<EOF
APP_DIR=$APP_DIR
RUNTIME_DIR=$RUNTIME_DIR
ENV_FILE=$ENV_FILE
EOF

run_manage() {
  (cd "$APP_DIR" && VPNSHOP_RUNTIME_DIR="$RUNTIME_DIR" PYTHONIOENCODING=utf-8 "$APP_DIR/.venv/bin/python" manage.py "$@")
}

step "آماده‌سازی دیتابیس و فایل‌های استاتیک"
run_manage migrate --noinput >/dev/null
run_manage collectstatic --noinput >/dev/null
green "دیتابیس و فایل‌های استاتیک آماده شد."

step "ساخت کاربر مدیر"
HAS_ADMIN="$(run_manage shell -c 'from django.contrib.auth import get_user_model; print(get_user_model().objects.filter(is_superuser=True).exists())' 2>/dev/null | tr -d '[:space:]' || true)"
ADMIN_USER=""
ADMIN_PASS=""
if [ "$HAS_ADMIN" = "True" ]; then
  yellow "کاربر مدیر از قبل وجود دارد؛ ساخت کاربر جدید انجام نشد."
else
  ADMIN_USER="admin"
  ADMIN_PASS="$(random_password)"
  DJANGO_SUPERUSER_USERNAME="$ADMIN_USER" \
  DJANGO_SUPERUSER_EMAIL="admin@localhost" \
  DJANGO_SUPERUSER_PASSWORD="$ADMIN_PASS" \
    run_manage createsuperuser --noinput >/dev/null
  green "کاربر مدیر ساخته شد."
fi

step "ساخت سرویس‌های systemd"
cat > "/etc/systemd/system/${WEB_SERVICE}.service" <<EOF
[Unit]
Description=VPN Shop Web Panel
After=network.target
# بدون این، systemd بعد از چند کرش پشت‌سرهم برای همیشه تسلیم می‌شود
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
Environment=VPNSHOP_RUNTIME_DIR=$RUNTIME_DIR
ExecStart=$APP_DIR/.venv/bin/gunicorn vpnshop.wsgi:application --bind $BIND_HOST:$PORT --workers 2 --timeout 120
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > "/etc/systemd/system/${BOT_SERVICE}.service" <<EOF
[Unit]
Description=VPN Shop Telegram Bot
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
Environment=VPNSHOP_RUNTIME_DIR=$RUNTIME_DIR
ExecStart=$APP_DIR/.venv/bin/python manage.py bot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$WEB_SERVICE" "$BOT_SERVICE" >/dev/null 2>&1
systemctl restart "$WEB_SERVICE" "$BOT_SERVICE"
green "سرویس‌ها ساخته و اجرا شدند (بعد از ری‌استارت سرور هم خودکار بالا می‌آیند)."

step "نصب دستور مدیریتی vpnshop"
install -m 755 "$APP_DIR/scripts/vpnshop" /usr/local/bin/vpnshop
green "دستور vpnshop نصب شد."

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  step "باز کردن پورت در فایروال"
  if [ -n "$DOMAIN" ]; then
    ufw allow 80/tcp >/dev/null 2>&1 || true
    ufw allow 443/tcp >/dev/null 2>&1 || true
  else
    ufw allow "$PORT/tcp" >/dev/null 2>&1 || true
  fi
  green "پورت‌های لازم در فایروال باز شد."
fi

if [ -n "$DOMAIN" ]; then
  step "نصب Nginx و گواهی HTTPS برای $DOMAIN"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get install -y -qq nginx certbot python3-certbot-nginx >/dev/null || yellow "نصب Nginx/Certbot ناموفق بود."
  else
    yellow "نصب خودکار Nginx فقط روی Debian/Ubuntu انجام می‌شود؛ این مرحله رد شد."
  fi

  if command -v nginx >/dev/null 2>&1; then
    cat > "/etc/nginx/conf.d/vpnshop.conf" <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
    rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
    nginx -t >/dev/null 2>&1 && systemctl restart nginx && green "Nginx تنظیم شد."
    if command -v certbot >/dev/null 2>&1; then
      certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect >/dev/null 2>&1 \
        && green "گواهی HTTPS نصب شد." \
        || yellow "گرفتن گواهی HTTPS ناموفق بود. مطمئن شوید دامنه به IP این سرور وصل است، سپس اجرا کنید: certbot --nginx -d $DOMAIN"
    fi
  fi
fi

step "نصب کامل شد"
echo ""
vpnshop info || true

if [ -n "$ADMIN_USER" ]; then
  echo ""
  green "=========================================================="
  green "  نام کاربری: $ADMIN_USER"
  green "  رمز عبور:   $ADMIN_PASS"
  green "=========================================================="
  yellow "  این رمز فقط همین یک بار نمایش داده می‌شود. حتما ذخیره کنید."
  yellow "  برای تغییر رمز:  vpnshop passwd $ADMIN_USER"
  echo ""
fi

echo "  اگر آدرس پنل را گم کردید، کافی است روی سرور بزنید:"
green "     vpnshop info"
echo ""
