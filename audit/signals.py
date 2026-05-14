from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from .utils import log_action

@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    # It is good practice to check user here too
    if user:
        log_action(request, action='LOGIN', target=f'{user.username} logged in')

@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):

    if user:
        log_action(request, action='LOGOUT', target=f'{user.username} logged out')
    else:

        pass