from rest_framework import serializers
from .cookies import CookieFormatError, normalize_cookies
from .models import PinterestAccount, PinterestBoard
from .proxies import PROXY_TYPES, ProxyFormatError, normalize_proxy
from .services import PINTEREST_TITLE_MAX_LENGTH

class PinterestBoardSerializer(serializers.ModelSerializer):
    class Meta:
        model = PinterestBoard
        fields = ['id', 'board_id', 'name', 'url']

class PinterestAccountSerializer(serializers.ModelSerializer):
    webhook_url = serializers.SerializerMethodField()
    boards = PinterestBoardSerializer(many=True, read_only=True)
    proxy_type = serializers.ChoiceField(
        choices=PROXY_TYPES, required=False, write_only=True,
        help_text="Overrides the scheme of 'proxy'. Defaults to the URL scheme or http.",
    )

    class Meta:
        model = PinterestAccount
        fields = [
            'id', 'name', 'cookies', 'proxy', 'proxy_type', 'is_active',
            'webhook_token', 'webhook_url', 'boards', 'created_at',
        ]
        extra_kwargs = {
            'cookies': {'write_only': True},
            'proxy': {
                'write_only': True,
                'help_text': (
                    "host:port, host:port:user:password, user:password@host:port "
                    "or a full URL such as socks5://user:password@host:port."
                ),
            },
        }
        read_only_fields = ['is_active', 'webhook_token', 'created_at']

    def validate_cookies(self, value):
        # Stored in the one format the publishing code reads, whatever the
        # export format was.
        try:
            return normalize_cookies(value)
        except CookieFormatError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, data):
        # proxy_type applies to the proxy in this request, or to the stored one
        # when only the type changes.
        proxy_type = data.pop("proxy_type", None)
        proxy = data.get("proxy")
        if proxy is None and proxy_type is not None and self.instance is not None:
            proxy = self.instance.proxy
        if proxy is not None:
            try:
                data["proxy"] = normalize_proxy(proxy, proxy_type)
            except ProxyFormatError as exc:
                raise serializers.ValidationError({"proxy": str(exc)}) from exc
        return data

    def get_webhook_url(self, obj):
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(f'/api/v1/publish/{obj.webhook_token}/')
        return f'/api/v1/publish/{obj.webhook_token}/'

class PinPublishSerializer(serializers.Serializer):
    title = serializers.CharField(
        required=False, allow_blank=True, max_length=PINTEREST_TITLE_MAX_LENGTH
    )
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    link = serializers.URLField(required=False, allow_blank=True, allow_null=True, max_length=512)
    image_url = serializers.URLField(max_length=512)
    board_id = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=100)
    board_name = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=255)

    def validate(self, data):
        if not data.get('board_id') and not data.get('board_name'):
            raise serializers.ValidationError("Either board_id or board_name must be provided.")
        return data
