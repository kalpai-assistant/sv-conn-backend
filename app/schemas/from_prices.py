from uuid import UUID

from pydantic import BaseModel


class VariantFromPrice(BaseModel):
    """One active variant's "from ₹X" on the service sheet (SVC-496).

    Plain float, never Decimal; no default, so both stay ``required``.
    """

    variant_id: UUID
    # The service's rule on this variant alone: the lowest in-range price among
    # the SPs the sheet can list right now, else their lowest (out-of-range)
    # price. NULL when nobody listable prices it: the client greys it out.
    from_price: float | None
    # True when from_price is that out-of-range fallback (no listable SP is in
    # this variant's recommended range), so the client can label it. False when
    # from_price is in range or NULL; always false in a non-sensitive category.
    outside_recommended_range: bool


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
    # Every ACTIVE variant of the service, in sort_order, each with its own
    # from_price ([] when the service has none). Never filtered: an unpriced
    # variant is listed with NULL, so a missing variant_id means an inactive one.
    # from_price above is NOT min(variants): an in-range price on one variant
    # beats a cheaper out-of-range one on another.
    variants: list[VariantFromPrice]
