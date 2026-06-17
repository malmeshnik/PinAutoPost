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


def extract_base_url(cookies_data):
    """
    Extracts base URL from cookies structure.
    Supports both {"url": "...", "cookies": [...]} and [...] formats.
    Returns the base domain (e.g., www.pinterest.com or ru.pinterest.com).
    """
    base_url = "www.pinterest.com"

    if isinstance(cookies_data, dict):
        if "url" in cookies_data:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(cookies_data["url"])
                if parsed.netloc:
                    base_url = parsed.netloc
                    logger.info(f"✓ Extracted base URL from cookies: {base_url}")
                else:
                    logger.warning(f"⚠ No netloc in parsed URL, using default: {base_url}")
            except Exception as e:
                logger.error(f"✗ Failed to parse URL from cookies: {e}, using default: {base_url}")
        else:
            logger.debug(f"No 'url' field in cookies_data, using default: {base_url}")
    else:
        logger.debug(f"Cookies data is not a dict, using default base URL: {base_url}")

    return base_url


def get_session_and_csrf(account: PinterestAccount):
    """
    Creates a curl_cffi session, sets cookies and extracts CSRF token.
    Also extracts base URL from cookies for API requests.
    """
    logger.info(f"Setting up session for account: {account.name}")

    check_proxy(account.proxy)
    session = curl_requests.Session(impersonate="chrome120")
    session.proxies = {"http": account.proxy, "https": account.proxy}
    csrf_token = ""

    # Extract base URL
    base_url = extract_base_url(account.cookies)

    cookies_data = account.cookies
    if isinstance(cookies_data, dict) and "cookies" in cookies_data:
        cookies_data = cookies_data["cookies"]
        logger.debug(f"Extracted cookies array from dict structure")

    if not cookies_data:
        logger.error(f"✗ No cookies data found for account {account.name}")
        return session, csrf_token, base_url

    cookies_count = 0
    for cookie in cookies_data:
        name = cookie.get("name")
        value = cookie.get("value", "")
        domain = cookie.get("domain", ".pinterest.com")
        path = cookie.get("path", "/")

        session.cookies.set(name, value, domain=domain, path=path)
        cookies_count += 1

        if name == "csrftoken":
            csrf_token = value
            logger.debug(f"✓ Found CSRF token: {csrf_token[:20]}...")

    logger.info(f"✓ Session configured with {cookies_count} cookies, CSRF: {'✓' if csrf_token else '✗'}, Base URL: {base_url}")

    if not csrf_token:
        logger.warning(f"⚠ No CSRF token found in cookies for account {account.name}")

    return session, csrf_token, base_url


def refresh_boards(account: PinterestAccount):
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

    logger.info(f"🔄 Refreshing boards for account {account.name} using base URL {base_url}...")
    logger.debug(f"Request URL: {url}")
    logger.debug(f"Request params: {params}")

    try:
        response = session.post(url, headers=headers, params=params, timeout=30)
        logger.info(f"📡 API response status: {response.status_code}")

        if response.status_code == 200:
            res_json = response.json()
            logger.debug(f"Response JSON keys: {res_json.keys()}")

            boards = res_json.get("resource_response", {}).get("data", [])

            if boards:
                deleted_count = PinterestBoard.objects.filter(account=account).delete()[0]
                logger.debug(f"Deleted {deleted_count} old boards")

                for board in boards:
                    PinterestBoard.objects.create(
                        account=account,
                        board_id=board.get("id"),
                        name=board.get("name"),
                        url=board.get("url"),
                    )

                logger.info(f"✓ Boards refreshed for account {account.name}. Total boards: {len(boards)}")
            else:
                error_data = res_json.get("resource_response", {})
                logger.error(f"✗ No boards found for account {account.name}")
                logger.error(f"API response: {json.dumps(error_data, indent=2, ensure_ascii=False)}")
                send_telegram_alert(
                    f"⚠️ Не вдалося отримати дошки для акаунту <b>{account.name}</b>\n"
                    f"Base URL: {base_url}\n"
                    f"Response: {json.dumps(error_data, ensure_ascii=False)[:500]}"
                )

        elif response.status_code == 401:
            logger.error(f"🚫 Auth error (401) for account {account.name} - deactivating")
            account.is_active = False
            account.save()
            send_telegram_alert(
                f"🚫 <b>Auth Error</b>\n"
                f"Account: {account.name}\n"
                f"Base URL: {base_url}\n"
                f"Cookies expired during board refresh. Account deactivated."
            )
        else:
            logger.error(f"✗ Unexpected status code {response.status_code}: {response.text[:500]}")
            send_telegram_alert(
                f"❌ Помилка при оновленні дошок для акаунту <b>{account.name}</b>\n"
                f"Status: {response.status_code}\n"
                f"Response: {response.text[:500]}"
            )

    except requests.exceptions.Timeout as e:
        logger.error(f"⏱ Timeout error refreshing boards for {account.name}: {e}")
        send_telegram_alert(
            f"⏱ Timeout при оновленні дошок для акаунту <b>{account.name}</b>\n"
            f"Можливо проблема з проксі або Pinterest API"
        )
    except Exception as e:
        logger.error(f"✗ Error refreshing boards for {account.name}: {e}", exc_info=True)
        send_telegram_alert(
            f"❌ Помилка при оновленні дошок для акаунту <b>{account.name}</b>: {e}"
        )
        return


