import json
import logging
from datetime import timedelta
from time import monotonic, sleep, time
from html import escape
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from curl_cffi import requests as curl_requests
from .models import PinterestAccount, PinterestBoard, PinPublishTask

logger = logging.getLogger(__name__)
PUBLISH_TIMEOUT_SECONDS = 60
PINTEREST_TITLE_MAX_LENGTH = 100


class PinterestServiceError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _deactivate_account(account):
    # An account that is still being validated (not saved yet) has nothing to update.
    if account.pk and account.is_active:
        account.is_active = False
        account.save(update_fields=["is_active", "updated_at"])


def send_telegram_alert(message):
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_ADMIN_CHAT_ID
    if not token or not chat_id:
        logger.warning("Telegram credentials not configured.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        logger.warning("Failed to send Telegram alert.")
        return False
    return True


def describe_proxy_failure(error):
    details = str(error).lower()
    if (
        "user was rejected by the socks5 server" in details
        or "proxy authentication required" in details
        or "authentication failed" in details
        or "http 407" in details
    ):
        return (
            "Проксі відхилив авторизацію. Перевірте логін і пароль у провайдера. "
            "Якщо вони містять спеціальні символи (@ : / # %), закодуйте їх для URL."
        )
    if (
        "could not resolve proxy" in details
        or "name or service not known" in details
        or "nodename nor servname provided" in details
        or "temporary failure in name resolution" in details
    ):
        return (
            "Не вдалося знайти адресу проксі за іменем хоста. Перевірте hostname на "
            "помилки та звірте його з даними провайдера."
        )
    if "timed out" in details or "timeout" in details:
        return (
            "Час очікування відповіді проксі вичерпано. Перевірте, чи сервер працює, "
            "чи правильні hostname і порт, та чи дозволяє провайдер підключення з "
            "вашого сервера."
        )
    if (
        "cannot complete socks5 connection" in details
        or "connection refused" in details
        or "could not connect to proxy" in details
        or "failed to connect to" in details
    ):
        return (
            "Не вдалося підключитися до проксі. Перевірте hostname, порт і тип "
            "проксі (HTTP або SOCKS5), а також статус сервісу у провайдера."
        )
    return (
        "Перевірка з'єднання через проксі не пройшла. Перевірте адресу, порт, тип "
        "проксі, облікові дані та доступність сервісу у провайдера."
    )


def check_proxy(proxy_url, timeout=5):
    try:
        response = curl_requests.get(
            "https://api.ipify.org?format=json",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=timeout,
            impersonate="chrome120",
        )
    except Exception as exc:
        message = describe_proxy_failure(exc)
        logger.warning("Proxy connectivity check failed: %s", message)
        raise PinterestServiceError(
            "proxy_error", message
        ) from None
    if response.status_code == 200:
        return True
    if response.status_code == 407:
        message = describe_proxy_failure("HTTP 407")
    else:
        message = (
            f"Перевірка проксі повернула HTTP {response.status_code}. Перевірте "
            "доступність сервера та правила доступу у провайдера."
        )
    logger.warning("Proxy connectivity check failed: %s", message)
    raise PinterestServiceError("proxy_error", message)


def extract_base_url(cookies_data):
    """
    Extracts base URL from cookies structure.
    Supports both {"url": "...", "cookies": [...]} and [...] formats.
    Returns the base domain (e.g., www.pinterest.com or ru.pinterest.com).
    """
    if isinstance(cookies_data, dict):
        cookie_url = cookies_data.get("url")
        if cookie_url:
            if not isinstance(cookie_url, str):
                raise ValueError("Cookie URL must be a Pinterest domain URL.")
            try:
                parsed = urlparse(cookie_url)
                hostname = (parsed.hostname or "").lower()
            except ValueError as exc:
                raise ValueError("Cookie URL must be a Pinterest domain URL.") from exc
            if (
                parsed.scheme not in {"http", "https"}
                or not hostname
                or (hostname != "pinterest.com" and not hostname.endswith(".pinterest.com"))
            ):
                raise ValueError("Cookie URL must point to a Pinterest domain.")
            return hostname

    return "www.pinterest.com"


def get_session_and_csrf(account: PinterestAccount):
    """
    Creates a curl_cffi session, sets cookies and extracts CSRF token.
    Also extracts base URL from cookies for API requests.
    """
    logger.info(f"Setting up session for account: {account.name}")

    session = curl_requests.Session(impersonate="chrome120")
    session.proxies = {"http": account.proxy, "https": account.proxy}
    csrf_token = ""

    # Extract base URL
    try:
        base_url = extract_base_url(account.cookies)
    except ValueError as exc:
        raise PinterestServiceError("validation_error", str(exc)) from None

    cookies_data = account.cookies
    if isinstance(cookies_data, dict) and "cookies" in cookies_data:
        cookies_data = cookies_data["cookies"]

    if not isinstance(cookies_data, list) or not cookies_data:
        raise PinterestServiceError("validation_error", "Account cookies are missing or invalid.")

    cookies_count = 0
    for cookie in cookies_data:
        if (
            not isinstance(cookie, dict)
            or not isinstance(cookie.get("name"), str)
            or not cookie["name"]
            or not isinstance(cookie.get("value", ""), str)
            or not isinstance(cookie.get("domain", ".pinterest.com"), str)
            or not isinstance(cookie.get("path", "/"), str)
        ):
            raise PinterestServiceError("validation_error", "Account cookies are malformed.")
        name = cookie["name"]
        value = cookie.get("value", "")
        domain = cookie.get("domain", ".pinterest.com")
        path = cookie.get("path", "/")

        try:
            session.cookies.set(name, value, domain=domain, path=path)
        except (TypeError, ValueError):
            raise PinterestServiceError(
                "validation_error", "Account cookies are malformed."
            ) from None
        cookies_count += 1

        if name == "csrftoken":
            csrf_token = value
    logger.info(
        "Session configured for account %s with %s cookies and CSRF token %s.",
        account.id,
        cookies_count,
        "present" if csrf_token else "missing",
    )

    if not csrf_token:
        _deactivate_account(account)
        raise PinterestServiceError("auth_error", "Pinterest CSRF token is missing.")

    return session, csrf_token, base_url


def refresh_boards(account: PinterestAccount, deadline=None, timeout=30):
    boards = fetch_boards(account, deadline=deadline, timeout=timeout)
    replace_boards(account, boards)
    logger.info("Refreshed %s boards for account %s.", len(boards), account.id)
    return len(boards)


def replace_boards(account, boards):
    with transaction.atomic():
        PinterestBoard.objects.filter(account=account).delete()
        PinterestBoard.objects.bulk_create(
            PinterestBoard(account=account, **board) for board in boards
        )


def fetch_boards(account: PinterestAccount, deadline=None, timeout=30):
    """
    Loads the account boards from Pinterest without writing to the database,
    so an account can be validated before it is saved.
    Returns a list of {"board_id", "name", "url"} dicts.
    """
    session, csrf_token, base_url = get_session_and_csrf(account)
    url = f"https://{base_url}/resource/BoardsResource/get/"

    headers = {
        "x-csrftoken": csrf_token if csrf_token else "",
        "x-requested-with": "XMLHttpRequest",
        "accept": "application/json, text/javascript, */*; q=0.01",
        "accept-language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    options = {
        "username": account.name,
    }
    params = {
        "data": json.dumps({"options": options, "context": {}}),
        "_": str(int(time() * 1000)),
    }

    logger.info("Refreshing boards for account %s.", account.id)

    try:
        response = session.post(
            url,
            headers=headers,
            params=params,
            timeout=_request_timeout(deadline, timeout),
        )
    except PinterestServiceError:
        raise
    except Exception as exc:
        message = describe_proxy_failure(exc)
        logger.warning(
            "Board refresh request failed for account %s: %s.",
            account.id,
            message,
        )
        if deadline is not None and monotonic() >= deadline:
            raise PinterestServiceError(
                "timeout_error",
                f"The {PUBLISH_TIMEOUT_SECONDS}-second Pinterest request deadline was exceeded.",
            ) from None
        raise PinterestServiceError("proxy_error", message) from None
    finally:
        session.close()

    if response.status_code == 401:
        if account.pk:
            _deactivate_account(account)
            send_telegram_alert(
                f"🚫 Pinterest authentication failed for account <b>{escape(account.name)}</b>."
            )
        raise PinterestServiceError("auth_error", "Pinterest cookies expired. Account deactivated.")
    if response.status_code != 200:
        code = "proxy_error" if response.status_code >= 500 else "api_error"
        raise PinterestServiceError(code, "Pinterest could not load account boards.")
    try:
        response_data = response.json()
    except ValueError:
        raise PinterestServiceError("api_error", "Pinterest returned an invalid response.") from None

    if not isinstance(response_data, dict):
        raise PinterestServiceError("api_error", "Pinterest returned malformed board data.")
    resource_response = response_data.get("resource_response")
    if not isinstance(resource_response, dict):
        raise PinterestServiceError("api_error", "Pinterest returned malformed board data.")
    boards = resource_response.get("data", [])
    if not isinstance(boards, list) or not boards:
        raise PinterestServiceError("no_boards", "Pinterest returned no boards for this account.")

    valid_boards = [
        dict(
            board_id=str(board["id"]),
            name=str(board["name"]),
            url=str(board.get("url", "")),
        )
        for board in boards
        if isinstance(board, dict) and board.get("id") and board.get("name")
    ]
    if not valid_boards:
        raise PinterestServiceError("api_error", "Pinterest returned malformed board data.")
    return valid_boards

def get_signed_pinterest_url(session, csrf_token, original_image_url, deadline=None):
    """
    Робить запит до FindPinImagesResource і повертає підписаний проксі-лінк 
    із домену i.pinimgproxy.com (із сигнатурою sig).
    Якщо запит упав, повертає None, щоб код міг зробити фолбек на оригінальний URL.
    """
    url = "https://www.pinterest.com/resource/FindPinImagesResource/get/"
    
    data = {
        "options": {
            "url": original_image_url,
        },
    }

    headers = {
        "x-requested-with": "XMLHttpRequest",
        "x-csrftoken": csrf_token,
        "referer": "https://www.pinterest.com/pin-builder/?tab=save_from_url",
    }

    try:
        response = session.get(
            url,
            params={"source_url": "/pin-builder/?tab=save_from_url", "data": json.dumps(data)},
            headers=headers,
            timeout=_request_timeout(deadline, 15),
        )

        if response.status_code == 200:
            res_json = response.json()
            items = res_json.get("resource_response", {}).get("data", {}).get("items", [])
            if items:
                signed_url = items[0].get("url")
                return signed_url
            
        logger.warning("Pinterest did not return a signed image URL; using the original URL.")
    except Exception:
        logger.warning("Could not request a signed image URL; using the original URL.")
        
    return None


def _request_timeout(deadline, maximum):
    if deadline is None:
        return maximum
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise PinterestServiceError(
            "timeout_error",
            f"The {PUBLISH_TIMEOUT_SECONDS}-second Pinterest request deadline was exceeded.",
        )
    return min(maximum, remaining)


def resolve_board_id(account, board_id, board_name, deadline=None):
    """
    Resolves the board from the locally cached list first and refreshes the
    cache from Pinterest only when the board is not found there.
    """
    try:
        return _find_cached_board_id(account, board_id, board_name)
    except PinterestServiceError as exc:
        if exc.code != "board_not_found":
            raise
    refresh_boards(account, deadline=deadline)
    try:
        return _find_cached_board_id(account, board_id, board_name)
    except PinterestServiceError as exc:
        if exc.code != "board_not_found":
            raise
        message = (
            "The requested board does not belong to this account."
            if board_id
            else "The requested board was not found."
        )
        raise PinterestServiceError("validation_error", message) from None


def _find_cached_board_id(account, board_id, board_name):
    if board_id:
        board = account.boards.filter(board_id=board_id).first()
        if not board:
            raise PinterestServiceError("board_not_found", "Board is not cached.")
        return board.board_id
    if not board_name:
        raise PinterestServiceError(
            "validation_error", "A board ID or board name is required."
        )

    board_ids = list(
        account.boards.filter(name__iexact=board_name).values_list("board_id", flat=True)[:2]
    )
    if not board_ids:
        raise PinterestServiceError("board_not_found", "Board is not cached.")
    if len(board_ids) > 1:
        raise PinterestServiceError(
            "validation_error", "The board name is ambiguous; provide a board ID."
        )
    return board_ids[0]


# Errors raised before the pin request reached Pinterest, or explicit 5xx
# answers from Pinterest. Repeating them cannot create a duplicate pin.
RETRYABLE_ERROR_CODES = frozenset({"proxy_error", "timeout_error", "queue_error"})
MAX_PUBLISH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (2, 5)
# A PROCESSING task older than this lost its process (killed mid-publish).
STALE_PROCESSING_AFTER = timedelta(seconds=PUBLISH_TIMEOUT_SECONDS + 60)

ERROR_HTTP_STATUS = {
    "auth_error": 401,
    "proxy_error": 502,
    "timeout_error": 504,
    "publish_unknown": 504,
    "api_error": 502,
    "validation_error": 400,
    "no_boards": 400,
    "queue_error": 503,
}


def _failure_fields(code, message, attempts=1):
    error_json = {
        "error": code,
        "message": message,
        "retryable": code in RETRYABLE_ERROR_CODES,
    }
    if attempts > 1:
        error_json["retry_count"] = attempts - 1
    return {
        "status": "FAILED",
        "error_json": error_json,
        "response_status_code": ERROR_HTTP_STATUS.get(code, 500),
        "response_content_type": "application/json",
        "response_body": json.dumps(error_json).encode("utf-8"),
        "updated_at": timezone.now(),
    }


def fail_pending_task(task, code, message):
    """Marks a task that was never claimed (e.g. could not be queued) as failed."""
    PinPublishTask.objects.filter(pk=task.pk, status="PENDING").update(
        **_failure_fields(code, message)
    )
    task.refresh_from_db()
    return task


def reset_retryable_task(task):
    """Moves a task that failed with a retryable error back to PENDING."""
    reset = PinPublishTask.objects.filter(
        pk=task.pk,
        status="FAILED",
        error_json__retryable=True,
    ).update(
        status="PENDING",
        error_json=None,
        response_status_code=None,
        response_content_type=None,
        response_body=None,
        updated_at=timezone.now(),
    )
    task.refresh_from_db()
    return bool(reset)


def recover_stale_processing_tasks(queryset=None):
    """
    Fails tasks whose publishing process died while they were PROCESSING.
    The pin request may already have reached Pinterest, so they are marked
    publish_unknown instead of being published again.
    """
    queryset = PinPublishTask.objects.all() if queryset is None else queryset
    cutoff = timezone.now() - STALE_PROCESSING_AFTER
    stale_ids = list(
        queryset.filter(status="PROCESSING", updated_at__lt=cutoff).values_list(
            "pk", flat=True
        )
    )
    recovered = 0
    for task_id in stale_ids:
        recovered += PinPublishTask.objects.filter(
            pk=task_id, status="PROCESSING", updated_at__lt=cutoff
        ).update(
            **_failure_fields(
                "publish_unknown",
                "Процес публікації перервався. Pinterest міг уже прийняти пін — "
                "перевірте дошку, перш ніж публікувати його з новим Idempotency-Key.",
            )
        )
    if recovered:
        logger.warning("Marked %s stale PROCESSING tasks as publish_unknown.", recovered)
    return recovered


def _has_time_for_retry(deadline, backoff):
    # Leave room for at least one more short request after the pause.
    return deadline is None or deadline - monotonic() > backoff + 10


def _publish_attempt(task, account, deadline):
    if task.title and len(task.title) > PINTEREST_TITLE_MAX_LENGTH:
        raise PinterestServiceError(
            "validation_error",
            f"Pin title must be {PINTEREST_TITLE_MAX_LENGTH} characters or fewer.",
        )
    account.refresh_from_db(fields=["is_active"])
    if not account.is_active:
        raise PinterestServiceError("auth_error", "Pinterest account is inactive.")
    check_proxy(account.proxy, timeout=_request_timeout(deadline, 5))

    board_id = resolve_board_id(
        account, task.board_id, task.board_name, deadline=deadline
    )
    if board_id != task.board_id:
        task.board_id = board_id
        task.save(update_fields=["board_id", "updated_at"])

    return create_pin(
        account=account,
        title=task.title,
        description=task.description,
        link=task.link,
        image_url=task.image_url,
        board_id=board_id,
        deadline=deadline,
    )


def publish_pin_for_task(task, deadline=None):
    claimed = PinPublishTask.objects.filter(
        pk=task.pk,
        status="PENDING",
        response_status_code__isnull=True,
    ).update(status="PROCESSING", error_json=None, updated_at=timezone.now())
    task.refresh_from_db()
    if not claimed:
        return task

    account = task.account
    attempt = 0
    while True:
        attempt += 1
        response = None
        try:
            response = _publish_attempt(task, account, deadline)
        except PinterestServiceError as exc:
            error = exc
        except Exception:
            logger.exception("Unexpected error while publishing task %s.", task.id)
            error = PinterestServiceError(
                "internal_error",
                "An unexpected error occurred while publishing the pin.",
            )
        else:
            error = classify_pin_response(response)

        if error is None or error.code not in RETRYABLE_ERROR_CODES:
            break
        if attempt >= MAX_PUBLISH_ATTEMPTS:
            break
        backoff = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS)) - 1]
        if not _has_time_for_retry(deadline, backoff):
            break
        logger.info(
            "Retrying task %s after %s (attempt %s of %s).",
            task.id,
            error.code,
            attempt + 1,
            MAX_PUBLISH_ATTEMPTS,
        )
        PinPublishTask.objects.filter(pk=task.pk).update(updated_at=timezone.now())
        sleep(backoff)

    if response is None:
        fields = _failure_fields(error.code, error.message, attempts=attempt)
    else:
        fields = {
            "status": "FAILED" if error else "SUCCESS",
            "error_json": None,
            "response_status_code": response.status_code,
            "response_content_type": response.headers.get(
                "content-type", "application/json"
            ),
            "response_body": response.content,
            "updated_at": timezone.now(),
        }
        if error:
            fields["error_json"] = _failure_fields(
                error.code, error.message, attempts=attempt
            )["error_json"]

    PinPublishTask.objects.filter(pk=task.pk).update(**fields)
    task.refresh_from_db()
    if task.status == "SUCCESS":
        _request_stats_refresh(account)
    return task


