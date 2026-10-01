from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CreateServiceRequest(BaseModel):
    category_id: UUID
    name: str = Field(..., min_length=3, max_length=200)
    description: str | None = Field(None, max_length=2000)
    base_price: float = Field(..., gt=0)
    duration_minutes: int = Field(default=60, ge=15, le=480)


class UpdateServiceRequest(BaseModel):
    name: str | None = Field(None, min_length=3, max_length=200)
    description: str | None = None
    base_price: float | None = Field(None, gt=0)
    duration_minutes: int | None = Field(None, ge=15, le=480)
    is_active: bool | None = None


class ServiceVariantResponse(BaseModel):
    """One priced option under a service.

    ``base_price`` is what the customer sees before GST; ``gst_percent`` is 0
    for the launch summary rows and 18 for the vertical sheets. Frontend
    computes the customer total as ``base_price * (1 + gst_percent/100)``
    when rendering the checkout.

    ``provider_payout`` + ``platform_commission`` are stored, not derived,
    because the split ratio isn't a global constant — the launch summary
    rows are 70/30 and the vertical sheets are 65/35. The booking service
    snapshots these fields into ``booking_items`` at booking time.
    """

    id: UUID
    label: str
    subcategory: str | None
    base_price: float
    provider_payout: float
    platform_commission: float
    gst_percent: float
    duration_minutes: int | None
    materials_note: str | None
    sort_order: int
    is_active: bool

    model_config = {"from_attributes": True}


class ServiceResponse(BaseModel):
    id: UUID
    category_id: UUID
    name: str
    description: str | None
    base_price: float
    duration_minutes: int
    is_active: bool
    created_at: datetime
    # Empty list until the variants relationship is loaded. Routes that
    # render variants call an attach helper first — the model relationship
    # is `lazy="raise_on_sql"` and will refuse an implicit lookup here.
    variants: list[ServiceVariantResponse] = []
    # SVC-293: banner media, both nullable. Frontend falls through a chain
    # (sub-cat banner → L1 banner → colour block) when they're null, so a
    # service without curated media renders intentionally.
    banner_image_url: str | None = None
    banner_video_url: str | None = None

    model_config = {"from_attributes": True}


class RelatedServiceEntry(BaseModel):
    """One entry in a related-services rail on the service detail page (SVC-292).

    Denormalises name + base_price so the frontend renders the three rails
    in the same round trip that fetches the service itself — no per-row
    lookups from the client. ``sort_order`` is preserved so the frontend
    respects the seed's / admin's ordering.
    """

    id: UUID
    name: str
    base_price: float
    sort_order: int

    model_config = {"from_attributes": True}


class RelatedServicesBucket(BaseModel):
    """The three related-services surfaces the redesigned service page renders.

    Names track the three ``relationship_type`` enum values so the mapping
    from row to render surface is 1:1 — no server-side re-shaping. An empty
    list is a legitimate answer (a service with no seeded hooks, for example)
    and the frontend renders nothing for that surface.
    """

    hook: list[RelatedServiceEntry] = []
    upsell: list[RelatedServiceEntry] = []
    related: list[RelatedServiceEntry] = []


class ServiceDetailResponse(ServiceResponse):
    """``GET /services/{id}`` response — the ServiceResponse shape plus the
    three related-services buckets from SVC-291.

    Kept as a subclass so the plain-list ``GET /services`` endpoint stays
    lean (no related payload per row across dozens of catalogue entries)
    and the per-service fetch pays exactly one extra roundtrip's worth of
    joins to surface the rails."""

    related: RelatedServicesBucket = RelatedServicesBucket()
    # SVC-340: what the Pro does (ordered) and what the service doesn't cover.
    # Detail-only — the list stays lean. None means no data; the frontend
    # hides the section, as it does for an empty list.
    steps: list[str] | None = None
    exclusions: list[str] | None = None


class CategoryResponse(BaseModel):
    id: UUID
    name: str
    slug: str
    icon_url: str | None
    description: str | None
    sort_order: int
    # SVC-286: NULL ⇒ Level-1 (home tile). Non-NULL ⇒ Level-2 (sub-category
    # rendered inside its parent's category page). Frontend filters the flat
    # /categories response client-side using this field; keeping the endpoint
    # backward-compatible (returns everything) meant an old frontend
    # continued to render fine during the deploy window.
    parent_id: UUID | None

    model_config = {"from_attributes": True}
