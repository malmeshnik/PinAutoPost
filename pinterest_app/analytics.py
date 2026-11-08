"""
Account statistics from Pinterest Analytics (analytics.pinterest.com).

The analytics site talks to a GraphQL endpoint with persisted queries: the
request names a stored query by its hash and only passes variables, so the
fields of the answer are fixed by the hash. The summary query below is the
"topline metrics" block of the Overview page: totals for the period and their
change against the previous period of the same length.
"""
import json
import logging
from datetime import datetime, time as dt_time, timedelta, timezone as dt_timezone

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import AccountStatsSnapshot, PinterestAccount
from .services import PinterestServiceError, describe_proxy_failure, get_session_and_csrf

logger = logging.getLogger(__name__)

ANALYTICS_GRAPHQL_URL = "https://analytics.pinterest.com/_/graphql/"
SUMMARY_QUERY_NAME = "TitleWithDataAvailabilityDisclaimerWithDataCoreToplineMetricsQuery"
STATS_PERIOD_DAYS = 30
# Pinterest finalizes daily data with a delay; its own "last 30 days" view
# ends two days before today.
STATS_DATA_LAG_DAYS = 2
STATS_REFRESH_INTERVAL = timedelta(hours=1)
REQUEST_TIMEOUT_SECONDS = 30

# Pinterest response key -> snapshot field.
METRICS = (
    ("impression", "impressions"),
    ("engagement", "engagements"),
    ("outboundClick", "outbound_clicks"),
    ("save", "saves"),
)


def stats_period(today=None):
    """Returns (start, end) dates of the "last 30 days" period, inclusive."""
    today = today or timezone.now().astimezone(dt_timezone.utc).date()
    end = today - timedelta(days=STATS_DATA_LAG_DAYS)
    return end - timedelta(days=STATS_PERIOD_DAYS), end


def _utc_timestamp(day, at):
    return int(datetime.combine(day, at, tzinfo=dt_timezone.utc).timestamp())


def build_summary_variables(user_id, start, end):
    return {
        "user": user_id,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "startTimestamp": _utc_timestamp(start, dt_time.min),
        "endTimestamp": _utc_timestamp(end, dt_time(23, 59, 59)),
        "appTypes": "all",
        "ownedContentList": [],
        "pinFormat": None,
        # "2" means "any" for these filters in the analytics UI.
        "paid": "2",
        "inProfile": "2",
        "fromOwnedContent": "2",
        "includeCurated": "0",
        "boardIds": None,
        "metricTypes": ["IMPRESSION", "ENGAGEMENT", "OUTBOUND_CLICK", "SAVE"],
        "ages": "all",
        "genders": "all",
        "productGroupIds": None,
        "includeRealtimeData": True,
        "includeOfflineData": True,
    }


def _post(session, url, **kwargs):
    try:
        return session.request(**kwargs, url=url, timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception as exc:
        raise PinterestServiceError("proxy_error", describe_proxy_failure(exc)) from None


def _json(response):
    try:
        return response.json()
    except ValueError:
        raise PinterestServiceError("api_error", "Pinterest returned an invalid response.") from None


def fetch_pinterest_user_id(session, csrf_token, base_url, username):
    """
    Returns the id of the user the cookies are logged in as. Pinterest puts it
    in client_context of every resource response; the requested profile is
    the fallback.
    """
    data = {"options": {"username": username, "field_set_key": "profile"}, "context": {}}
    response = _post(
        session,
        f"https://{base_url}/resource/UserResource/get/",
        method="GET",
        params={"source_url": f"/{username}/", "data": json.dumps(data)},
        headers={
            "x-csrftoken": csrf_token,
            "x-requested-with": "XMLHttpRequest",
            "accept": "application/json, text/javascript, */*; q=0.01",
            # Without the page headers Pinterest answers 403 "Invalid Resource Request".
            "x-pinterest-appstate": "active",
            "x-pinterest-pws-handler": "www/[username].js",
            "x-pinterest-source-url": f"/{username}/",
            "referer": f"https://{base_url}/{username}/",
        },
    )
    if response.status_code == 401:
        raise PinterestServiceError("auth_error", "Pinterest rejected the account cookies.")
    if response.status_code != 200:
        raise PinterestServiceError("api_error", "Pinterest could not load the account profile.")
    payload = _json(response)
    if not isinstance(payload, dict):
        payload = {}
    user_id = None
    for container in (payload.get("client_context"), payload.get("resource_response")):
        if not isinstance(container, dict):
            continue
        user = container.get("user") if "user" in container else container.get("data")
        if isinstance(user, dict) and str(user.get("id") or "").isdigit():
            user_id = user["id"]
            break
    if not user_id:
        raise PinterestServiceError("api_error", "Pinterest returned no user id for this account.")
    return str(user_id)


def fetch_summary_metrics(session, csrf_token, user_id, start, end):
    body = {
        "queryHash": settings.PINTEREST_ANALYTICS_SUMMARY_QUERY_HASH,
        "variables": build_summary_variables(user_id, start, end),
    }
    response = _post(
        session,
        ANALYTICS_GRAPHQL_URL,
        method="POST",
        data=json.dumps(body),
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "x-csrftoken": csrf_token,
            "x-requested-with": "XMLHttpRequest",
            "x-pinterest-appstate": "active",
            "x-pinterest-graphql-name": SUMMARY_QUERY_NAME,
            "x-pinterest-pws-handler": "analytics/overview.js",
            "x-pinterest-source-url": f"/overview/?actingBusinessId={user_id}&aggregation=last30d",
            "origin": "https://analytics.pinterest.com",
            "referer": "https://analytics.pinterest.com/",
        },
    )
    if response.status_code in (401, 403):
        raise PinterestServiceError(
            "auth_error",
            "Pinterest Analytics denied access. Check the cookies and that the account is a business account.",
        )
    if response.status_code != 200:
        code = "proxy_error" if response.status_code >= 500 else "api_error"
        raise PinterestServiceError(code, f"Pinterest Analytics returned HTTP {response.status_code}.")
    return parse_summary_response(_json(response))


