"""
System health checks and Telegram alerts.

The checks run in a dedicated process (``manage.py monitor_system_health``),
not inside Celery: a check that is delivered through Redis and executed by a
worker cannot report that Redis or the workers are down. Celery beat proves
it is alive by scheduling ``record_beat_heartbeat_task`` every minute.
"""
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from html import escape

from celery import current_app
from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.utils import timezone
import redis

from .models import PinterestAccount, PinPublishTask, SystemAlertState
from .services import (
    describe_proxy_failure,
    recover_stale_processing_tasks,
    send_telegram_alert,
)

logger = logging.getLogger(__name__)

ALERT_COOLDOWN = timedelta(minutes=30)
PROXY_ERROR_WINDOW = timedelta(minutes=15)
PROXY_ERROR_THRESHOLD = 3
PENDING_QUEUE_THRESHOLD = 20
DATABASE_ALERT_KEY = "pinautopost:monitor:database-down"
BEAT_HEARTBEAT_KEY = "pinautopost:monitor:beat-heartbeat"
BEAT_HEARTBEAT_MAX_AGE = timedelta(minutes=3)
HEALTH_SNAPSHOT_KEY = "monitor:health-snapshot"
# The dashboard treats a snapshot older than this as "monitor is not running".
HEALTH_SNAPSHOT_MAX_AGE = timedelta(minutes=3)
# A service counts as down only after this many failed checks in a row, so a
# stack that is still starting (or a worker restart) does not raise an alert.
FAILURES_BEFORE_ALERT = 2


def _redis_client():
    return redis.Redis.from_url(
        settings.CELERY_BROKER_URL,
        socket_connect_timeout=1,
        socket_timeout=1,
    )


def record_beat_heartbeat():
    client = _redis_client()
    try:
        client.set(BEAT_HEARTBEAT_KEY, str(timezone.now().timestamp()))
    finally:
        client.close()


def _probe_database():
    try:
        connection.ensure_connection()
    except Exception:
        logger.warning("Database health check failed.")
        return False
    return True


def _probe_redis():
    client = _redis_client()
    try:
        return bool(client.ping())
    except (redis.RedisError, ValueError):
        logger.warning("Redis health check failed.")
        return False
    finally:
        client.close()


def _probe_workers():
    # The monitor is not a worker itself, so a reply proves a real worker is up.
    try:
        return bool(current_app.control.ping(timeout=1, limit=1))
    except Exception:
        logger.warning("Celery worker health check failed.")
        return False


def _probe_beat():
    client = _redis_client()
    try:
        raw_value = client.get(BEAT_HEARTBEAT_KEY)
    except (redis.RedisError, ValueError):
        logger.warning("Celery beat heartbeat could not be read.")
        return None
    finally:
        client.close()

    if raw_value is None:
        return None
    try:
        last_beat = datetime.fromtimestamp(float(raw_value), tz=dt_timezone.utc)
    except (TypeError, ValueError):
        return None
    return last_beat >= timezone.now() - BEAT_HEARTBEAT_MAX_AGE


def get_system_health():
    close_old_connections()
    database_ok = _probe_database()
    redis_ok = _probe_redis()
    workers_ok = _probe_workers() if redis_ok else False
    beat_ok = _probe_beat() if redis_ok else None

    return [
        {"name": "Database", "ok": database_ok, "detail": "PostgreSQL connection"},
        {"name": "Redis", "ok": redis_ok, "detail": "Celery broker connection"},
        {
            "name": "Celery workers",
            "ok": workers_ok,
            "detail": "At least one worker responds to ping",
        },
        {
            "name": "Celery beat",
            "ok": beat_ok,
            "detail": "Scheduler heartbeat received within the last 3 minutes",
        },
    ]


def save_health_snapshot(health):
    SystemAlertState.objects.update_or_create(
        key=HEALTH_SNAPSHOT_KEY,
        defaults={
            "payload": {
                "checked_at": timezone.now().isoformat(),
                "components": health,
            }
        },
    )