def _request_stats_refresh(account):
    from .analytics import request_stats_refresh

    try:
        request_stats_refresh(account)
    except Exception:
        # Statistics must never affect the publish result.
        logger.warning("Could not request a statistics refresh for account %s.", account.id)


def classify_pin_response(response):
    if response.status_code == 401:
        return PinterestServiceError(
            "auth_error", "Pinterest cookies expired. Account deactivated."
        )
    if response.status_code != 200:
        code = "proxy_error" if response.status_code >= 500 else "api_error"
        return PinterestServiceError(
            code,
            "Pinterest could not publish the pin.",
        )

    try:
        response_data = response.json()
    except ValueError:
        return PinterestServiceError("api_error", "Pinterest returned an invalid response.")
    if not isinstance(response_data, dict):
        return PinterestServiceError("api_error", "Pinterest returned an invalid response.")
    resource_response = response_data.get("resource_response")
    if not isinstance(resource_response, dict):
        return PinterestServiceError("api_error", "Pinterest returned an invalid response.")
    if resource_response.get("error"):
        return PinterestServiceError(
            "api_error", "Pinterest rejected the pin request."
        )
    return None


def create_pin(
    account: PinterestAccount, title, description, link, image_url, board_id,
    deadline=None,
):
    """
    Publishes a pin to Pinterest.
    """
    logger.info("Creating pin for account %s on board %s.", account.id, board_id)
    try:
        session, csrf_token, base_url = get_session_and_csrf(account)
    except ValueError as exc:
        raise PinterestServiceError("validation_error", str(exc)) from exc

    url = f"https://{base_url}/resource/PinResource/create/"
    base_origin = f"https://{base_url}"

    try:
        signed_url = get_signed_pinterest_url(
            session, csrf_token, image_url, deadline=deadline
        )
        final_image_url = signed_url if signed_url else image_url

        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "x-csrftoken": csrf_token,
            "x-requested-with": "XMLHttpRequest",
            "referer": f"{base_origin}/pin-builder/?tab=save_from_url",
            "origin": base_origin,
            "accept": "application/json, text/javascript, */*; q=0.01",
        }
        options = {
            "field_set_key": "create_success",
            "skip_pin_create_log": True,
            "board_id": board_id,
            "description": description,
            "title": title,
            "image_url": final_image_url,
            "link": link,
        }
        payload = {
            "source_url": "/pin-builder/?tab=save_from_url",
            "data": json.dumps({"options": options, "context": {}}),
            "context": "{}",
        }
        response = session.post(
            url,
            headers=headers,
            data=payload,
            timeout=_request_timeout(deadline, 30),
        )
    except PinterestServiceError:
        raise
    except Exception as exc:
        message = describe_proxy_failure(exc)
        logger.warning(
            "Pin publish request failed for account %s: %s.",
            account.id,
            message,
        )
        raise PinterestServiceError(
            "publish_unknown",
            f"{message} Pinterest міг уже прийняти пін. Повтор з тим самим "
            "Idempotency-Key поверне цю ж відповідь; перевірте дошку, перш ніж "
            "публікувати пін знову з новим ключем.",
        ) from None
    finally:
        session.close()

    if response.status_code == 401:
        _deactivate_account(account)
        send_telegram_alert(
            f"🚫 Pinterest authentication failed for account <b>{escape(account.name)}</b>."
        )
    error = classify_pin_response(response)
    if error:
        if error.code == "api_error":
            send_telegram_alert(
                f"❌ Pinterest rejected a pin for account <b>{escape(account.name)}</b>."
            )
        logger.warning(
            "Pinterest returned %s for pin publish on account %s.",
            response.status_code,
            account.id,
        )
    else:
        logger.info("Pin created successfully for account %s.", account.id)
    return response
