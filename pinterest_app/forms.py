from django import forms
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminSelectWidget, UnfoldAdminTextareaWidget

from .cookies import CookieFormatError, normalize_cookies
from .models import PinterestAccount, PinterestBoard, PinPublishTask
from .proxies import DEFAULT_PROXY_TYPE, ProxyFormatError, normalize_proxy, proxy_type_of
from .services import (
    PINTEREST_TITLE_MAX_LENGTH,
    PinterestServiceError,
    check_proxy,
    fetch_boards,
)

PROXY_TYPE_CHOICES = (
    ("http", "HTTP"),
    ("https", "HTTPS"),
    ("socks5", "SOCKS5"),
    ("socks5h", _("SOCKS5 (DNS through the proxy)")),
    ("socks4", "SOCKS4"),
)

ACCOUNT_CHECK_MESSAGES = {
    "auth_error": _(
        "Pinterest rejected these cookies. Log in to pinterest.com again and "
        "export fresh cookies."
    ),
    "no_boards": _(
        "Pinterest returned no boards. Check that the name is the Pinterest "
        "username and that the account has at least one board."
    ),
    "api_error": _(
        "Pinterest could not load the boards. Check that the name is the "
        "Pinterest username."
    ),
}


class PinterestAccountAdminForm(forms.ModelForm):
    """
    Validates the account against Pinterest before it is saved: cookies are
    normalized, the proxy is checked and the boards are loaded. The loaded
    boards are kept in ``fetched_boards`` for the admin to store.
    """

    proxy_type = forms.ChoiceField(
        label=_("Proxy type"),
        choices=PROXY_TYPE_CHOICES,
        initial=DEFAULT_PROXY_TYPE,
        widget=UnfoldAdminSelectWidget,
        help_text=_("Replaces the scheme if the proxy below is a full URL."),
    )
    cookies_input = forms.CharField(
        label=_("Cookies"),
        required=False,
        strip=True,
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 8}),
        help_text=_(
            "Paste the export from a browser extension (JSON), a "
            "\"name=value; ...\" Cookie header or a cookies.txt file. "
            "csrftoken and _pinterest_sess are required."
        ),
    )

    class Meta:
        model = PinterestAccount
        fields = ("name", "user", "proxy", "is_active")
        help_texts = {
            "name": _(
                "Pinterest username, as in pinterest.com/<username>/. "
                "It is used to load the boards."
            ),
            "proxy": _(
                "host:port, host:port:user:password, user:password@host:port "
                "or a full URL."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fetched_boards = None
        self.normalized_cookies = None
        if self.instance.pk is not None:
            self.initial.setdefault("proxy_type", proxy_type_of(self.instance.proxy))
        if self.instance.pk is None:
            self.fields["cookies_input"].required = True
        else:
            self.fields["cookies_input"].help_text = _(
                "Leave empty to keep the current cookies. New cookies are "
                "checked with Pinterest and reactivate the account."
            )


    def clean_cookies_input(self):
        raw = self.cleaned_data.get("cookies_input")
        if not raw:
            return raw
        try:
            self.normalized_cookies = normalize_cookies(raw)
        except CookieFormatError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return raw

    def clean(self):
        cleaned_data = super().clean()
        # Model fields are cleaned before declared ones, so the proxy is
        # normalized here, once proxy_type is available.
        if "proxy" in cleaned_data and "proxy_type" in cleaned_data:
            try:
                cleaned_data["proxy"] = normalize_proxy(
                    cleaned_data["proxy"], cleaned_data["proxy_type"]
                )
            except ProxyFormatError as exc:
                self.add_error("proxy", str(exc))
        if self.errors:
            return cleaned_data

        is_new = self.instance.pk is None
        credentials_changed = (
            is_new
            or self.normalized_cookies is not None
            or cleaned_data["proxy"] != self.instance.proxy
            or "name" in self.changed_data
        )
        if not credentials_changed:
            return cleaned_data

        proxy = cleaned_data["proxy"]
        try:
            check_proxy(proxy)
        except PinterestServiceError as exc:
            self.add_error("proxy", exc.message)
            return cleaned_data

        probe = PinterestAccount(
            name=cleaned_data["name"],
            proxy=proxy,
            cookies=self.normalized_cookies or self.instance.cookies,
        )
        try:
            self.fetched_boards = fetch_boards(probe)
        except PinterestServiceError as exc:
            field = "cookies_input" if exc.code == "auth_error" else None
            if exc.code == "proxy_error":
                field = "proxy"
            self.add_error(field, ACCOUNT_CHECK_MESSAGES.get(exc.code, exc.message))
        return cleaned_data

    def save(self, commit=True):
        if self.normalized_cookies is not None:
            self.instance.cookies = self.normalized_cookies
            # Cookies that Pinterest just accepted make the account usable again.
            self.instance.is_active = True
        return super().save(commit=commit)


class BoardChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, board):
        return f"{board.account.name} → {board.name}"


class PinPublishTaskAdminForm(forms.ModelForm):
    board = BoardChoiceField(
        label=_("Board"),
        queryset=PinterestBoard.objects.none(),
        widget=UnfoldAdminSelectWidget,
        help_text=_(
            "Boards of active accounts. Use \"Refresh boards\" on the account "
            "list if a board is missing."
        ),
    )

    class Meta:
        model = PinPublishTask
        fields = ("title", "description", "link", "image_url")

    def __init__(self, *args, boards=None, **kwargs):
        super().__init__(*args, **kwargs)
        if boards is not None:
            self.fields["board"].queryset = boards
        self.fields["image_url"].required = True

    def clean_title(self):
        title = self.cleaned_data.get("title") or ""
        if len(title) > PINTEREST_TITLE_MAX_LENGTH:
            raise forms.ValidationError(
                _("Pinterest allows at most %(limit)s characters in a title.")
                % {"limit": PINTEREST_TITLE_MAX_LENGTH}
            )
        return title or None

    def save(self, commit=True):
        board = self.cleaned_data["board"]
        self.instance.account = board.account
        self.instance.board_id = board.board_id
        self.instance.board_name = board.name
        return super().save(commit=commit)
