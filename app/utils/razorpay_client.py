"""
Razorpay SDK wrapper — lazy singleton, avoids re-instantiation on every call.
All Razorpay interactions go through this module.
"""
import razorpay

from app.config import get_settings

settings = get_settings()

_client: razorpay.Client | None = None


def _get_client() -> razorpay.Client:
    global _client
    if _client is None:
        _client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )
    return _client


# Expose a module-level proxy object so callers can do:
#   from app.utils.razorpay_client import razorpay_client
#   razorpay_client.order.create(...)
class _RazorpayProxy:
    """Thin proxy that defers client creation until first use."""

    def __getattr__(self, item):
        return getattr(_get_client(), item)


razorpay_client = _RazorpayProxy()
