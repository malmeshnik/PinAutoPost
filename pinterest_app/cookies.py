"""
Normalizes Pinterest cookies from the formats people export them in into the
single structure the publishing code reads:

    {"url": "https://www.pinterest.com",
     "cookies": [{"name": ..., "value": ..., "domain": ..., "path": ...,
                  "expires": <unix seconds, optional>}]}

Accepted input:
- a JSON array from a browser extension (Cookie-Editor, EditThisCookie, ...);
- an object {"url": ..., "cookies": [...]};
- a "name=value; name2=value2" Cookie header string;
- a Netscape cookies.txt file;
- any of the above as a JSON-encoded string.
"""
import json
import time
from urllib.parse import urlparse

DEFAULT_DOMAIN = ".pinterest.com"
DEFAULT_URL = "https://www.pinterest.com"
# Without these Pinterest rejects every request the account makes.
REQUIRED_COOKIES = ("csrftoken", "_pinterest_sess")


class CookieFormatError(ValueError):
    pass


def _is_pinterest_host(host):
    host = host.lower().lstrip(".")
    return host == "pinterest.com" or host.endswith(".pinterest.com")


def _normalize_url(url):
    if not isinstance(url, str) or not url.strip():
        return DEFAULT_URL
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
    except ValueError:
        raise CookieFormatError("Cookie URL must be a Pinterest domain URL.") from None
    if parsed.scheme not in {"http", "https"} or not host or not _is_pinterest_host(host):
        raise CookieFormatError("Cookie URL must point to a Pinterest domain.")
    return f"https://{host}"


def _parse_header_string(text):
    cookies = []
    for part in text.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name.strip():
            cookies.append({"name": name.strip(), "value": value.strip()})
    return cookies


def _parse_netscape(text):
    cookies = []
    for line in text.splitlines():
        line = line.strip()
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_"):]
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        domain, _subdomains, path, _secure, expires, name, value = fields
        cookie = {"name": name, "value": value, "domain": domain, "path": path or "/"}
        if expires.isdigit() and int(expires) > 0:
            cookie["expires"] = int(expires)
        cookies.append(cookie)
    return cookies


def _parse_text(text):
    text = text.strip()
    if not text:
        return None, []
    if text[0] in "[{":
        try:
            return _split_container(json.loads(text))
        except json.JSONDecodeError:
            raise CookieFormatError("Cookies look like JSON but cannot be parsed.") from None
    if "\t" in text:
        return None, _parse_netscape(text)
    return None, _parse_header_string(text)


def _split_container(raw):
    """Returns (url, list of raw cookie items) for any accepted shape."""
    if isinstance(raw, str):
        return _parse_text(raw)
    if isinstance(raw, dict):
        items = raw.get("cookies")
        if isinstance(items, str):
            _url, parsed = _parse_text(items)
            return raw.get("url"), parsed
        if not isinstance(items, list):
            raise CookieFormatError(
                "Provide a cookie array, an object with a 'cookies' array, "
                "a Cookie header string or a cookies.txt export."
            )
        return raw.get("url"), items
    if isinstance(raw, list):
        return None, raw
    raise CookieFormatError(
        "Provide a cookie array, an object with a 'cookies' array, "
        "a Cookie header string or a cookies.txt export."
    )


def _expiry(item):
    # Extensions use expirationDate (float seconds); DevTools uses expires.
    for key in ("expirationDate", "expires", "expiry"):
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)
    return None


def normalize_cookies(raw, require_auth=True, now=None):
    url, items = _split_container(raw)
    now = time.time() if now is None else now

    by_key = {}
    for item in items:
        if not isinstance(item, dict):
            raise CookieFormatError("Each cookie must be an object with a name and value.")
        name = item.get("name")
        value = item.get("value", "")
        if not isinstance(name, str) or not name.strip():
            raise CookieFormatError("Each cookie must have a non-empty name.")
        if not isinstance(value, str):
            raise CookieFormatError(f"Cookie '{name}' must have a string value.")
        domain = item.get("domain") or DEFAULT_DOMAIN
        path = item.get("path") or "/"
        if not isinstance(domain, str) or not isinstance(path, str):
            raise CookieFormatError(f"Cookie '{name}' has an invalid domain or path.")
        if not _is_pinterest_host(domain):
            # Exports often include cookies of other sites; they are not needed.
            continue
        expires = _expiry(item)
        if expires is not None and expires <= now:
            continue

        cookie = {"name": name.strip(), "value": value, "domain": domain.lower(), "path": path}
        if expires is not None:
            cookie["expires"] = expires
        # A later duplicate (same name, domain and path) replaces the earlier one.
        by_key[(cookie["name"], cookie["domain"], cookie["path"])] = cookie

    cookies = sorted(by_key.values(), key=lambda c: (c["name"], c["domain"], c["path"]))
    if not cookies:
        raise CookieFormatError("No valid, unexpired Pinterest cookies were found.")
    if require_auth:
        present = {cookie["name"] for cookie in cookies if cookie["value"]}
        missing = [name for name in REQUIRED_COOKIES if name not in present]
        if missing:
            raise CookieFormatError(
                "Missing Pinterest cookies: "
                + ", ".join(missing)
                + ". Export the cookies while logged in to pinterest.com."
            )
    return {"url": _normalize_url(url), "cookies": cookies}
