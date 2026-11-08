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
    # Numeric Pinterest user id, resolved from the username once; the
    # analytics API addresses accounts by it.
    pinterest_user_id = models.CharField(_("Pinterest user ID"), max_length=64, blank=True, default="", editable=False)
    # Set when a statistics refresh is queued; limits refreshes to one per hour.
    stats_requested_at = models.DateTimeField(_("Statistics requested at"), blank=True, null=True, editable=False)
    stats_error = models.CharField(_("Statistics error"), max_length=255, blank=True, default="", editable=False)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.user.username})"

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_name = instance.__dict__.get("name")
        return instance

    def save(self, *args, **kwargs):
        # The user id belongs to the username; a renamed account resolves it again.
        loaded_name = getattr(self, "_loaded_name", None)
        if loaded_name is not None and self.name != loaded_name:
            self.pinterest_user_id = ""
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "name" in update_fields:
                kwargs["update_fields"] = {*update_fields, "pinterest_user_id"}
        super().save(*args, **kwargs)
        self._loaded_name = self.name

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
    status_token = models.UUIDField(_("Status token"), default=uuid.uuid4, unique=True, editable=False)
    idempotency_key = models.CharField(_("Idempotency key"), max_length=255, blank=True, null=True)
    request_fingerprint = models.CharField(_("Request fingerprint"), max_length=64, blank=True, null=True)
    response_status_code = models.PositiveSmallIntegerField(blank=True, null=True)
    response_content_type = models.CharField(max_length=255, blank=True, null=True)
    response_body = models.BinaryField(blank=True, null=True)

    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    def __str__(self):
        return f"Task {self.id} - {self.account.name} - {self.status}"

    class Meta:
        verbose_name = _("Pin Publish Task")
        verbose_name_plural = _("Pin Publish Tasks")
        constraints = [
            models.UniqueConstraint(
                fields=("account", "idempotency_key"),
                condition=models.Q(idempotency_key__isnull=False),
                name="unique_account_idempotency_key",
            ),
        ]


class SystemAlertState(models.Model):
    key = models.CharField(_("Alert key"), max_length=255, primary_key=True)
    is_active = models.BooleanField(_("Is active"), default=False)
    last_notified_at = models.DateTimeField(_("Last notified at"), blank=True, null=True)
    payload = models.JSONField(_("Payload"), blank=True, null=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("System Alert State")
        verbose_name_plural = _("System Alert States")

    def __str__(self):
        return self.key


class AccountStatsSnapshot(models.Model):
    """Account totals over a rolling period, as Pinterest Analytics showed them."""

    account = models.ForeignKey(
        PinterestAccount, on_delete=models.CASCADE, related_name="stats_snapshots",
        verbose_name=_("Account"),
    )
    period_start = models.DateField(_("Period start"))
    period_end = models.DateField(_("Period end"))
    impressions = models.BigIntegerField(_("Impressions"), default=0)
    engagements = models.BigIntegerField(_("Engagements"), default=0)
    outbound_clicks = models.BigIntegerField(_("Outbound clicks"), default=0)
    saves = models.BigIntegerField(_("Saves"), default=0)
    # Change against the previous period of the same length, as a fraction
    # (-0.24 means -24%). Empty when the previous period had no data.
    impressions_delta = models.FloatField(_("Impressions change"), blank=True, null=True)
    engagements_delta = models.FloatField(_("Engagements change"), blank=True, null=True)
    outbound_clicks_delta = models.FloatField(_("Outbound clicks change"), blank=True, null=True)
    saves_delta = models.FloatField(_("Saves change"), blank=True, null=True)
    # False while Pinterest is still computing some of the metrics.
    is_complete = models.BooleanField(_("Data complete"), default=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Statistics snapshot")
        verbose_name_plural = _("Statistics snapshots")
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("account", "-created_at"), name="stats_account_created_idx")]

    def __str__(self):
        return f"{self.account.name} {self.period_start}..{self.period_end}"


class AccountStatistics(PinterestAccount):
    """Admin entry point for the statistics pages; stores nothing of its own."""

    class Meta:
        proxy = True
        verbose_name = _("Account statistics")
        verbose_name_plural = _("Account statistics")
