import json
from django.utils import timezone
from unfold.admin import ModelAdmin
from pinterest_app.models import PinterestAccount, PinPublishTask

def dashboard_callback(request, context):
    today = timezone.now().date()

    # Metrics
    total_accounts = PinterestAccount.objects.count()
    active_accounts = PinterestAccount.objects.filter(is_active=True).count()
    inactive_accounts = total_accounts - active_accounts

    pending_tasks = PinPublishTask.objects.filter(status='PENDING').count()
    processing_tasks = PinPublishTask.objects.filter(status='PROCESSING').count()
    queue_count = pending_tasks + processing_tasks

    success_today = PinPublishTask.objects.filter(status='SUCCESS', updated_at__date=today).count()
    failed_today = PinPublishTask.objects.filter(status='FAILED', updated_at__date=today).count()

    # Categorized failed tasks for today
    auth_errors = 0
    proxy_errors = 0

    failed_tasks_today = PinPublishTask.objects.filter(status='FAILED', updated_at__date=today)
    for task in failed_tasks_today:
        error_type = task.error_json.get('error') if task.error_json else None
        if error_type == 'auth_error':
            auth_errors += 1
        elif error_type == 'proxy_error':
            proxy_errors += 1

    context.update({
        "metrics": [
            {
                "title": "Accounts",
                "active": active_accounts,
                "inactive": inactive_accounts,
                "total": total_accounts,
            },
            {
                "title": "Queue",
                "pending": pending_tasks,
                "processing": processing_tasks,
                "total": queue_count,
            },
            {
                "title": "Today Performance",
                "success": success_today,
                "failed": failed_today,
            },
            {
                "title": "Critical Issues Today",
                "auth_errors": auth_errors,
                "proxy_errors": proxy_errors,
            }
        ],
    })
    return context
