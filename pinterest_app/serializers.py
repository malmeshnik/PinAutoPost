from rest_framework import serializers
from .models import PinterestAccount, PinterestBoard, PinPublishTask

class PinterestBoardSerializer(serializers.ModelSerializer):
    class Meta:
        model = PinterestBoard
        fields = ['id', 'board_id', 'name', 'url']

class PinterestAccountSerializer(serializers.ModelSerializer):
    webhook_url = serializers.SerializerMethodField()

    class Meta:
        model = PinterestAccount
        fields = ['id', 'name', 'cookies', 'proxy', 'is_active', 'webhook_token', 'webhook_url', 'created_at']
        read_only_fields = ['is_active', 'webhook_token', 'created_at']

    def get_webhook_url(self, obj):
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(f'/api/v1/publish/{obj.webhook_token}/')
        return f'/api/v1/publish/{obj.webhook_token}/'

class PinPublishSerializer(serializers.Serializer):
    title = serializers.CharField(required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    link = serializers.URLField(required=False, allow_blank=True, allow_null=True)
    image_url = serializers.URLField()
    board_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    board_name = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, data):
        if not data.get('board_id') and not data.get('board_name'):
            raise serializers.ValidationError("Either board_id or board_name must be provided.")
        return data

class PinPublishTaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = PinPublishTask
        fields = '__all__'