def parse_summary_response(payload):
    """Turns the GraphQL answer into snapshot field values."""
    if isinstance(payload, dict) and payload.get("errors"):
        # A changed query hash shows up here (Pinterest updated its frontend).
        raise PinterestServiceError(
            "api_error",
            "Pinterest Analytics rejected the query. The query hash may be outdated.",
        )
    try:
        data = payload["data"]["v3AnalyticsMetricsDeltasQuery"]["data"]
        summary = data["summaryMetrics"]
    except (KeyError, TypeError):
        raise PinterestServiceError("api_error", "Pinterest Analytics returned no statistics.") from None
    if not isinstance(summary, dict):
        raise PinterestServiceError("api_error", "Pinterest Analytics returned no statistics.")

    deltas = data.get("deltas") if isinstance(data.get("deltas"), dict) else {}
    availability = data.get("dateAvailability") if isinstance(data.get("dateAvailability"), dict) else {}
    values = {"is_complete": True}
    for key, field in METRICS:
        values[field] = _to_int(summary.get(f"{key}Float"))
        values[f"{field}_delta"] = _to_float(deltas.get(key))
        status = (availability.get(key) or {}).get("summaryDataStatus")
        if status is not None and status != "READY":
            values["is_complete"] = False
    return values


def _to_int(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(round(value))


def _to_float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def refresh_account_stats(account, today=None):
    """Loads the current statistics and stores them as a new snapshot."""
    start, end = stats_period(today)
    try:
        session, csrf_token, base_url = get_session_and_csrf(account)
        # Exported csrftoken cookies are usually bound to www.pinterest.com and
        # are not sent to the analytics subdomain, which then answers
        # 403 "No CSRF cookie".
        session.cookies.set(
            "csrftoken", csrf_token, domain="analytics.pinterest.com", path="/"
        )
        try:
            if not account.pinterest_user_id:
                account.pinterest_user_id = fetch_pinterest_user_id(
                    session, csrf_token, base_url, account.name
                )
                PinterestAccount.objects.filter(pk=account.pk).update(
                    pinterest_user_id=account.pinterest_user_id
                )
            values = fetch_summary_metrics(
                session, csrf_token, account.pinterest_user_id, start, end
            )
        finally:
            session.close()
    except PinterestServiceError as exc:
        # Statistics are informational: a failure never deactivates the account.
        PinterestAccount.objects.filter(pk=account.pk).update(stats_error=exc.message[:255])
        logger.warning("Statistics refresh failed for account %s (%s): %s", account.id, exc.code, exc.message)
        raise

    snapshot = AccountStatsSnapshot.objects.create(
        account=account, period_start=start, period_end=end, **values
    )
    PinterestAccount.objects.filter(pk=account.pk).update(stats_error="")
    logger.info("Stored statistics snapshot %s for account %s.", snapshot.id, account.id)
    return snapshot


def request_stats_refresh(account, force=False):
    """
    Queues a statistics refresh unless one was queued within the last hour.
    The check and the timestamp update are a single UPDATE, so concurrent
    publications of one account queue at most one refresh. Returns True when
    a refresh was queued.
    """
    now = timezone.now()
    accounts = PinterestAccount.objects.filter(pk=account.pk)
    if not force:
        accounts = accounts.filter(
            Q(stats_requested_at__isnull=True)
            | Q(stats_requested_at__lt=now - STATS_REFRESH_INTERVAL)
        )
    if not accounts.update(stats_requested_at=now):
        return False
    # Enqueue after the surrounding transaction (if any) commits.
    transaction.on_commit(lambda: _enqueue_refresh(account.pk))
    return True


def _enqueue_refresh(account_id):
    from .tasks import refresh_account_stats_task

    try:
        refresh_account_stats_task.delay(account_id)
    except Exception:
        logger.warning("Could not queue a statistics refresh for account %s.", account_id)
        # Let the next publication try again instead of waiting an hour.
        PinterestAccount.objects.filter(pk=account_id).update(stats_requested_at=None)