def create_pin(
    account: PinterestAccount, title, description, link, image_url, board_id
):
    """
    Publishes a pin to Pinterest.
    """
    logger.info(f"📌 Creating pin for account {account.name}, board_id: {board_id}")
    logger.debug(f"Pin data - title: {title}, image_url: {image_url}, link: {link}")

    session, csrf_token, base_url = get_session_and_csrf(account)

    url = f"https://{base_url}/resource/PinResource/create/"
    base_origin = f"https://{base_url}"

    logger.debug(f"Using base URL: {base_url}, origin: {base_origin}")

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
        "image_url": image_url,
        "link": link,
        "method": "scraped",
        "scrape_metric": {"source": "www_url_scrape"},
        "user_mention_tags": [],
    }

    payload = {
        "source_url": "/pin-builder/?tab=save_from_url",
        "data": json.dumps({"options": options, "context": {}}),
        "context": "{}",
    }

    try:
        logger.debug(f"Sending POST request to {url}")
        response = session.post(url, headers=headers, data=payload, timeout=30)
        logger.info(f"📡 Pinterest API response status: {response.status_code}")

        if response.status_code == 401:
            logger.error(f"🚫 Auth error (401) for account {account.name} - deactivating")
            account.is_active = False
            account.save()
            send_telegram_alert(
                f"🚫 <b>Auth Error</b>\n"
                f"Account: {account.name}\n"
                f"Base URL: {base_url}\n"
                f"Cookies expired during posting. Account deactivated."
            )
            return False, "Auth error"

        if response.status_code == 200:
            res_json = response.json()
            logger.debug(f"Response JSON keys: {res_json.keys()}")

            if (
                "resource_response" in res_json
                and "error" in res_json["resource_response"]
                and res_json["resource_response"]["error"]
            ):
                error_msg = res_json["resource_response"]["error"].get(
                    "message", "Unknown error"
                )
                logger.error(f"✗ Pinterest API error: {error_msg}")
                logger.debug(f"Full error response: {json.dumps(res_json, indent=2, ensure_ascii=False)}")
                send_telegram_alert(
                    f"❌ Помилка при створенні піну для акаунту <b>{account.name}</b>\n"
                    f"Base URL: {base_url}\n"
                    f"Error: {error_msg}"
                )
                return False, f"Pinterest error: {error_msg}"

            logger.info(f"✓ Pin created successfully for account {account.name}")
            return True, "Success"

        logger.error(f"✗ Unexpected status code {response.status_code}")
        logger.debug(f"Response text: {response.text[:500]}")
        send_telegram_alert(
            f"❌ Помилка при створенні піну для акаунту <b>{account.name}</b>\n"
            f"Status: {response.status_code}\n"
            f"Response: {response.text[:500]}"
        )
        return False, f"Pinterest returned {response.status_code}: {response.text[:200]}"

    except requests.exceptions.Timeout as e:
        logger.error(f"⏱ Timeout error creating pin for {account.name}: {e}")
        send_telegram_alert(
            f"⏱ Timeout при створенні піну для акаунту <b>{account.name}</b>\n"
            f"Можливо проблема з проксі або Pinterest API"
        )
        return False, "Proxy connection timeout"
    except Exception as e:
        logger.error(f"✗ Error creating pin for {account.name}: {e}", exc_info=True)
        send_telegram_alert(
            f"❌ Помилка при створенні піну для акаунту <b>{account.name}</b>: {e}"
        )
        return False, str(e)
