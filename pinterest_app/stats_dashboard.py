"""Builds the context of the account statistics pages in the admin."""
from django.db.models import OuterRef, Subquery
from django.urls import reverse
from django.utils.translation import gettext as _

from .analytics import STATS_PERIOD_DAYS, stats_period
from .models import AccountStatsSnapshot

HISTORY_LIMIT = 30


def _metric_labels():
    # Built per request so labels follow the active admin language.
    return (
        ("impressions", _("Impressions"), "visibility"),
        ("engagements", _("Engagements"), "touch_app"),
        ("outbound_clicks", _("Outbound clicks"), "open_in_new"),
        ("saves", _("Saves"), "bookmark"),
    )


def format_number(value):
    return f"{value:,}".replace(",", " ")


def _delta(fraction):
    if fraction is None:
        return None
    percent = round(fraction * 100, 1)
    tone = "up" if percent > 0 else "down" if percent < 0 else "flat"
    sign = "+" if percent > 0 else ""
    return {"text": f"{sign}{percent:g}%", "tone": tone}


def snapshot_metrics(snapshot):
    return [
        {
            "key": key,
            "label": label,
            "icon": icon,
            "value": format_number(getattr(snapshot, key)) if snapshot else "—",
            "delta": _delta(getattr(snapshot, f"{key}_delta")) if snapshot else None,
        }
        for key, label, icon in _metric_labels()
    ]


def _total_metrics(snapshots):
    """
    Sums the accounts. The change is the total against the summed previous
    periods, where an account's previous value is value / (1 + change);
    accounts without a known change are left out of that comparison.
    """
    metrics = []
    for key, label, icon in _metric_labels():
        total = sum(getattr(s, key) for s in snapshots)
        current = previous = 0.0
        for s in snapshots:
            change = getattr(s, f"{key}_delta")
            if change is not None and change > -1:
                current += getattr(s, key)
                previous += getattr(s, key) / (1 + change)
        change = current / previous - 1 if previous else None
        metrics.append(
            {
                "key": key,
                "label": label,
                "icon": icon,
                "value": format_number(total),
                "delta": _delta(change),
            }
        )
    return metrics


def latest_snapshots(accounts):
    latest_id = (
        AccountStatsSnapshot.objects.filter(account=OuterRef("account_id"))
        .order_by("-created_at", "-id")
        .values("id")[:1]
    )
    snapshots = AccountStatsSnapshot.objects.filter(account__in=accounts).filter(
        id=Subquery(latest_id)
    )
    return {s.account_id: s for s in snapshots}


def stats_overview_context(accounts):
    accounts = list(accounts.order_by("name"))
    by_account = latest_snapshots(accounts)
    rows = []
    for account in accounts:
        snapshot = by_account.get(account.id)
        rows.append(
            {
                "account": account,
                "snapshot": snapshot,
                "metrics": snapshot_metrics(snapshot),
                "error": account.stats_error,
                "url": reverse("admin:pinterest_app_accountstatistics_change", args=[account.pk]),
                "sort_key": (snapshot is None, -(snapshot.impressions if snapshot else 0)),
            }
        )
    rows.sort(key=lambda row: row["sort_key"])

    snapshots = list(by_account.values())
    period_start, period_end = stats_period()
    if snapshots:
        newest = max(snapshots, key=lambda s: s.created_at)
        period_start, period_end = newest.period_start, newest.period_end
    return {
        "rows": rows,
        "totals": _total_metrics(snapshots),
        "accounts_total": len(accounts),
        "accounts_with_stats": len(snapshots),
        "accounts_with_errors": sum(1 for row in rows if row["error"]),
        "period_start": period_start,
        "period_end": period_end,
        "period_days": STATS_PERIOD_DAYS,
    }


def stats_detail_context(account):
    history = list(account.stats_snapshots.order_by("-created_at", "-id")[:HISTORY_LIMIT])
    latest = history[0] if history else None
    return {
        "account": account,
        "latest": latest,
        "metrics": snapshot_metrics(latest),
        "history": [
            {"snapshot": s, "metrics": snapshot_metrics(s)} for s in history
        ],
        "error": account.stats_error,
        "period_days": STATS_PERIOD_DAYS,
        "overview_url": reverse("admin:pinterest_app_accountstatistics_changelist"),
        "account_url": reverse("admin:pinterest_app_pinterestaccount_change", args=[account.pk]),
    }