def get_health_snapshot():
    """
    Returns the latest health check written by the monitor process, without
    probing anything. ``is_stale`` is True when the monitor stopped reporting.
    """
    state = SystemAlertState.objects.filter(key=HEALTH_SNAPSHOT_KEY).first()
    payload = state.payload if state and isinstance(state.payload, dict) else {}
    checked_at = None
    if isinstance(payload.get("checked_at"), str):
        try:
            checked_at = datetime.fromisoformat(payload["checked_at"])
        except ValueError:
            checked_at = None
    components = payload.get("components")
    if not isinstance(components, list):
        components = []
    return {
        "checked_at": checked_at,
        "components": [c for c in components if isinstance(c, dict)],
        "is_stale": (
            checked_at is None
            or checked_at < timezone.now() - HEALTH_SNAPSHOT_MAX_AGE
        ),
    }


def _debounce_failures(health):
    """
    Reports a failing service as unknown (None) until it has failed
    FAILURES_BEFORE_ALERT checks in a row.
    """
    snapshot = get_health_snapshot()
    previous = {} if snapshot["is_stale"] else {
        component.get("name"): component for component in snapshot["components"]
    }
    debounced = []
    for component in health:
        component = dict(component)
        if component["ok"] is False:
            streak = previous.get(component["name"], {}).get("fail_streak", 0) + 1
            component["fail_streak"] = streak
            if streak < FAILURES_BEFORE_ALERT:
                component["ok"] = None
        else:
            component["fail_streak"] = 0
        debounced.append(component)
    return debounced


