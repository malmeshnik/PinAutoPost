from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from .models import PinterestAccount, PinterestBoard, PinPublishTask
from .serializers import PinterestAccountSerializer, PinterestBoardSerializer, PinPublishSerializer, PinPublishTaskSerializer
from .services import refresh_boards, check_proxy
from .tasks import publish_pin_task

class PinterestAccountViewSet(viewsets.ModelViewSet):
    serializer_class = PinterestAccountSerializer

    def get_queryset(self):
        if self.request.user.is_superuser:
            return PinterestAccount.objects.all()
        return PinterestAccount.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        from rest_framework.exceptions import ValidationError
        import logging

        logger = logging.getLogger(__name__)

        # Proxy validation on creation
        proxy = self.request.data.get('proxy')
        if proxy and not check_proxy(proxy):
            logger.error(f"✗ Proxy validation failed for: {proxy}")
            raise ValidationError({"proxy": _("Proxy is unavailable or invalid. Check format: http://user:pass@ip:port")})

        cookies = self.request.data.get('cookies')
        if not cookies:
            raise ValidationError({"cookies": _("Cookies are required for account creation")})

        # Validate cookies structure
        if isinstance(cookies, dict):
            if "cookies" not in cookies and "url" not in cookies:
                raise ValidationError({"cookies": _("Invalid cookies structure. Expected: {\"url\": \"...\", \"cookies\": [...]}")})
        elif not isinstance(cookies, list):
            raise ValidationError({"cookies": _("Cookies must be an array or an object with url and cookies fields")})

        account = serializer.save(user=self.request.user)
        logger.info(f"✓ Account {account.name} created, starting board refresh...")

        # Initial boards refresh to validate account
        try:
            refresh_boards(account)
            boards_count = account.boards.count()

            if boards_count == 0:
                account.is_active = False
                account.save()
                logger.error(f"✗ No boards found for account {account.name}, marking as inactive")
                raise ValidationError({
                    "account": _("Account created, but failed to retrieve boards. "
                                 "Check cookies and try again. Account deactivated.")
                })

            logger.info(f"✓ Account {account.name} validated successfully with {boards_count} boards")
        except Exception as e:
            account.is_active = False
            account.save()
            logger.error(f"✗ Failed to validate account {account.name}: {e}")
            raise ValidationError({
                "account": _("Error during account validation: {}. Account deactivated.").format(str(e))
            })

    @action(detail=True, methods=['get'])
    def boards(self, request, pk=None):
        import logging
        logger = logging.getLogger(__name__)

        account = self.get_object()
        if request.query_params.get('refresh') == 'true':
            logger.info(f"Manual board refresh requested for account {account.name}")
            try:
                refresh_boards(account)
                boards_count = account.boards.count()

                if boards_count == 0:
                    logger.warning(f"⚠ No boards found after refresh for account {account.name}")
                    return Response({
                        "error": "no_boards",
                        "message": _("Failed to get boards for account {}. Check cookies and proxy.").format(account.name)
                    }, status=status.HTTP_400_BAD_REQUEST)

                logger.info(f"✓ Refreshed {boards_count} boards for account {account.name}")
            except Exception as e:
                logger.error(f"✗ Error refreshing boards for account {account.name}: {e}")
                return Response({
                    "error": "refresh_failed",
                    "message": _("Error while refreshing boards: {}").format(str(e))
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        boards = account.boards.all()
        serializer = PinterestBoardSerializer(boards, many=True)
        return Response(serializer.data)

@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def publish_pin_webhook(request, webhook_token):
    import logging
    logger = logging.getLogger(__name__)

    account = get_object_or_404(PinterestAccount, webhook_token=webhook_token)

    if not account.is_active:
        logger.warning(f"⚠ Attempt to publish to inactive account: {account.name}")
        return Response({"error": "account_inactive", "message": "Account is inactive"}, status=status.HTTP_400_BAD_REQUEST)

    serializer = PinPublishSerializer(data=request.data)
    if not serializer.is_valid():
        logger.error(f"✗ Invalid publish request data: {serializer.errors}")
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data

    # Create Task
    task = PinPublishTask.objects.create(
        account=account,
        title=data.get('title', ''),
        description=data.get('description', ''),
        link=data.get('link', ''),
        image_url=data.get('image_url'),
        board_id=data.get('board_id'),
        board_name=data.get('board_name'),
        status='PENDING'
    )

    logger.info(f"📌 Created publish task {task.id} for account {account.name}")

    # Check if client wants synchronous response
    wait_for_result = request.query_params.get('wait', 'false').lower() == 'true'
    timeout = int(request.query_params.get('timeout', '60'))  # Default 60 seconds

    # Trigger Celery
    celery_task = publish_pin_task.delay(task.id)
    task.celery_task_id = celery_task.id
    task.save()

    if wait_for_result:
        logger.info(f"⏳ Client requested synchronous response, waiting up to {timeout}s...")
        try:
            # Wait for task completion
            import time
            start_time = time.time()
            while time.time() - start_time < timeout:
                task.refresh_from_db()

                if task.status in ['SUCCESS', 'FAILED']:
                    logger.info(f"✓ Task {task.id} completed with status: {task.status}")
                    break

                time.sleep(0.5)

            # Return final status
            task.refresh_from_db()

            if task.status == 'SUCCESS':
                return Response({
                    "status": "success",
                    "message": "Pin published successfully",
                    "task_id": str(task.id)
                }, status=status.HTTP_200_OK)

            elif task.status == 'FAILED':
                error_data = task.error_json or {"error": "unknown_error", "message": "An unknown error occurred"}

                http_status = status.HTTP_400_BAD_REQUEST
                if error_data.get('error') == 'auth_error':
                    http_status = status.HTTP_401_UNAUTHORIZED
                elif error_data.get('error') == 'proxy_error':
                    http_status = status.HTTP_502_BAD_GATEWAY

                return Response({
                    **error_data,
                    "task_id": str(task.id)
                }, status=http_status)

            else:
                # Still processing after timeout
                logger.warning(f"⏱ Task {task.id} timed out after {timeout}s, status: {task.status}")
                return Response({
                    "status": "timeout",
                    "message": f"Task is still processing after {timeout}s. Check status at /api/v1/task/{task.id}/",
                    "task_id": str(task.id)
                }, status=status.HTTP_202_ACCEPTED)

        except Exception as e:
            logger.error(f"✗ Error while waiting for task {task.id}: {e}")
            return Response({
                "status": "error",
                "message": f"Error while processing: {str(e)}",
                "task_id": str(task.id)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    else:
        # Async response
        logger.info(f"✓ Task {task.id} queued for async processing")
        return Response({
            "status": "accepted",
            "task_id": str(task.id),
            "status_url": f"/api/v1/task/{task.id}/"
        }, status=status.HTTP_202_ACCEPTED)

@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def get_task_status(request, task_id):
    task = get_object_or_404(PinPublishTask, id=task_id)

    if task.status == 'PENDING' or task.status == 'PROCESSING':
        return Response({"status": task.status.lower()}, status=status.HTTP_200_OK)

    if task.status == 'SUCCESS':
        return Response({"status": "success", "message": "Pin published successfully"}, status=status.HTTP_200_OK)

    if task.status == 'FAILED':
        error_data = task.error_json or {"error": "unknown_error", "message": "An unknown error occurred"}

        http_status = status.HTTP_400_BAD_REQUEST
        if error_data.get('error') == 'auth_error':
            http_status = status.HTTP_401_UNAUTHORIZED
        elif error_data.get('error') == 'proxy_error':
            http_status = status.HTTP_502_BAD_GATEWAY

        return Response(error_data, status=http_status)

    return Response({"status": "unknown"}, status=status.HTTP_400_BAD_REQUEST)
