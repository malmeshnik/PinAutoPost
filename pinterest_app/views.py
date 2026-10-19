import logging
import hashlib
import json
import uuid
from time import monotonic, sleep

from django.http import HttpResponse
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.urls import reverse
from rest_framework import permissions, status, viewsets
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from .models import PinterestAccount, PinPublishTask
from .serializers import (
    PinterestAccountSerializer,
    PinterestBoardSerializer,
    PinPublishSerializer,
)
from .services import (
    ERROR_HTTP_STATUS,
    PUBLISH_TIMEOUT_SECONDS,
    PinterestServiceError,
    check_proxy,
    fail_pending_task,
    publish_pin_for_task,
    recover_stale_processing_tasks,
    refresh_boards,
    reset_retryable_task,
)
from .tasks import publish_pin_task

logger = logging.getLogger(__name__)

ERROR_MESSAGES = {
    "auth_error": "Pinterest cookies expired. Account deactivated.",
    "proxy_error": (
        "З'єднання через проксі не пройшло. Перевірте адресу, порт, тип проксі, "
        "облікові дані та доступність сервісу у провайдера."
    ),
    "api_error": "Pinterest could not complete the request.",
    "validation_error": "The publish request is invalid.",
    "no_boards": "Pinterest returned no boards for this account.",
    "internal_error": "An unexpected error occurred while publishing the pin.",
    "timeout_error": (
        f"The {PUBLISH_TIMEOUT_SECONDS}-second Pinterest request deadline was exceeded."
    ),
    "publish_unknown": (
        "Pinterest may have received the pin. Check the board before publishing "
        "it again with a new Idempotency-Key."
    ),
    "queue_error": "The publish queue is unavailable. Retry with the same Idempotency-Key.",
}

ACCOUNT_CREATE_ERROR_MESSAGES = {
    "auth_error": (
        "Pinterest rejected these cookies. Log in to pinterest.com again, export "
        "fresh cookies and retry. The account was not saved."
    ),
    "no_boards": (
        "Pinterest returned no boards for this account. Create at least one board "
        "and retry. The account was not saved."
    ),
}


def _error_status(error_code):
    if error_code in ERROR_HTTP_STATUS:
        return ERROR_HTTP_STATUS[error_code]
    if error_code in {"internal_error", "unknown_error"}:
        return status.HTTP_500_INTERNAL_SERVER_ERROR
    return status.HTTP_400_BAD_REQUEST


def _request_fingerprint(data):
    # Omitted optional fields, explicit nulls and empty strings mean the same
    # request, so they must not change the fingerprint.
    normalized = {key: value for key, value in data.items() if value not in (None, "")}
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _wait_for_task(task, deadline):
    while task.status in {"PENDING", "PROCESSING"} and monotonic() < deadline:
        sleep(min(0.25, max(0, deadline - monotonic())))
        task.refresh_from_db()
    return task


def _accepted_response(request, task):
    return Response(
        {
            "status": "accepted",
            "task_id": str(task.id),
            "status_url": _task_status_url(request, task),
        },
        status=status.HTTP_202_ACCEPTED,
    )


def _enqueue_task(request, task):
    if task.status == "PENDING":
        try:
            async_result = publish_pin_task.delay(task.id)
        except Exception:
            logger.error("Could not queue publish task %s.", task.id)
            fail_pending_task(task, "queue_error", ERROR_MESSAGES["queue_error"])
            return _task_response(task, request)
        PinPublishTask.objects.filter(pk=task.pk).update(celery_task_id=async_result.id)
    return _accepted_response(request, task)


def _task_status_url(request, task):
    path = reverse(
        "task-status",
        kwargs={"status_token": task.status_token},
    )
    return request.build_absolute_uri(path)


def _stored_task_response(request, task):
    response = HttpResponse(
        content=task.response_body or b"",
        status=task.response_status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
        content_type=task.response_content_type or "application/json",
    )
    response["X-PinAutoPost-Task-ID"] = str(task.id)
    response["X-PinAutoPost-Status-URL"] = _task_status_url(request, task)
    return response


def _idempotency_error(code, message, http_status):
    return Response(
        {"error": code, "message": message},
        status=http_status,
    )


def _task_response(task, request=None):
    response_data = {"task_id": str(task.id)}
    if request is not None:
        response_data["status_url"] = _task_status_url(request, task)

    if task.status == "PENDING":
        response_data["status"] = "pending"
        return Response(response_data, status=status.HTTP_200_OK)
    if task.status == "PROCESSING":
        response_data["status"] = "processing"
        return Response(response_data, status=status.HTTP_200_OK)
    if task.status == "SUCCESS":
        response_data.update(
            {"status": "success", "message": "Pin published successfully"}
        )
        return Response(response_data, status=status.HTTP_200_OK)

    error_data = task.error_json
    if not isinstance(error_data, dict):
        error_data = {}
    error_code = error_data.get("error", "unknown_error")
    if not isinstance(error_code, str):
        error_code = "unknown_error"
    error_message = error_data.get("message")
    if (
        error_code != "proxy_error"
        or not isinstance(error_message, str)
        or not error_message
    ):
        error_message = ERROR_MESSAGES.get(
            error_code, "An unexpected error occurred while publishing the pin."
        )
    response_data.update(
        {
            "status": "failed",
            "error": error_code,
            "message": error_message,
        }
    )
    retry_count = error_data.get("retry_count")
    if isinstance(retry_count, int):
        response_data["retry_count"] = retry_count
    return Response(response_data, status=_error_status(error_code))


class ApiTokenView(ObtainAuthToken):
    """
    POST username/password → {"token": ...} for the "Authorization: Token"
    header. Returns the existing token if the user already has one.
    """
    throttle_scope = "auth_token"
    throttle_classes = [ScopedRateThrottle]