def _check_database_alert(database_ok, redis_ok):
    if not redis_ok:
        return
    client = None
    try:
        client = redis.Redis.from_url(
            settings.CELERY_BROKER_URL,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        if database_ok:
            if client.get(DATABASE_ALERT_KEY) and send_telegram_alert(
                "✅ <b>Database</b>: з'єднання з БД відновлено."
            ):
                client.delete(DATABASE_ALERT_KEY)
        else:
            claimed = client.set(
                DATABASE_ALERT_KEY,
                "1",
                nx=True,
                ex=int(ALERT_COOLDOWN.total_seconds()),
            )
            if claimed and not send_telegram_alert(
                "🚨 <b>Database</b>: застосунок не може підключитися до БД. "
                "Перевірте PostgreSQL та мережеве з'єднання."
            ):
                client.delete(DATABASE_ALERT_KEY)
    except (redis.RedisError, ValueError):
        logger.warning("Could not update database alert state in Redis.")
    finally:
        if client is not None:
            client.close()


def _send_transition_alert(key, label, message, is_active):
    should_notify = False
    now = timezone.now()
    with transaction.atomic():
        state, _ = SystemAlertState.objects.select_for_update().get_or_create(key=key)
        if is_active:
            should_notify = (
                not state.is_active
                or state.last_notified_at is None
                or now - state.last_notified_at >= ALERT_COOLDOWN
            )
            state.is_active = True
        else:
            should_notify = state.is_active
            if should_notify:
                state.last_notified_at = now

        if should_notify:
            state.last_notified_at = now
        state.save(update_fields=["is_active", "last_notified_at", "updated_at"])

    if not should_notify:
        return

    state_text = "відновлено" if not is_active else "потрібна увага"
    sent = send_telegram_alert(
        f"⚠️ <b>{escape(label)}</b>: {state_text}\n{escape(message)}"
    )
    if sent:
        if not is_active:
            SystemAlertState.objects.filter(key=key).update(is_active=False)
    else:
        SystemAlertState.objects.filter(key=key).update(last_notified_at=None)


def _recent_proxy_failures():
    since = timezone.now() - PROXY_ERROR_WINDOW
    rows = (
        PinPublishTask.objects.filter(
            status="FAILED",
            updated_at__gte=since,
            error_json__error="proxy_error",
        )
        .values_list("account_id", "account__name")
    )
    failures = {}
    for account_id, account_name in rows:
        item = failures.setdefault(account_id, {"name": account_name, "count": 0})
        item["count"] += 1
    return failures


def _recent_auth_failures():
    since = timezone.now() - PROXY_ERROR_WINDOW
    rows = (
        PinPublishTask.objects.filter(
            status="FAILED",
            updated_at__gte=since,
            error_json__error="auth_error",
        )
        .values_list("account_id", "account__name")
    )
    return {account_id: name for account_id, name in rows}


def check_and_alert_system_health():
    health = get_system_health()
    database_ok = next(
        component["ok"] for component in health if component["name"] == "Database"
    )
    redis_ok = next(
        component["ok"] for component in health if component["name"] == "Redis"
    )
    _check_database_alert(database_ok, redis_ok)
    if not database_ok:
        return

    health = _debounce_failures(health)
    save_health_snapshot(health)

    for component in health:
        if component["name"] == "Database" or component["ok"] is None:
            continue
        _send_transition_alert(
            f"service:{component['name']}",
            component["name"],
            component["detail"],
            not component["ok"],
        )

    recent_proxy_failures = _recent_proxy_failures()
    accounts = PinterestAccount.objects.in_bulk(recent_proxy_failures)
    for account_id, account in accounts.items():
        failures = recent_proxy_failures[account_id]["count"]
        _send_transition_alert(
            f"proxy:{account_id}",
            f"Проксі акаунта {account.name}",
            (
                f"{failures} помилки за останні 15 хвилин. Перевірте адресу, порт, "
                "тип проксі та доступи у провайдера."
            ),
            failures >= PROXY_ERROR_THRESHOLD,
        )

    recent_auth_failures = _recent_auth_failures()
    accounts = PinterestAccount.objects.in_bulk(recent_auth_failures)
    for account_id, account in accounts.items():
        _send_transition_alert(
            f"auth:{account_id}",
            f"Pinterest акаунт {account.name}",
            "Pinterest відхилив авторизацію. Оновіть cookies та перевірте акаунт.",
            True,
        )

    pending_count = PinPublishTask.objects.filter(status="PENDING").count()
    _send_transition_alert(
        "queue:pending",
        "Черга публікацій",
        f"Очікує {pending_count} задач. Перевірте Celery worker та його доступ до Redis.",
        pending_count >= PENDING_QUEUE_THRESHOLD,
    )

    recovered_count = recover_stale_processing_tasks()
    _send_transition_alert(
        "queue:stale-processing",
        "Перервані публікації",
        (
            f"{recovered_count} задач зависли у статусі PROCESSING і позначені як "
            "publish_unknown. Перевірте, чи не перезапускався web/worker, і чи "
            "з'явилися піни на дошках."
        ),
        recovered_count > 0,
    )

    active_proxy_alerts = {
        f"proxy:{account_id}"
        for account_id, failure in recent_proxy_failures.items()
        if failure["count"] >= PROXY_ERROR_THRESHOLD
    }
    active_auth_alerts = {f"auth:{account_id}" for account_id in recent_auth_failures}
    for state in SystemAlertState.objects.filter(
        is_active=True,
        key__startswith="proxy:",
    ).exclude(key__in=active_proxy_alerts):
        _send_transition_alert(state.key, "Проксі", "Помилку проксі більше не спостерігаємо.", False)
    for state in SystemAlertState.objects.filter(
        is_active=True,
        key__startswith="auth:",
    ).exclude(key__in=active_auth_alerts):
        _send_transition_alert(state.key, "Pinterest авторизація", "Нових помилок авторизації немає.", False)


def get_recent_publish_failures(tasks=None, limit=10):
    tasks = PinPublishTask.objects.all() if tasks is None else tasks
    tasks = (
        tasks.filter(status="FAILED")
        .select_related("account")
        .order_by("-updated_at")[:limit]
    )
    failures = []
    for task in tasks:
        error_data = task.error_json if isinstance(task.error_json, dict) else {}
        error_code = error_data.get("error")
        if not isinstance(error_code, str):
            error_code = "unknown_error"
        message = error_data.get("message")
        if error_code == "proxy_error":
            message = describe_proxy_failure(message or "")
        elif not isinstance(message, str) or not message:
            message = error_code.replace("_", " ")
        failures.append(
            {
                "id": task.id,
                "account": task.account.name,
                "title": task.title or "",
                "error": error_code,
                "message": message,
                "updated_at": task.updated_at,
            }
        )
    return failures
