from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from .models import PinterestAccount, PinterestBoard
from .serializers import PinterestAccountSerializer, PinterestBoardSerializer, PinPublishSerializer
from .services import refresh_boards, create_pin, check_proxy

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
        success, message = refresh_boards(account)
        if not success:
            # If initial board refresh fails (e.g. auth error), we still created the account
            # but it will be inactive. We can inform the user.
            pass

    @action(detail=True, methods=['get'])
    def boards(self, request, pk=None):
        account = self.get_object()
        if request.query_params.get('refresh') == 'true':
            success, message = refresh_boards(account)
            if not success:
                return Response({"error": message}, status=status.HTTP_502_BAD_GATEWAY)

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
    board_id = data.get('board_id')
    board_name = data.get('board_name')

    if not board_id and board_name:
        board = account.boards.filter(name__iexact=board_name).first()
        if not board:
            return Response({"error": f"Board with name '{board_name}' not found"}, status=status.HTTP_400_BAD_REQUEST)
        board_id = board.board_id

    # Final check: if we have board_id but it's not in our cached boards?
    # Usually we trust the board_id if provided directly.

    success, message = create_pin(
        account=account,
        title=data.get('title', ''),
        description=data.get('description', ''),
        link=data.get('link', ''),
        image_url=data.get('image_url'),
        board_id=board_id
    )

    if success:
        return Response({"status": "success", "message": "Pin published successfully"}, status=status.HTTP_200_OK)
    else:
        # If it was a proxy error, create_pin already deactivated the account
        return Response({"status": "error", "message": message}, status=status.HTTP_502_BAD_GATEWAY if "Proxy" in message else status.HTTP_400_BAD_REQUEST)
