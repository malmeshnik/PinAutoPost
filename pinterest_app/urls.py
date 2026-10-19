from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ApiTokenView,
    PinterestAccountViewSet,
    get_legacy_task_status,
    get_task_status,
    publish_pin_webhook,
)

router = DefaultRouter()
router.register(r'accounts', PinterestAccountViewSet, basename='pinterestaccount')

urlpatterns = [
    path('', include(router.urls)),
    path('auth/token/', ApiTokenView.as_view(), name='api-token'),
    path('publish/<uuid:webhook_token>/', publish_pin_webhook, name='publish-pin-webhook'),
    path(
        'publish/status/<uuid:status_token>/',
        get_task_status,
        name='task-status',
    ),
    path(
        'task/<uuid:status_token>/',
        get_task_status,
        name='task-status-legacy',
    ),
    # Integer-id routes returned by the old API; they expose the status only.
    path(
        'publish/status/<int:task_id>/',
        get_legacy_task_status,
        name='task-status-by-id',
    ),
    path(
        'task/<int:task_id>/',
        get_legacy_task_status,
        name='task-status-by-id-legacy',
    ),
]
