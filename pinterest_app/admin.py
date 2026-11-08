import logging
import uuid

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django_celery_beat.models import (
    ClockedSchedule,
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
    SolarSchedule,
)
from rest_framework.authtoken.admin import TokenAdmin
from rest_framework.authtoken.models import TokenProxy
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display

from .forms import PinPublishTaskAdminForm, PinterestAccountAdminForm
from .analytics import refresh_account_stats, request_stats_refresh
from .models import AccountStatistics, PinterestAccount, PinterestBoard, PinPublishTask
from .services import PinterestServiceError, fail_pending_task, replace_boards
from .stats_dashboard import format_number, stats_detail_context, stats_overview_context
from .tasks import publish_pin_task, refresh_all_accounts_boards_task

logger = logging.getLogger(__name__)

admin.site.unregister(
    [
        PeriodicTask,
        IntervalSchedule,
        CrontabSchedule,
        SolarSchedule,
        ClockedSchedule,
        TokenProxy,
    ]
)


@admin.register(TokenProxy)
class ApiTokenAdmin(TokenAdmin, ModelAdmin):
    """DRF API tokens, re-registered so the page uses the Unfold theme."""


class PinterestBoardInline(TabularInline):
    model = PinterestBoard
    extra = 0
    can_delete = False
    show_change_link = False
    readonly_fields = ("board_id", "name", "url")
    fields = ("name", "board_id", "url")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PinterestAccount)
class PinterestAccountAdmin(ModelAdmin):
    form = PinterestAccountAdminForm
    list_display = ("name", "user", "is_active", "created_at", "statistics_link")
    list_filter = ("is_active", "user")
    search_fields = ("name", "user__username")
    readonly_fields = ("webhook_url", "created_at", "updated_at")
    inlines = (PinterestBoardInline,)
    actions = ("refresh_boards_action", "refresh_stats_action")

    def get_fields(self, request, obj=None):
        fields = ["name", "user", "proxy_type", "proxy", "cookies_input"]
        if obj is not None:
            fields += ["is_active", "webhook_url", "created_at", "updated_at"]
        if not request.user.is_superuser:
            fields.remove("user")
        return fields

    def get_inlines(self, request, obj):
        # Boards are loaded from Pinterest when the account is saved.
        return self.inlines if obj is not None else ()

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        initial.setdefault("user", request.user.pk)
        return initial

    @display(description=_("Webhook URL"))
    def webhook_url(self, obj):
        path = reverse("publish-pin-webhook", kwargs={"webhook_token": obj.webhook_token})
        return format_html("<code>{}</code>", path)

    @display(description=_("Statistics"))
    def statistics_link(self, obj):
        url = reverse("admin:pinterest_app_accountstatistics_change", args=[obj.pk])
        return format_html(
            '<a class="font-medium text-primary-600 dark:text-primary-500" href="{}">{}</a>',
            url,
            _("View"),
        )

    @admin.action(description=_("Refresh statistics for selected accounts"))
    def refresh_stats_action(self, request, queryset):
        queued = sum(request_stats_refresh(account, force=True) for account in queryset)
        self.message_user(
            request,
            _("Statistics refresh started for {} account(s).").format(queued),
            messages.SUCCESS,
        )

    @admin.action(description=_("Refresh boards for selected accounts"))
    def refresh_boards_action(self, request, queryset):
        account_ids = list(queryset.values_list("id", flat=True))
        try:
            refresh_all_accounts_boards_task.delay(account_ids)
        except Exception:
            logger.error("Could not queue board refresh from the admin.")
            self.message_user(
                request,
                _("Could not start the board refresh task. Check the Celery worker."),
                messages.ERROR,
            )
            return

        self.message_user(
            request,
            _("Board refresh started for {} account(s).").format(len(account_ids)),
            messages.SUCCESS,
        )

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset
        return queryset.filter(user=request.user)

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ("is_active", "user")
        return ("is_active",)

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            obj.user = request.user
        super().save_model(request, obj, form, change)
        # Boards were already loaded from Pinterest while validating the form.
        if form.fetched_boards is not None:
            replace_boards(obj, form.fetched_boards)


