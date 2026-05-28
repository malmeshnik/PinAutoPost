import json
import logging
import requests
from django.conf import settings
from curl_cffi import requests as curl_requests
from .models import PinterestAccount, PinterestBoard

logger = logging.getLogger(__name__)

def send_telegram_alert(message):
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_ADMIN_CHAT_ID
    if not token or not chat_id:
        logger.warning("Telegram credentials not configured.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
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
            impersonate="chrome120"
        )
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Proxy check failed for {proxy_url}: {e}")
        return False

def get_session_and_csrf(account: PinterestAccount):
    """
    Creates a curl_cffi session, sets cookies and extracts CSRF token.
    """
    session = curl_requests.Session(impersonate="chrome120")
    csrf_token = ""

    cookies_data = account.cookies
    if isinstance(cookies_data, dict) and "cookies" in cookies_data:
        cookies_data = cookies_data["cookies"]

    for cookie in cookies_data:
        name = cookie.get('name')
        value = cookie.get('value', '')
        domain = cookie.get('domain', '.pinterest.com')
        path = cookie.get('path', '/')

        session.cookies.set(name, value, domain=domain, path=path)

        if name == 'csrftoken':
            csrf_token = value

    return session, csrf_token

def refresh_boards(account: PinterestAccount):
    """
    Fetches boards from Pinterest and updates the local database.
    """
    if not check_proxy(account.proxy):
        account.is_active = False
        account.save()
        send_telegram_alert(f"⚠️ <b>Proxy Error</b>\nAccount: {account.name}\nProxy is unreachable. Account deactivated.")
        return False, "Proxy error"

    session, csrf_token = get_session_and_csrf(account)

    url = "https://www.pinterest.com/resource/BoardsResource/get/?data=%7B%22options%22%3A%7B%22field_set_key%22%3A%20%22detailed%22%7D%22%7D"
    headers = {
        "x-csrftoken": csrf_token,
        "x-requested-with": "XMLHttpRequest",
        "referer": "https://www.pinterest.com/",
    }

    try:
        response = session.get(
            url,
            headers=headers,
            proxies={"http": account.proxy, "https": account.proxy},
            timeout=15
        )

        if response.status_code == 401:
            account.is_active = False
            account.save()
            send_telegram_alert(f"🚫 <b>Auth Error</b>\nAccount: {account.name}\nCookies expired. Account deactivated.")
            return False, "Auth error"

        if response.status_code != 200:
            return False, f"Pinterest returned {response.status_code}"

        data = response.json()
        boards_list = data.get('resource_response', {}).get('data', [])

        # Sync boards
        existing_ids = []
        for b in boards_list:
            p_id = b.get('id')
            name = b.get('name')
            url_path = b.get('url')

            PinterestBoard.objects.update_or_create(
                account=account,
                pinterest_id=p_id,
                defaults={'name': name, 'url': url_path}
            )
            existing_ids.append(p_id)

        # Optional: remove boards that are no longer on Pinterest
        # PinterestBoard.objects.filter(account=account).exclude(pinterest_id__in=existing_ids).delete()

        return True, "Success"

    except Exception as e:
        logger.error(f"Error refreshing boards for {account.name}: {e}")
        return False, str(e)

def create_pin(account: PinterestAccount, title, description, link, image_url, board_id):
    """
    Publishes a pin to Pinterest.
    """
    if not check_proxy(account.proxy):
        account.is_active = False
        account.save()
        send_telegram_alert(f"⚠️ <b>Proxy Error</b>\nAccount: {account.name}\nProxy unreachable during posting. Account deactivated.")
        return False, "Proxy error"

    session, csrf_token = get_session_and_csrf(account)

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
        "image_url": image_url,
        "link": link,
        "method": "scraped",
        "scrape_metric": {
            "source": "www_url_scrape"
        },
        "user_mention_tags": []
    }

    payload = {
        "source_url": "/pin-builder/?tab=save_from_url",
        "data": json.dumps({"options": options, "context": {}}),
        "context": "{}"
    }

    try:
        response = session.post(
            url,
            headers=headers,
            data=payload,
            proxies={"http": account.proxy, "https": account.proxy},
            timeout=20
        )

        if response.status_code == 401:
            account.is_active = False
            account.save()
            send_telegram_alert(f"🚫 <b>Auth Error</b>\nAccount: {account.name}\nCookies expired during posting. Account deactivated.")
            return False, "Auth error"

        if response.status_code == 200:
            res_json = response.json()
            if 'resource_response' in res_json and 'error' in res_json['resource_response'] and res_json['resource_response']['error']:
                error_msg = res_json['resource_response']['error'].get('message', 'Unknown error')
                return False, f"Pinterest error: {error_msg}"
            return True, "Success"

        return False, f"Pinterest returned {response.status_code}: {response.text}"

    except Exception as e:
        logger.error(f"Error creating pin for {account.name}: {e}")
        return False, str(e)
