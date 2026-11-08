from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from pinterest_app.models import PinterestAccount, PinPublishTask
from pinterest_app.stats_dashboard import format_number, latest_snapshots
from pinterest_app.monitoring import (
    get_health_snapshot,
    get_recent_publish_failures,
)


def _health_labels():
    # Built per request so labels follow the active admin language.
    return {
        "Database": (_("Database"), _("PostgreSQL connection")),
        "Redis": (_("Redis"), _("Celery broker connection")),
        "Celery workers": (_("Celery workers"), _("At least one worker responds to ping")),
        "Celery beat": (_("Celery beat"), _("Scheduler heartbeat within the last 3 minutes")),
    }


ERROR_GROUPS = (
    ("auth_error", "critical"),
    ("proxy_error", "serious"),
    ("timeout_error", "warning"),
    ("publish_unknown", "warning"),
)


def _health_components(snapshot):
    components = []
    by_name = {c.get("name"): c for c in snapshot["components"]}
    for name, (label, detail) in _health_labels().items():
        if name == "Database":
            # This page was rendered from the database, so it is reachable now.
            state = "ok"
        else:
            ok = by_name.get(name, {}).get("ok")
            if snapshot["is_stale"] or ok is None:
                state = "unknown"
            else:
                state = "ok" if ok else "issue"
        components.append({"name": label, "detail": detail, "state": state})
    return components


def dashboard_callback(request, context):
    today = timezone.localdate()
    accounts = PinterestAccount.objects.all()
    if not request.user.is_superuser:
        accounts = accounts.filter(user=request.user)
    tasks = PinPublishTask.objects.filter(account__in=accounts)

    account_counts = accounts.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True)),
    )
    task_counts = tasks.aggregate(
        pending=Count("id", filter=Q(status="PENDING")),
        processing=Count("id", filter=Q(status="PROCESSING")),
        success_today=Count("id", filter=Q(status="SUCCESS", updated_at__date=today)),
        failed_today=Count("id", filter=Q(status="FAILED", updated_at__date=today)),
    )
    finished_today = task_counts["success_today"] + task_counts["failed_today"]
    success_rate = (
        round(task_counts["success_today"] * 100 / finished_today)
        if finished_today
        else None
    )

    error_counts = {code: 0 for code, _tone in ERROR_GROUPS}
    other_errors = 0
    for error_json in tasks.filter(
        status="FAILED", updated_at__date=today
    ).values_list("error_json", flat=True):
        code = error_json.get("error") if isinstance(error_json, dict) else None
        if code in error_counts:
            error_counts[code] += 1
        else:
            other_errors += 1

    tasks_url = reverse("admin:pinterest_app_pinpublishtask_changelist")
    accounts_url = reverse("admin:pinterest_app_pinterestaccount_changelist")
    error_labels = {
        "auth_error": _("Authorization"),
        "proxy_error": _("Proxy"),
        "timeout_error": _("Timeouts"),
        "publish_unknown": _("Unknown outcome"),
    }
    issues = [
        {
            "label": error_labels[code],
            "code": code,
            "count": error_counts[code],
            "tone": tone,
        }
        for code, tone in ERROR_GROUPS
    ]
    issues.append(
        {"label": _("Other"), "code": "", "count": other_errors, "tone": "neutral"}
    )

    snapshots = latest_snapshots(accounts).values()
    statistics = {
        "impressions": format_number(sum(s.impressions for s in snapshots)),
        "saves": format_number(sum(s.saves for s in snapshots)),
        "accounts": len(snapshots),
        "url": reverse("admin:pinterest_app_accountstatistics_changelist"),
    }

    snapshot = get_health_snapshot() if request.user.is_superuser else None
    health = _health_components(snapshot) if snapshot else []
    health_issues = sum(1 for c in health if c["state"] == "issue")

    context.update(
        {
            "dashboard": {
                "accounts": {
                    "total": account_counts["total"],
                    "active": account_counts["active"],
                    "inactive": account_counts["total"] - account_counts["active"],
                    "url": accounts_url,
                },
                "queue": {
                    "total": task_counts["pending"] + task_counts["processing"],
                    "pending": task_counts["pending"],
                    "processing": task_counts["processing"],
                    "url": f"{tasks_url}?status__exact=PENDING",
                },
                "today": {
                    "success": task_counts["success_today"],
                    "failed": task_counts["failed_today"],
                    "total": finished_today,
                    "success_rate": success_rate,
                    "url": f"{tasks_url}?status__exact=FAILED",
                    "success_url": f"{tasks_url}?status__exact=SUCCESS",
                },
                "statistics": statistics,
                "issues": issues,
                "issues_total": task_counts["failed_today"],
                "health": health,
                "health_issues": health_issues,
                "health_checked_at": snapshot["checked_at"] if snapshot else None,
                "health_stale": snapshot["is_stale"] if snapshot else False,
                "show_health": request.user.is_superuser,
                "recent_failures": get_recent_publish_failures(tasks, limit=8),
                "tasks_url": tasks_url,
            }
        }
    )
    return context