@admin.register(PinPublishTask)
class PinPublishTaskAdmin(ModelAdmin):
    form = PinPublishTaskAdminForm
    add_fields = ("board", "title", "description", "link", "image_url")
    list_display = ("id", "account", "display_status", "title", "error_details", "created_at")
    list_filter = ("status", "account")
    search_fields = ("title", "account__name")
    task_readonly_fields = (
        "account",
        "title",
        "description",
        "link",
        "image_url",
        "board_id",
        "board_name",
        "status",
        "error_details",
        "celery_task_id",
        "created_at",
        "updated_at",
    )

    def get_fields(self, request, obj=None):
        return self.add_fields if obj is None else self.task_readonly_fields

    def get_readonly_fields(self, request, obj=None):
        # Existing tasks are a record of what was published; only new ones are editable.
        return () if obj is None else self.task_readonly_fields

    def get_form(self, request, obj=None, change=False, **kwargs):
        if obj is not None:
            # Everything is read-only on existing tasks, so no board selector.
            kwargs["form"] = forms.ModelForm
            return super().get_form(request, obj, change=change, **kwargs)
        form_class = super().get_form(request, obj, change=change, **kwargs)
        boards = PinterestBoard.objects.filter(account__is_active=True).select_related(
            "account"
        ).order_by("account__name", "name")
        if not request.user.is_superuser:
            boards = boards.filter(account__user=request.user)

        class RequestBoundForm(form_class):
            def __init__(self, *args, **form_kwargs):
                super().__init__(*args, boards=boards, **form_kwargs)

        return RequestBoundForm

    def save_model(self, request, obj, form, change):
        if change:
            return
        obj.idempotency_key = f"admin:{uuid.uuid4()}"
        super().save_model(request, obj, form, change)
        # Queue only after the admin transaction commits, otherwise the worker
        # may look the task up before it exists.
        transaction.on_commit(lambda: self._enqueue(request, obj))

    def _enqueue(self, request, task):
        try:
            async_result = publish_pin_task.delay(task.id)
        except Exception:
            logger.error("Could not queue publish task %s from the admin.", task.id)
            fail_pending_task(
                task,
                "queue_error",
                "The publish queue is unavailable. Check Redis and the Celery worker.",
            )
            self.message_user(
                request,
                _("The task was saved but could not be queued. Check Redis and the Celery worker."),
                messages.ERROR,
            )
            return
        PinPublishTask.objects.filter(pk=task.pk).update(celery_task_id=async_result.id)

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
        if not isinstance(obj.error_json, dict):
            return "-"
        error_code = obj.error_json.get("error", "unknown_error")
        retry_count = obj.error_json.get("retry_count")
        details = error_code
        if isinstance(retry_count, int):
            details = f"{details} (retries: {retry_count})"
        return format_html(
            '<pre style="max-width: 300px; white-space: pre-wrap;">{}</pre>',
            details,
        )

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related("account")
        if request.user.is_superuser:
            return queryset
        return queryset.filter(account__user=request.user)

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ("status", "account")
        return ("status",)


def _can_view_accounts(request):
    user = request.user
    return user.is_active and user.is_staff and (
        user.has_perm("pinterest_app.view_pinterestaccount")
        or user.has_perm("pinterest_app.change_pinterestaccount")
    )


@admin.register(AccountStatistics)
class AccountStatisticsAdmin(ModelAdmin):
    """
    Read-only statistics pages: the list is the overview of all accounts and
    the change page shows one account. Access follows the account permissions.
    """

    def get_queryset(self, request):
        queryset = PinterestAccount.objects.all()
        if request.user.is_superuser:
            return queryset
        return queryset.filter(user=request.user)

    def has_module_permission(self, request):
        return _can_view_accounts(request)

    def has_view_permission(self, request, obj=None):
        return _can_view_accounts(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _context(self, request, title, **extra):
        return {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": title,
            **extra,
        }

    def _can_refresh(self, request):
        return request.user.has_perm("pinterest_app.change_pinterestaccount")

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_permission(request):
            raise PermissionDenied
        if request.method == "POST":
            # "Refresh all": queued, since many accounts would block the page.
            if not self._can_refresh(request):
                raise PermissionDenied
            accounts = self.get_queryset(request).filter(is_active=True)
            queued = sum(request_stats_refresh(account, force=True) for account in accounts)
            self.message_user(
                request,
                _("Statistics refresh started for {} account(s). Reload the page in a few minutes.").format(queued),
                messages.SUCCESS,
            )
            return HttpResponseRedirect(request.path)
        context = self._context(
            request,
            _("Account statistics"),
            stats=stats_overview_context(self.get_queryset(request)),
            can_refresh=self._can_refresh(request),
        )
        return TemplateResponse(request, "admin/pinterest_app/stats_overview.html", context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        if not self.has_view_permission(request):
            raise PermissionDenied
        account = self.get_queryset(request).filter(pk=object_id).first()
        if account is None:
            raise Http404
        can_refresh = self._can_refresh(request)
        if request.method == "POST":
            if not can_refresh:
                raise PermissionDenied
            self._refresh_now(request, account)
            return HttpResponseRedirect(request.path)
        context = self._context(
            request,
            _("Statistics: {}").format(account.name),
            stats=stats_detail_context(account),
            can_refresh=can_refresh,
        )
        return TemplateResponse(request, "admin/pinterest_app/stats_detail.html", context)

    def _refresh_now(self, request, account):
        """Loads the statistics in the request, so the result shows right away."""
        # Counts as the hourly refresh, so the next publication does not repeat it.
        PinterestAccount.objects.filter(pk=account.pk).update(stats_requested_at=timezone.now())
        try:
            snapshot = refresh_account_stats(account)
        except PinterestServiceError as exc:
            self.message_user(
                request, _("Could not load statistics: {}").format(exc.message), messages.ERROR
            )
        except Exception:
            logger.exception("Unexpected statistics refresh error for account %s.", account.id)
            self.message_user(
                request,
                _("Could not load statistics because of an unexpected error. See the server log."),
                messages.ERROR,
            )
        else:
            self.message_user(
                request,
                _("Statistics updated: {impressions} impressions, {saves} saves.").format(
                    impressions=format_number(snapshot.impressions),
                    saves=format_number(snapshot.saves),
                ),
                messages.SUCCESS,
            )
