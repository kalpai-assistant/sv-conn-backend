"""Structured address fields (SVC-287).

An address used to be one free-text line. A pin locates a building; it does not
say which flat, which floor, or that the entrance is behind the chemist — and an
address created from "use my current location" was never asked for any of it, so
a provider got a geocoded street and no door.

Three properties are load-bearing here, and the first is the only one whose
failure is silent:

1. **Copy-on-write carries every field.** `update_address` soft-deletes the old
   row and builds a new one. A column added to the table but missed in that
   constructor is dropped on *every edit* — with a 200 and nothing logged.
2. **`full_address` is composed on write, never recomposed.** Rows written
   before this keep their string; an edit that does not mention the structured
   fields must not rewrite the address line.
3. **The PATCH stays a PATCH.** `house_floor` is required by the create schema
   in due course, but demanding it on an edit would break changing a label.
"""

import pytest

from app.models.address import Address
from app.routers.users import compose_full_address
from app.schemas.user import CreateAddressRequest, UpdateAddressRequest
from app.services.user_service import AddressDetails, UserService
from tests.factories.user_factory import UserFactory

PUNE = (18.5204, 73.8567)

FULL_DETAILS = AddressDetails(
    house_floor="Flat 302, 3rd floor",
    building="Amogh Residency, B Wing",
    landmark_area="Near D-Mart",
    pincode="411014",
    contact_name="Asha Kharat",
    contact_phone="+919325709787",
)


async def _saved(db, user_id, *, details=FULL_DETAILS, label="home"):
    return await UserService(db).add_address(
        user_id=user_id,
        label=label,
        full_address="Kharadi, Pune",
        lat=PUNE[0],
        lng=PUNE[1],
        is_default=False,
        details=details,
    )


class TestCopyOnWriteCarriesEveryField:
    """The silent failure this ticket is most likely to ship."""

    @pytest.mark.asyncio
    async def test_editing_only_the_label_keeps_every_structured_field(self, db_session):
        # The device-shaped bug: save a flat number, later switch Home → Work,
        # and the flat number is gone. The request returns 200, nothing is
        # logged, and the provider reaches the building with no door to knock
        # on. Nobody finds out until a job goes wrong.
        user = await UserFactory.create(db_session)
        original = await _saved(db_session, user.id)

        updated = await UserService(db_session).update_address(
            user_id=user.id,
            address_id=original.id,
            label="work",
            full_address=None,
            lat=None,
            lng=None,
            is_default=None,
            details=None,
        )

        assert updated is not None
        assert updated.label == "work"
        # Every one, named individually: a loop here would pass while silently
        # checking nothing if the field list were ever emptied.
        assert updated.house_floor == "Flat 302, 3rd floor"
        assert updated.building == "Amogh Residency, B Wing"
        assert updated.landmark_area == "Near D-Mart"
        assert updated.pincode == "411014"
        assert updated.contact_name == "Asha Kharat"
        assert updated.contact_phone == "+919325709787"

    @pytest.mark.asyncio
    async def test_a_patch_overlays_rather_than_replaces(self, db_session):
        # Correcting the flat number must not clear the landmark the user typed
        # a week ago. Set fields win; unset ones keep what the row holds.
        user = await UserFactory.create(db_session)
        original = await _saved(db_session, user.id)

        updated = await UserService(db_session).update_address(
            user_id=user.id,
            address_id=original.id,
            label=None,
            full_address=None,
            lat=None,
            lng=None,
            is_default=None,
            details=AddressDetails(house_floor="Flat 404, 4th floor"),
        )

        assert updated is not None
        assert updated.house_floor == "Flat 404, 4th floor"
        assert updated.landmark_area == "Near D-Mart"
        assert updated.contact_phone == "+919325709787"

    @pytest.mark.asyncio
    async def test_an_address_saved_without_details_still_edits(self, db_session):
        # Every row written before today. Nothing to carry, and that must be
        # unremarkable rather than an error.
        user = await UserFactory.create(db_session)
        original = await _saved(db_session, user.id, details=None)

        updated = await UserService(db_session).update_address(
            user_id=user.id,
            address_id=original.id,
            label="other",
            full_address=None,
            lat=None,
            lng=None,
            is_default=None,
            details=None,
        )

        assert updated is not None
        assert updated.label == "other"
        assert updated.house_floor is None


