from django.urls import path

from .views import oxapay_webhook, sms_webhook, telegram_webhook

urlpatterns = [
    path('payments/oxapay/webhook/', oxapay_webhook, name='oxapay-webhook'),
    path('payments/sms/webhook/', sms_webhook, name='sms-webhook'),
    path('telegram/<str:secret>/', telegram_webhook, name='telegram-webhook'),
]
