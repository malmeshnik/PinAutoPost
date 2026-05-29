import uuid
from django.db import models
from django.contrib.auth.models import User

class PinterestAccount(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pinterest_accounts')
    name = models.CharField(max_length=255)
    cookies = models.JSONField(help_text="Original cookies array from browser extension")
    proxy = models.CharField(max_length=512, help_text="Format: http://username:password@ip:port or socks5://...")
    is_active = models.BooleanField(default=True)
    webhook_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.user.username})"

class PinterestBoard(models.Model):
    account = models.ForeignKey(PinterestAccount, on_delete=models.CASCADE, related_name='boards')
    board_id = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    url = models.CharField(max_length=512)

    def __str__(self):
        return f"{self.name} - {self.account.name}"
