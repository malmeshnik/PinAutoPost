from django.db import migrations, models
import django.db.models


class Migration(migrations.Migration):

    dependencies = [
        ("pinterest_app", "0004_pinpublishtask_status_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="pinpublishtask",
            name="idempotency_key",
            field=models.CharField(blank=True, max_length=255, null=True, verbose_name="Idempotency key"),
        ),
        migrations.AddField(
            model_name="pinpublishtask",
            name="request_fingerprint",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                verbose_name="Request fingerprint",
            ),
        ),
        migrations.AddField(
            model_name="pinpublishtask",
            name="response_status_code",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pinpublishtask",
            name="response_content_type",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="pinpublishtask",
            name="response_body",
            field=models.BinaryField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="pinpublishtask",
            constraint=models.UniqueConstraint(
                condition=django.db.models.Q(idempotency_key__isnull=False),
                fields=("account", "idempotency_key"),
                name="unique_account_idempotency_key",
            ),
        ),
    ]
