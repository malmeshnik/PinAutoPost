import logging
from collections.abc import Sequence
from time import monotonic

from celery import shared_task
from .models import PinterestAccount, PinPublishTask
from .services import (
    PUBLISH_TIMEOUT_SECONDS,
    PinterestServiceError,
    publish_pin_for_task,
    refresh_boards,
)
from .monitoring import record_beat_heartbeat

logger = logging.getLogger(__name__)


@shared_task
def publish_pin_task(task_id):
    try:
        task = PinPublishTask.objects.get(id=task_id)
    except PinPublishTask.DoesNotExist:
        logger.error("Publish task %s was not found.", task_id)
        return

    publish_pin_for_task(task, deadline=monotonic() + PUBLISH_TIMEOUT_SECONDS)
    return task.status

@shared_task
def refresh_all_accounts_boards_task(account_ids: Sequence[int] | None = None):
    active_accounts = PinterestAccount.objects.filter(is_active=True)
    if account_ids is not None:
        active_accounts = active_accounts.filter(id__in=account_ids)
    logger.info(f"Starting scheduled board refresh for {active_accounts.count()} accounts")
    for account in active_accounts:
        try:
            refresh_boards(account)
        except PinterestServiceError as exc:
            logger.warning(
                "Board refresh failed for account %s (%s): %s",
                account.id,
                exc.code,
                exc.message,
            )
        except Exception:
            logger.exception("Unexpected board refresh error for account %s.", account.id)


@shared_task(ignore_result=True)
def refresh_account_stats_task(account_id):
    from .analytics import refresh_account_stats

    try:
        account = PinterestAccount.objects.get(id=account_id)
    except PinterestAccount.DoesNotExist:
        logger.warning("Statistics refresh skipped: account %s was not found.", account_id)
        return
    try:
        refresh_account_stats(account)
    except PinterestServiceError:
        pass  # Already logged and stored on the account.
    except Exception:
        logger.exception("Unexpected statistics refresh error for account %s.", account_id)


@shared_task(ignore_result=True)
def refresh_all_accounts_stats_task():
    """Daily refresh, so accounts that did not publish still have fresh numbers."""
    from .analytics import request_stats_refresh

    for account in PinterestAccount.objects.filter(is_active=True):
        request_stats_refresh(account)


@shared_task(ignore_result=True)
def record_beat_heartbeat_task():
    """Scheduled by Celery beat every minute; the monitor checks its freshness."""
    try:
        record_beat_heartbeat()
    except Exception:
        logger.warning("Could not record the Celery beat heartbeat.")