class PinterestAccountViewSet(viewsets.ModelViewSet):
    serializer_class = PinterestAccountSerializer

    def get_queryset(self):
        queryset = PinterestAccount.objects.prefetch_related("boards")
        if not self.request.user.is_superuser:
            queryset = queryset.filter(user=self.request.user)
        return queryset

    def perform_create(self, serializer):
        proxy = serializer.validated_data["proxy"]
        try:
            check_proxy(proxy)
        except PinterestServiceError as exc:
            raise ValidationError(
                {"proxy": exc.message}
            ) from exc

        # The account is only kept if Pinterest accepts its cookies, so a
        # failed attempt can be retried without leaving inactive duplicates.
        try:
            with transaction.atomic():
                account = serializer.save(user=self.request.user)
                refresh_boards(account)
        except PinterestServiceError as exc:
            logger.warning(
                "Account validation failed for a new account (%s).", exc.code
            )
            raise ValidationError(
                {
                    "account": {
                        "error": exc.code,
                        "message": ACCOUNT_CREATE_ERROR_MESSAGES.get(
                            exc.code, ERROR_MESSAGES.get(exc.code, exc.message)
                        ),
                    }
                }
            ) from exc

    def perform_update(self, serializer):
        proxy = serializer.validated_data.get("proxy")
        if proxy is not None:
            try:
                check_proxy(proxy)
            except PinterestServiceError as exc:
                raise ValidationError({"proxy": exc.message}) from exc
        serializer.save()

    @action(detail=True, methods=["get"])
    def boards(self, request, pk=None):
        account = self.get_object()
        refresh_value = request.query_params.get("refresh")
        if refresh_value is not None and refresh_value.lower() not in {"true", "false"}:
            return Response(
                {"error": "invalid_refresh", "message": "'refresh' must be true or false."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if refresh_value and refresh_value.lower() == "true":
            try:
                refresh_boards(account)
            except PinterestServiceError as exc:
                logger.warning(
                    "Board refresh failed for account %s (%s).",
                    account.id,
                    exc.code,
                )
                return Response(
                    {
                        "error": exc.code,
                        "message": ERROR_MESSAGES.get(exc.code, exc.message),
                    },
                    status=_error_status(exc.code),
                )

        boards = account.boards.all()
        return Response(PinterestBoardSerializer(boards, many=True).data)


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def publish_pin_webhook(request, webhook_token):
    account = get_object_or_404(PinterestAccount, webhook_token=webhook_token)
    if not account.is_active:
        return Response(
            {"error": "account_inactive", "message": "Account is inactive."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = PinPublishSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    wait_value = request.query_params.get("wait", "true").strip().lower()
    if wait_value not in {"true", "false"}:
        return Response(
            {"error": "invalid_wait", "message": "'wait' must be true or false."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    wait_for_result = wait_value == "true"

    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if len(idempotency_key) > 255:
        return _idempotency_error(
            "invalid_idempotency_key",
            "The Idempotency-Key header must contain at most 255 characters.",
            status.HTTP_400_BAD_REQUEST,
        )
    if not idempotency_key:
        # Clients that do not send a key get a unique one, i.e. no deduplication.
        idempotency_key = f"auto:{uuid.uuid4()}"

    data = serializer.validated_data
    request_fingerprint = _request_fingerprint(data)
    try:
        with transaction.atomic():
            task = PinPublishTask.objects.filter(
                account=account,
                idempotency_key=idempotency_key,
            ).first()
            is_new_task = task is None
            if is_new_task:
                task = PinPublishTask.objects.create(
                    account=account,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    **data,
                )
    except IntegrityError:
        task = PinPublishTask.objects.get(
            account=account,
            idempotency_key=idempotency_key,
        )
        is_new_task = False

    if task.request_fingerprint != request_fingerprint:
        return _idempotency_error(
            "idempotency_key_conflict",
            "This Idempotency-Key was already used with a different request body.",
            status.HTTP_409_CONFLICT,
        )

    if not is_new_task:
        # A process that died mid-publish leaves the task PROCESSING forever.
        recover_stale_processing_tasks(PinPublishTask.objects.filter(pk=task.pk))
        task.refresh_from_db()
        # Failures that happened before Pinterest got the pin may be retried.
        if task.status == "FAILED":
            reset_retryable_task(task)
        if task.status in {"SUCCESS", "FAILED"}:
            if wait_for_result:
                return _stored_task_response(request, task)
            return _task_response(task, request)

    if not wait_for_result:
        return _enqueue_task(request, task)

    deadline = monotonic() + PUBLISH_TIMEOUT_SECONDS
    if task.status == "PENDING":
        publish_pin_for_task(task, deadline=deadline)
    task = _wait_for_task(task, deadline)
    if task.response_status_code is None:
        return _idempotency_error(
            "request_in_progress",
            "A request with this Idempotency-Key is still running. Retry the same key.",
            status.HTTP_409_CONFLICT,
        )
    return _stored_task_response(request, task)


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def get_task_status(request, status_token):
    task = get_object_or_404(PinPublishTask, status_token=status_token)
    return _task_response(task)


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def get_legacy_task_status(request, task_id):
    """
    Integer-id status route kept for clients of the old API. Task ids can be
    enumerated, so it only exposes the status and error code, never messages.
    """
    task = get_object_or_404(PinPublishTask, id=task_id)
    response_data = {"task_id": str(task.id), "status": task.status.lower()}
    if task.status == "FAILED":
        error_data = task.error_json if isinstance(task.error_json, dict) else {}
        error_code = error_data.get("error")
        response_data["error"] = error_code if isinstance(error_code, str) else "unknown_error"
    return Response(response_data, status=status.HTTP_200_OK)
