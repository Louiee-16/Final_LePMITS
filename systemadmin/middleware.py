import time
import json
from django.contrib.auth import logout
from django.http import JsonResponse, HttpResponseRedirect
from django.middleware.csrf import rotate_token

class SessionIdleTimeoutMiddleware:
    
    EXEMPT_URLS = [
        '/session/status/',
        '/session/relogin/',
    ]
    
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            last_activity = request.session.get('last_activity')
            now = int(time.time())

            if last_activity and (now - last_activity) > 1800:
                logout(request)
                rotate_token(request)

                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'session_expired': True}, status=401)

                return HttpResponseRedirect('/login/?reason=timeout')

            # ✅ Only update last_activity for real user actions
            elif request.path not in self.EXEMPT_URLS:
                request.session['last_activity'] = now
                request.session.modified = True

        return self.get_response(request)