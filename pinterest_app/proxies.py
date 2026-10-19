"""
Normalizes proxies from the formats providers hand them out in into one URL:
``scheme://user:password@host:port``.

Accepted input:
- a full URL: ``socks5://user:pass@host:port``, ``http://host:port``;
- ``host:port``;
- ``host:port:user:password`` (the usual provider export);
- ``user:password:host:port``;
- ``user:password@host:port``.

The scheme comes from ``proxy_type`` when it is given, otherwise from the
URL, otherwise it is ``http``. Error messages never include the credentials.
"""
from urllib.parse import quote, urlsplit

PROXY_TYPES = ("http", "https", "socks4", "socks5", "socks5h")
DEFAULT_PROXY_TYPE = "http"


class ProxyFormatError(ValueError):
    pass


def _parse_port(value):
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        return None
    return int(value)


def _check_host(host):
    if not host or any(char in host for char in " /@"):
        raise ProxyFormatError("The proxy host is missing or invalid.")
    return host


def _from_url(value):
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ProxyFormatError("The proxy port must be a number from 1 to 65535.") from None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ProxyFormatError("A proxy URL must not contain a path or query.")
    if port is None:
        raise ProxyFormatError("The proxy port is missing.")
    host = _check_host(parts.hostname)
    # Credentials in a URL are kept as written: they may already be URL-encoded.
    userinfo = parts.netloc.rpartition("@")[0] if "@" in parts.netloc else ""
    return parts.scheme.lower(), host, port, userinfo


def _from_plain(value):
    parts = value.split(":")
    # host:port:user:password first: the password may contain "@" or ":".
    if len(parts) >= 4 and _parse_port(parts[1]) is not None:
        return None, _check_host(parts[0]), _parse_port(parts[1]), _userinfo(
            parts[2], ":".join(parts[3:])
        )

    if "@" in value:
        credentials, _at, address = value.rpartition("@")
        host, _colon, port_text = address.rpartition(":")
        port = _parse_port(port_text)
        if port is None:
            raise ProxyFormatError("The proxy port must be a number from 1 to 65535.")
        user, _colon, password = credentials.partition(":")
        return None, _check_host(host), port, _userinfo(user, password)

    if len(parts) == 2:
        port = _parse_port(parts[1])
        if port is None:
            raise ProxyFormatError("The proxy port must be a number from 1 to 65535.")
        return None, _check_host(parts[0]), port, ""
    if len(parts) == 4 and _parse_port(parts[3]) is not None:
        # user:password:host:port
        return None, _check_host(parts[2]), _parse_port(parts[3]), _userinfo(
            parts[0], parts[1]
        )
    raise ProxyFormatError(
        "Unrecognized proxy format. Use host:port, host:port:user:password, "
        "user:password@host:port or a full URL such as socks5://user:password@host:port."
    )


def _userinfo(user, password):
    if not user:
        raise ProxyFormatError("The proxy username is empty.")
    userinfo = quote(user, safe="")
    if password:
        userinfo += ":" + quote(password, safe="")
    return userinfo


def proxy_type_of(proxy_url):
    scheme = proxy_url.partition("://")[0].lower() if "://" in proxy_url else ""
    return scheme if scheme in PROXY_TYPES else DEFAULT_PROXY_TYPE


def normalize_proxy(value, proxy_type=None):
    if not isinstance(value, str) or not value.strip():
        raise ProxyFormatError("The proxy is empty.")
    value = value.strip()
    if proxy_type is not None and proxy_type not in PROXY_TYPES:
        raise ProxyFormatError(
            "Unsupported proxy type. Use one of: " + ", ".join(PROXY_TYPES) + "."
        )

    if "://" in value:
        scheme, host, port, userinfo = _from_url(value)
        if scheme not in PROXY_TYPES:
            raise ProxyFormatError(
                "Unsupported proxy scheme. Use one of: " + ", ".join(PROXY_TYPES) + "."
            )
    else:
        scheme, host, port, userinfo = _from_plain(value)

    scheme = proxy_type or scheme or DEFAULT_PROXY_TYPE
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    credentials = f"{userinfo}@" if userinfo else ""
    return f"{scheme}://{credentials}{host}:{port}"
