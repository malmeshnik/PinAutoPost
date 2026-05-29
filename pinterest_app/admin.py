from django.contrib import admin
from .models import PinterestAccount, PinterestBoard
from .services import refresh_boards

class PinterestBoardInline(admin.TabularInline):
    model = PinterestBoard
    extra = 0
    readonly_fields = ('board_id', 'name', 'url')

@admin.register(PinterestAccount)
class PinterestAccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'is_active', 'created_at')
    list_filter = ('is_active', 'user')
    search_fields = ('name', 'user__username')
    inlines = [PinterestBoardInline]
    readonly_fields = ('webhook_token', 'created_at', 'updated_at')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(user=request.user)

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if not request.user.is_superuser:
            # Hide 'user' field for non-superusers if they are creating or editing
            if 'user' in fields:
                fields.remove('user')
        return fields

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            obj.user = request.user
        super().save_model(request, obj, form, change)

        # After saving, we can try to refresh boards
        if obj.is_active and obj.cookies and obj.proxy:
            refresh_boards(obj)

@admin.register(PinterestBoard)
class PinterestBoardAdmin(admin.ModelAdmin):
    list_display = ('name', 'board_id', 'account')
    list_filter = ('account',)
    search_fields = ('name', 'board_id')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(account__user=request.user)
