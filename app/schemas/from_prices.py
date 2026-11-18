from uuid import UUID

from pydantic import BaseModel


class ServiceFromPrice(BaseModel):
    """One service's tile price at the customer's point (SVC-415).

    ``GET /services/from-prices`` returns one of these per ACTIVE service. A
    service_id missing from the list is an inactive service: the client shows
    its catalogue price, as when the request fails.

    Plain float, never Decimal: a Decimal serialises as a JSON string. No field
    has a default, so every one stays ``required`` in the OpenAPI schema.
    """

    service_id: UUID
    # The lowest price among the SPs the sheet can list right now: in-range
    # prices first, the lowest out-of-range one only if the service has no
    # in-range price on any variant. NULL when nobody is listable now.
    from_price: float | None
    # Some SP covers this point with an active price, online or not. Always
    # true when from_price is set; false means "not in your area yet".
    served: bool
