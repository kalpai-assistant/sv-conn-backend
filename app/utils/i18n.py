"""The SVC-374 kill switch: whether Hindi/Marathi apply to a user at all.

``I18N_MODE`` (config) is ``off``, ``testers`` or ``on``. It gates display,
not storage: a user's ``preferred_language`` is saved whatever the mode, and
only takes effect (in pushes, and in the app via ``i18n_enabled`` on
``GET /users/me``) while this returns True for them.
"""

from app.config import get_settings


def i18n_enabled(user) -> bool:
    """True when ``user`` (anything with a ``phone``) gets their language.

    Settings are read on every call rather than at import, so the Celery
    worker and the API agree and tests can flip the mode.
    """
    settings = get_settings()
    if settings.I18N_MODE == "on":
        return True
    if settings.I18N_MODE == "testers":
        testers = {p.strip() for p in settings.I18N_TESTER_PHONES.split(",") if p.strip()}
        return getattr(user, "phone", None) in testers
    return False
