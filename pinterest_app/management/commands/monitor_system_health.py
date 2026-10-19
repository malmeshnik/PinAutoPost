import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from pinterest_app.monitoring import check_and_alert_system_health

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Checks the database, Redis, Celery workers and beat outside of Celery "
        "and sends Telegram alerts on state changes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=60,
            help="Seconds between checks (default: 60).",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single check and exit.",
        )

    def handle(self, *args, interval, once, **options):
        interval = max(interval, 10)
        while True:
            started = time.monotonic()
            close_old_connections()
            try:
                check_and_alert_system_health()
            except Exception:
                logger.exception("System health check failed.")
            if once:
                return
            time.sleep(max(0, interval - (time.monotonic() - started)))
