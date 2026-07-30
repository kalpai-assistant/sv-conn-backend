from uuid import UUID

from pydantic import BaseModel


class CreatePaymentOrderRequest(BaseModel):
    booking_id: UUID


class PaymentOrderResponse(BaseModel):
    razorpay_order_id: str
    amount: int       # In paise (₹100 = 10000)
    currency: str = "INR"
    razorpay_key_id: str  # Public key for frontend checkout


class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class RazorpayWebhookPayload(BaseModel):
    event: str
    payload: dict
