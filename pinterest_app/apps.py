from django.apps import AppConfig
from django.contrib import admin
from django.utils.translation import gettext_lazy as _


class PinterestAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pinterest_app"
    verbose_name = _("Pinterest Application")

    def ready(self):
        admin.site.index_template = "admin/dashboard.html"
