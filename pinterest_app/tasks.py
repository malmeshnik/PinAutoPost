import logging
from celery import shared_task
from django.utils import timezone
from .models import PinterestAccount, PinterestBoard, PinPublishTask
from .services import create_pin, refresh_boards

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3)
def publish_pin_task(self, task_id):
    try:
        task = PinPublishTask.objects.get(id=task_id)
    except PinPublishTask.DoesNotExist:
        logger.error(f"Task with id {task_id} not found")
        return

    task.status = 'PROCESSING'
    task.celery_task_id = self.request.id
    task.save()

    account = task.account
    board_id = task.board_id
    board_name = task.board_name

    # Logic for finding board_id by board_name
    if not board_id and board_name:
        board = account.boards.filter(name__iexact=board_name).first()
        if not board:
            # Refresh boards and try again
            refresh_boards(account)
            board = account.boards.filter(name__iexact=board_name).first()

        if board:
            board_id = board.board_id
            task.board_id = board_id
            task.save()
        else:
            task.status = 'FAILED'
            task.error_json = {
                "error": "validation_error",
                "message": f"Board with name '{board_name}' not found after refresh"
            }
            task.save()
            return

    if not board_id:
        task.status = 'FAILED'
        task.error_json = {
            "error": "validation_error",
            "message": "No board_id or board_name provided"
        }
        task.save()
        return

    success, message = create_pin(
        account=account,
        title=task.title,
        description=task.description,
        link=task.link,
        image_url=task.image_url,
        board_id=board_id
    )

    if success:
        task.status = 'SUCCESS'
        task.error_json = None
    else:
        task.status = 'FAILED'
        # Standardizing errors
        error_type = "unknown_error"
        if "Auth error" in message or "expired" in message.lower():
            error_type = "auth_error"
            message = "Pinterest cookies expired. Account deactivated."
        elif "Proxy" in message or "proxy" in message.lower() or "connection timeout" in message.lower():
            error_type = "proxy_error"
            message = "Proxy connection timeout or IP blocked"
        elif "Pinterest error" in message:
            error_type = "api_error"

        task.error_json = {
            "error": error_type,
            "message": message
        }

    task.save()

@shared_task
def refresh_all_accounts_boards_task():
    active_accounts = PinterestAccount.objects.filter(is_active=True)
    logger.info(f"Starting scheduled board refresh for {active_accounts.count()} accounts")
    for account in active_accounts:
        try:
            refresh_boards(account)
        except Exception as e:
            logger.error(f"Failed to refresh boards for account {account.name}: {e}")
