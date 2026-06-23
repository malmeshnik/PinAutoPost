from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PinterestAccountViewSet, publish_pin_webhook, get_task_status

router = DefaultRouter()
router.register(r'accounts', PinterestAccountViewSet, basename='pinterestaccount')

urlpatterns = [
    path('', include(router.urls)),
    path('publish/<uuid:webhook_token>/', publish_pin_webhook, name='publish-pin-webhook'),
    path('task/<int:task_id>/', get_task_status, name='task-status-legacy'),
    path('publish/status/<int:task_id>/', get_task_status, name='task-status'),
]
