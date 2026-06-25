from django.urls import path

from .views import oxapay_webhook

urlpatterns = [
    path('payments/oxapay/webhook/', oxapay_webhook, name='oxapay-webhook'),
]
