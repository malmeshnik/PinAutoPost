from django.db import migrations, models


def remove_celery_health_monitor_entry(apps, schema_editor):
    # Health checks moved out of Celery (manage.py monitor_system_health), so
    # the old beat entry would keep sending a task that no longer exists.
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="system-health-monitor").delete()


class Migration(migrations.Migration):

    dependencies = [
        ('pinterest_app', '0007_systemalertstate'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.AddField(
            model_name='systemalertstate',
            name='payload',
            field=models.JSONField(blank=True, null=True, verbose_name='Payload'),
        ),
        migrations.RunPython(
            remove_celery_health_monitor_entry,
            migrations.RunPython.noop,
        ),
    ]