class TestComposingTheAddressLine:
    """What the provider actually reads on the way to the job."""

    def test_reads_in_the_order_someone_navigates(self):
        # Flat first, then the building, then what to look for, then the area
        # and PIN. A provider reading this is navigating, not filing.
        composed = compose_full_address(FULL_DETAILS, "Kharadi, Pune")

        assert composed == (
            "Flat 302, 3rd floor, Amogh Residency, B Wing, Near D-Mart, Kharadi, Pune, 411014"
        )

    def test_keeps_the_client_string_when_there_is_nothing_structured(self):
        # An older app build sends only `full_address`. It must behave exactly
        # as it did before this ticket.
        assert compose_full_address(None, "Kharadi, Pune") == "Kharadi, Pune"

    def test_skips_the_fields_the_user_left_empty(self):
        # Building and landmark are optional, and most people will skip at
        # least one. No stray ", ," where a field was not filled in.
        composed = compose_full_address(
            AddressDetails(house_floor="Flat 302", pincode="411014"), "Kharadi, Pune"
        )

        assert composed == "Flat 302, Kharadi, Pune, 411014"

    def test_never_exceeds_the_column(self):
        # String(500), and the request fields sum to more than that. Truncating
        # beats a driver error on an address the user has already typed.
        composed = compose_full_address(
            AddressDetails(
                house_floor="x" * 120,
                building="y" * 120,
                landmark_area="z" * 200,
                pincode="411014",
            ),
            "w" * 400,
        )

        assert len(composed) <= 500


class TestTheSchemasStayHonest:
    """The contract, where an over-tight rule breaks an existing caller."""

    def test_create_demands_a_door_number(self):
        # A pin finds the building; it does not find the door. This is the one
        # field a provider standing outside a twelve-flat block cannot work
        # without, and the whole ticket exists because it was never asked for.
        #
        # It shipped optional first — no client sent it then, so requiring it
        # would have 422'd every installed build for a field the app had no way
        # to provide. The form ships it now.
        with pytest.raises(ValueError):
            CreateAddressRequest(full_address="Kharadi, Pune")

    def test_create_accepts_one(self):
        request = CreateAddressRequest(full_address="Kharadi, Pune", house_floor="Flat 302")

        assert request.house_floor == "Flat 302"

    def test_create_rejects_a_blank_door_number(self):
        # `""` would satisfy a bare `str` and store an empty string, which
        # reads to a provider exactly like the missing field it replaced.
        with pytest.raises(ValueError):
            CreateAddressRequest(full_address="Kharadi, Pune", house_floor="")

    def test_edit_stays_a_patch(self):
        # Requiring house_floor here would make "rename this to Work"
        # impossible without re-sending the whole address.
        request = UpdateAddressRequest(label="work")

        assert request.house_floor is None

    @pytest.mark.parametrize("bad", ["41101", "4110145", "abc123", "411 014"])
    def test_rejects_a_pincode_that_is_not_six_digits(self, bad):
        # Fixed-width, so a length check is a real check — it catches the
        # common paste of a phone number or a half-typed code.
        with pytest.raises(ValueError):
            CreateAddressRequest(full_address="Kharadi, Pune", house_floor="Flat 302", pincode=bad)

    @pytest.mark.parametrize("bad", ["9325709787", "+9193257097", "+1 9325709787"])
    def test_rejects_a_contact_phone_that_is_not_e164_india(self, bad):
        # Matches `users.phone`, so a tap-to-call link does not have to care
        # which of the two the number came from.
        with pytest.raises(ValueError):
            CreateAddressRequest(
                full_address="Kharadi, Pune", house_floor="Flat 302", contact_phone=bad
            )

    def test_accepts_the_shapes_the_app_sends(self):
        request = CreateAddressRequest(
            full_address="Kharadi, Pune",
            house_floor="Flat 302",
            pincode="411014",
            contact_phone="+919325709787",
        )

        assert request.pincode == "411014"
        assert request.contact_phone == "+919325709787"


class TestTheColumnsAreOptional:
    """Nullable in the strict sense — the migration adds no requirement."""

    @pytest.mark.asyncio
    async def test_a_row_with_no_structured_fields_is_valid(self, db_session):
        from app.utils.geo import build_point_wkt

        user = await UserFactory.create(db_session)
        address = Address(
            user_id=user.id,
            label="home",
            full_address="Kharadi, Pune",
            location=build_point_wkt(*PUNE),
            is_default=False,
        )
        db_session.add(address)
        await db_session.flush()

        assert address.house_floor is None
        assert address.contact_phone is None
