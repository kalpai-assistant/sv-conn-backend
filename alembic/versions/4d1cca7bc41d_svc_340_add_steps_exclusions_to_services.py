"""SVC-340: add steps + exclusions to services

Revision ID: 4d1cca7bc41d
Revises: b000e0a06330
Create Date: 2026-10-01 16:24:53.360405

Two nullable JSONB columns on ``services``, rendered on the service detail page:

- ``steps`` — what the Pro does, in order.
- ``exclusions`` — what the service does not cover.

NULL means "no data" and the frontend hides the section. No index (never
queried by these columns), no server default. Edited by SQL only; no admin UI.

Data migration, same revision: placeholder-but-plausible text for 10 services,
one per sub-category. Vipul supplies the final wording later.

``services`` has no ``slug`` column, so a service is identified the way
``scripts/seed_services.py`` keys it: (category slug, service name). Ids differ
between environments and are never used. Each UPDATE sets fixed values, so a
re-run is a no-op.

On CI and any fresh database the services table is empty at migration time
(services are seeded by scripts after ``alembic upgrade head``), so the UPDATEs
match nothing there. That is expected and must not fail the upgrade; the rows
only exist on beta.

Downgrade drops both columns; the seeded text goes with them.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '4d1cca7bc41d'
down_revision: Union[str, None] = 'b000e0a06330'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (category slug, service name) -> (steps, exclusions). Names are matched
# exactly, so they must stay in step with the live catalog.
SERVICE_DETAILS: dict[tuple[str, str], tuple[list[str], list[str]]] = {
    ('cleaning', 'Bathroom deep cleaning'): (
        [
            "Pro arrives with cleaning chemicals, brushes and a scrubbing machine",
            "Inspects the bathroom and confirms the areas to be cleaned with you",
            "Descales and scrubs tiles, floor, toilet, wash basin and fittings",
            "Cleans mirrors, glass and the exhaust fan cover",
            "Rinses and dries all surfaces and clears away waste",
        ],
        [
            "Does not include repair or replacement of fittings, tiles or grout",
            "Does not include removing permanent stains or rust from damaged surfaces",
            "Does not include cleaning the ceiling or walls above head height",
        ],
    ),
    ('salon-women', 'Facial'): (
        [
            "Professional arrives with a sanitised kit and single-use products",
            "Discusses your skin type and any allergies before starting",
            "Cleanses, exfoliates and steams the face",
            "Applies the face pack and gives a relaxing face massage",
            "Finishes with toner and moisturiser and cleans up the area",
        ],
        [
            "Does not include treatment of skin conditions such as acne or rashes",
            "Does not include threading, waxing or bleach",
            "Does not include products for you to keep after the session",
        ],
    ),
    ('salon-men', "Men's haircut"): (
        [
            "Professional arrives with sanitised tools and a fresh cape",
            "Discusses the style and length you want",
            "Cuts and shapes the hair to your chosen style",
            "Tidies the neckline and sideburns",
            "Cleans up the hair and the area after the cut",
        ],
        [
            "Does not include beard trim or styling",
            "Does not include hair wash, colour or treatments",
            "Does not include styling products for you to keep",
        ],
    ),
    ('plumber', 'Drain blockage removal'): (
        [
            "Pro arrives with drain-cleaning tools",
            "Locates the blocked drain and checks the cause",
            "Clears the blockage manually or with a drain snake",
            "Runs water to confirm the drain flows freely",
            "Cleans up the work area",
        ],
        [
            "Does not include replacement of broken or damaged pipes",
            "Does not include blockages inside walls, underground or in the main sewer line",
            "Does not include civil work such as breaking or re-laying tiles",
        ],
    ),
    ('ac', 'Split AC foam-jet service'): (
        [
            "Pro arrives with a foam-jet kit and protective covers",
            "Checks the AC's cooling and covers the wall and floor below the unit",
            "Cleans the filters, cooling coil and fan with foam and water jet",
            "Flushes the drain pipe and checks for leaks",
            "Reassembles the unit, tests cooling and cleans up",
        ],
        [
            "Does not include gas refilling or top-up",
            "Does not include repair or replacement of parts such as PCB, motor or compressor",
            "Does not include cleaning of the outdoor unit",
        ],
    ),
    ('electrician', 'Ceiling fan installation'): (
        [
            "Electrician arrives with tools and a tester",
            "Checks the ceiling hook and the wiring point",
            "Assembles the fan and fixes the blades",
            "Mounts the fan and connects the wiring and regulator",
            "Tests the fan at all speeds and cleans up",
        ],
        [
            "Does not include the fan, regulator or any other materials",
            "Does not include installing a new ceiling hook or new wiring",
            "Does not include removal of an existing fan",
        ],
    ),
    ('pest-control', 'Cockroach control'): (
        [
            "Pro arrives with odourless gel and spray treatment",
            "Inspects the kitchen and bathrooms for activity and hiding spots",
            "Applies gel in cracks, crevices and cabinet hinges",
            "Sprays drains and corners where needed",
            "Explains the precautions to follow after treatment",
        ],
        [
            "Does not include treatment for termites, bed bugs or rodents",
            "Does not include moving heavy furniture or appliances",
            "Does not include cleaning of utensils or cabinets before or after treatment",
        ],
    ),
    ('appliances', 'Washing machine repair'): (
        [
            "Technician arrives with diagnostic tools",
            "Inspects the machine and identifies the fault",
            "Shares the repair estimate before starting any work",
            "Repairs the fault once you approve",
            "Runs a test cycle to confirm it works",
        ],
        [
            "Does not include the cost of spare parts — charged separately at actuals",
            "Does not include repairs that need the machine taken to a workshop",
            "Does not include installation or uninstallation",
        ],
    ),
    ('carpenter', 'Furniture assembly'): (
        [
            "Carpenter arrives with assembly tools",
            "Checks all parts and fittings against the manual",
            "Assembles the furniture as per the manufacturer's instructions",
            "Tightens all joints and checks stability",
            "Clears packaging from the work area",
        ],
        [
            "Does not include missing or damaged parts and fittings",
            "Does not include wall mounting or drilling",
            "Does not include disposal of packaging outside your home",
        ],
    ),
    ('painting', 'Full room painting'): (
        [
            "Painter arrives with tools and covers for furniture and floor",
            "Moves and covers furniture and masks switches and fittings",
            "Fills minor cracks and sands the walls",
            "Applies primer and two coats of paint",
            "Removes covers and masking and cleans up",
        ],
        [
            "Does not include waterproofing or treatment of dampness",
            "Does not include painting of doors, windows, grills or furniture",
            "Does not include major wall repair or plastering",
        ],
    ),
}

# Scoped by (category slug, service name) — ``services`` has no slug. Fixed
# values make a re-run a no-op; a key with no matching row updates nothing.
UPDATE_SERVICE_DETAILS = """
    UPDATE services s
    SET steps = CAST(:steps AS jsonb),
        exclusions = CAST(:exclusions AS jsonb)
    FROM categories c
    WHERE c.id = s.category_id
      AND c.slug = :category_slug
      AND s.name = :name
"""


def service_details_params() -> list[dict]:
    """One bound-parameter set per seeded service, for UPDATE_SERVICE_DETAILS."""
    return [
        {
            'category_slug': category_slug,
            'name': name,
            'steps': json.dumps(steps),
            'exclusions': json.dumps(exclusions),
        }
        for (category_slug, name), (steps, exclusions) in SERVICE_DETAILS.items()
    ]


def upgrade() -> None:
    op.add_column('services', sa.Column('steps', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('services', sa.Column('exclusions', postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    bind = op.get_bind()
    for params in service_details_params():
        bind.execute(sa.text(UPDATE_SERVICE_DETAILS), params)


def downgrade() -> None:
    op.drop_column('services', 'exclusions')
    op.drop_column('services', 'steps')
