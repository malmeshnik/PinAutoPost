from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
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
        # Proxy validation on creation
        proxy = self.request.data.get('proxy')
        if proxy and not check_proxy(proxy):
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"proxy": "Proxy is unreachable or invalid."})

        account = serializer.save(user=self.request.user)
        # Initial boards refresh
        refresh_boards(account)

    @action(detail=True, methods=['get'])
    def boards(self, request, pk=None):
        account = self.get_object()
        if request.query_params.get('refresh') == 'true':
            refresh_boards(account)

        boards = account.boards.all()
        serializer = PinterestBoardSerializer(boards, many=True)
        return Response(serializer.data)

@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def publish_pin_webhook(request, webhook_token):
    account = get_object_or_404(PinterestAccount, webhook_token=webhook_token)

    if not account.is_active:
        return Response({"error": "Account is inactive"}, status=status.HTTP_400_BAD_REQUEST)

    serializer = PinPublishSerializer(data=request.data)
    if not serializer.is_valid():
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

    # Trigger Celery
    celery_task = publish_pin_task.delay(task.id)
    task.celery_task_id = celery_task.id
    task.save()

    return Response({
        "status": "accepted",
        "task_id": str(task.id)
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
