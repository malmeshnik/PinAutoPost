from django.contrib import admin
from django.utils.html import format_html
from django.urls import path
from django.http import JsonResponse
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display, action
from .models import PinterestAccount, PinterestBoard, PinPublishTask
from .tasks import publish_pin_task

class PinterestBoardInline(TabularInline):
    model = PinterestBoard
    extra = 0

@admin.register(PinterestAccount)
class PinterestAccountAdmin(ModelAdmin):
    list_display = ("name", "user", "is_active", "webhook_token", "created_at")
    list_filter = ("is_active", "user")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(user=request.user)

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            obj.user = request.user
        super().save_model(request, obj, form, change)
    search_fields = ("name", "user__username")
    inlines = [PinterestBoardInline]

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('ajax/boards/<int:account_id>/', self.admin_site.admin_view(self.get_boards_ajax), name='ajax_boards'),
        ]
        return custom_urls + urls

    def get_boards_ajax(self, request, account_id):
        boards = PinterestBoard.objects.filter(account_id=account_id).values('board_id', 'name')
        return JsonResponse(list(boards), safe=False)

@admin.register(PinPublishTask)
class PinPublishTaskAdmin(ModelAdmin):
    list_display = ("id", "account", "display_status", "error_details", "title", "created_at")
    list_filter = ("status", "account")
    search_fields = ("title", "account__name")
    readonly_fields = ("celery_task_id", "created_at", "updated_at")

    @display(
        description=_("Status"),
        label={
            "PENDING": "info",
            "PROCESSING": "warning",
            "SUCCESS": "success",
            "FAILED": "danger",
        },
    )
    def display_status(self, obj):
        return obj.get_status_display()

    @display(description=_("Error Details"))
    def error_details(self, obj):
        if obj.error_json:
            import json
            return format_html(
                '<pre style="max-width: 300px; white-space: pre-wrap;">{}</pre>',
                json.dumps(obj.error_json, indent=2, ensure_ascii=False)
            )
        return "-"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change: # Only for new tasks created via admin
            celery_task = publish_pin_task.delay(obj.id)
            obj.celery_task_id = celery_task.id
            obj.save()

    class Media:
        js = ("js/admin_dynamic_boards.js",)

@admin.register(PinterestBoard)
class PinterestBoardAdmin(ModelAdmin):
    list_display = ("name", "board_id", "account")
    list_filter = ("account",)
    search_fields = ("name", "board_id")
