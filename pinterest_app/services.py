import json
import logging
from time import time
import requests
from django.conf import settings
from curl_cffi import requests as curl_requests
from .models import PinterestAccount, PinterestBoard

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


def send_telegram_alert(message):
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_ADMIN_CHAT_ID
    if not token or not chat_id:
        logger.warning("Telegram credentials not configured.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Failed to send telegram alert: {e}")


def check_proxy(proxy_url):
    """
    Checks if proxy is working.
    """
    try:
        response = curl_requests.get(
            "https://api.ipify.org?format=json",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=5,
            impersonate="chrome120",
        )
        return response.status_code == 200
    except Exception as e:
        send_telegram_alert(f"Помилка при підключенні до проксі {proxy_url}: {e}")
        logger.error(f"Proxy check failed for {proxy_url}: {e}")
        return False


def get_session_and_csrf(account: PinterestAccount):
    """
    Creates a curl_cffi session, sets cookies and extracts CSRF token.
    """
    check_proxy(account.proxy)
    session = curl_requests.Session(impersonate="chrome120")
    session.proxies = {"http": account.proxy, "https": account.proxy}
    csrf_token = ""

    cookies_data = account.cookies
    if isinstance(cookies_data, dict) and "cookies" in cookies_data:
        cookies_data = cookies_data["cookies"]

    for cookie in cookies_data:
        name = cookie.get("name")
        value = cookie.get("value", "")
        domain = cookie.get("domain", ".pinterest.com")
        path = cookie.get("path", "/")

        session.cookies.set(name, value, domain=domain, path=path)

        if name == "csrftoken":
            csrf_token = value

    return session, csrf_token


def refresh_boards(account: PinterestAccount):
    url = "https://www.pinterest.com/resource/BoardsResource/get/"
    session, csrf_token = get_session_and_csrf(account)
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

    logger.info(f"Refreshing boards for account {account.name}...")
    try:
        response = session.post(url, headers=headers, params=params, timeout=30)

        if response.status_code == 200:
            res_json = response.json()
            boards = res_json.get("resource_response", {}).get("data", [])

            if boards:
                PinterestBoard.objects.filter(account=account).delete()
                for board in boards:
                    PinterestBoard.objects.create(
                        account=account,
                        board_id=board.get("id"),
                        name=board.get("name"),
                        url=board.get("url"),
                    )

                logger.info(
                    f"Boards refreshed for account {account.name}. Total boards: {len(boards)}"
                )
            else:
                logger.warning(
                    f"No boards found for account {account.name} or API error: {res_json}"
                )
                send_telegram_alert(
                    f"⚠️ Не вдалося отримати дошки для акаунту {account.name}. Можливо, проблема з API або куками."
                )

        elif response.status_code == 401:
            account.is_active = False
            account.save()
            send_telegram_alert(
                f"🚫 <b>Auth Error</b>\nAccount: {account.name}\nCookies expired during board refresh. Account deactivated."
            )

    except Exception as e:
        logger.error(f"Error refreshing boards for {account.name}: {e}")
        send_telegram_alert(
            f"❌ Помилка при оновленні дошок для акаунту {account.name}: {e}"
        )
        return

def get_signed_pinterest_url(session, csrf_token, original_image_url):
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
        logger.info(f"Отримуємо підписаний Pinterest URL для: {original_image_url}")
        response = session.get(
            url,
            params={"source_url": "/pin-builder/?tab=save_from_url", "data": json.dumps(data)},
            headers=headers,
            timeout=15,
        )

        if response.status_code == 200:
            res_json = response.json()
            items = res_json.get("resource_response", {}).get("data", {}).get("items", [])
            if items:
                signed_url = items[0].get("url")
                logger.info("Підписаний URL успішно отримано.")
                return signed_url
            
        logger.warning(f"Не вдалося отримати проксі-лінк, статус: {response.status_code}")
    except Exception as e:
        logger.error(f"Помилка при виконанні FindPinImagesResource: {e}")
        
    return None


def create_pin(
    account: PinterestAccount, title, description, link, image_url, board_id
):
    """
    Publishes a pin to Pinterest.
    """
    session, csrf_token = get_session_and_csrf(account)
    signed_url = get_signed_pinterest_url(session, csrf_token, image_url)
    logger.info(f"Signed URL: {signed_url}")
    final_image_url = signed_url if signed_url else image_url

    url = "https://www.pinterest.com/resource/PinResource/create/"
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "x-csrftoken": csrf_token,
        "x-requested-with": "XMLHttpRequest",
        "referer": "https://www.pinterest.com/pin-builder/?tab=save_from_url",
        "origin": "https://www.pinterest.com",
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

    try:
        response = session.post(url, headers=headers, data=payload, timeout=30)

        if response.status_code == 401:
            account.is_active = False
            account.save()
            send_telegram_alert(
                f"🚫 <b>Auth Error</b>\nAccount: {account.name}\nCookies expired during posting. Account deactivated."
            )
            return False, "Auth error"

        if response.status_code == 200:
            res_json = response.json()
            if (
                "resource_response" in res_json
                and "error" in res_json["resource_response"]
                and res_json["resource_response"]["error"]
            ):
                error_msg = res_json["resource_response"]["error"].get(
                    "message", "Unknown error"
                )
                send_telegram_alert(
                    f"❌ Помилка при створенні піну для акаунту {account.name}: {error_msg}"
                )
                return False, f"Pinterest error: {error_msg}"
            return True, "Success"

        return False, f"Pinterest returned {response.status_code}: {response.text}"

    except Exception as e:
        logger.error(f"Error creating pin for {account.name}: {e}")
        send_telegram_alert(
            f"❌ Помилка при створенні піну для акаунту {account.name}: {e}"
        )
        return False, str(e)
