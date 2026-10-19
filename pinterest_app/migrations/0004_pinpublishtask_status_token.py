import uuid

from django.db import migrations, models


def populate_status_tokens(apps, schema_editor):
    task_model = apps.get_model("pinterest_app", "PinPublishTask")
    database = schema_editor.connection.alias
    for task in task_model.objects.using(database).filter(status_token__isnull=True).iterator():
        task.status_token = uuid.uuid4()
        task.save(update_fields=["status_token"])


class Migration(migrations.Migration):

    dependencies = [
        ("pinterest_app", "0003_pinpublishtask"),
    ]

    operations = [
        migrations.AddField(
            model_name="pinpublishtask",
            name="status_token",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(populate_status_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="pinpublishtask",
            name="status_token",
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                unique=True,
                verbose_name="Status token",
            ),
        ),
    ]
