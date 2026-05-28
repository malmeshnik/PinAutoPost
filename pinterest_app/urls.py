from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PinterestAccountViewSet, publish_pin_webhook

router = DefaultRouter()
router.register(r'accounts', PinterestAccountViewSet, basename='pinterestaccount')

urlpatterns = [
    path('', include(router.urls)),
    path('publish/<uuid:webhook_token>/', publish_pin_webhook, name='publish_pin_webhook'),
]
