from .models import SystemSetting

def system_settings(request):

    settings, created = SystemSetting.objects.get_or_create(id=1)
    return {'settings': settings}