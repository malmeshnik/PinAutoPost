import uuid
from django.db import models
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _

class PinterestAccount(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pinterest_accounts', verbose_name=_("User"))
    name = models.CharField(_("Name"), max_length=255)
    cookies = models.JSONField(_("Cookies"), help_text=_("Original cookies array from browser extension"))
    proxy = models.CharField(_("Proxy"), max_length=512, help_text=_("Format: http://username:password@ip:port or socks5://..."))
    is_active = models.BooleanField(_("Is active"), default=True)
    webhook_token = models.UUIDField(_("Webhook token"), default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.user.username})"

    class Meta:
        verbose_name = _("Pinterest Account")
        verbose_name_plural = _("Pinterest Accounts")

class PinterestBoard(models.Model):
    account = models.ForeignKey(PinterestAccount, on_delete=models.CASCADE, related_name='boards', verbose_name=_("Account"))
    board_id = models.CharField(_("Board ID"), max_length=100)
    name = models.CharField(_("Name"), max_length=255)
    url = models.CharField(_("URL"), max_length=512)

    def __str__(self):
        return f"{self.name} - {self.account.name}"

    class Meta:
        verbose_name = _("Pinterest Board")
        verbose_name_plural = _("Pinterest Boards")

class PinPublishTask(models.Model):
    STATUS_CHOICES = [
        ('PENDING', _('Pending')),
        ('PROCESSING', _('Processing')),
        ('SUCCESS', _('Success')),
        ('FAILED', _('Failed')),
    ]

    account = models.ForeignKey(PinterestAccount, on_delete=models.CASCADE, related_name='tasks', verbose_name=_("Account"))
    title = models.CharField(_("Title"), max_length=255, blank=True, null=True)
    description = models.TextField(_("Description"), blank=True, null=True)
    link = models.URLField(_("Link"), max_length=512, blank=True, null=True)
    image_url = models.URLField(_("Image URL"), max_length=512)
    board_id = models.CharField(_("Board ID"), max_length=100, blank=True, null=True)
    board_name = models.CharField(_("Board Name"), max_length=255, blank=True, null=True)

    status = models.CharField(_("Status"), max_length=20, choices=STATUS_CHOICES, default='PENDING')
    error_json = models.JSONField(_("Error JSON"), blank=True, null=True)
    celery_task_id = models.CharField(_("Celery Task ID"), max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    def __str__(self):
        return f"Task {self.id} - {self.account.name} - {self.status}"

    class Meta:
        verbose_name = _("Pin Publish Task")
        verbose_name_plural = _("Pin Publish Tasks")
