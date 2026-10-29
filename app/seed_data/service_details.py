"""Service detail content — steps, exclusions, included items and FAQs (SVC-340, SVC-400).

Single source of truth. The migrations and both seed scripts
(``scripts/seed_services.py``, ``scripts/seed_pricing_from_xlsx.py``) import
from here, so a fresh DB seeded by the scripts lands on the same text as beta,
and a pricing re-seed (which deletes and recreates every service) puts it back.
Edit this file to change the wording, then add a small migration that re-runs
``UPDATE_SERVICE_DETAILS`` for the deployed DBs. Do NOT re-declare the text
inside a migration or a seed script.

``services`` has no slug, so a service is keyed by (category slug, service
name), the way ``seed_services.py`` keys it. Names are matched exactly and must
stay in step with ``EazyServ_Pune_Pricing_List.xlsx``; a test guards that.

Steps use the SVC-400 object shape ``{title, duration_minutes?, icon?}``, one
shape per service: a mixed list fails ``ServiceDetailResponse`` and 500s the
detail endpoint. ``duration_minutes`` is set only where a real step time is
known. ``icon`` must be one of the keywords the frontend maps (see SVC-400).
Never put ratings, job counts, guarantees, insurance or warranty claims here.
"""

from __future__ import annotations

import json

from sqlalchemy import text

# Shared by every service, appended after its own FAQs when the params are
# built (service_details_params), so it is written once.
COMMON_FAQS: list[dict[str, str]] = [
    {
        "question": "Can I cancel or reschedule?",
        "answer": (
            "Yes, free of charge up to 2 hours before the scheduled time. Cancelling or "
            "rescheduling within 2 hours of the visit may attract a fee."
        ),
    },
    {
        "question": "How do I pay?",
        "answer": "Pay online in the app or in cash to the Pro after the service.",
    },
    {
        "question": "What if I'm not happy with the service?",
        "answer": (
            "Contact our support team and we'll look into it. Refunds are reviewed case by "
            "case under our Refund & Cancellation Policy."
        ),
    },
]

# (category slug, service name) -> {steps, exclusions, included_items, faqs}.
# One service per sub-category. ``faqs`` is the service's own; COMMON_FAQS is
# added on top.
SERVICE_DETAILS: dict[tuple[str, str], dict[str, list]] = {
    ("cleaning", "Bathroom deep cleaning"): {
        "steps": [
            {
                "title": "Pro arrives with cleaning chemicals, brushes and a scrubbing machine",
                "duration_minutes": 5,
                "icon": "bath",
            },
            {
                "title": "Inspects the bathroom and confirms the areas to be cleaned with you",
                "duration_minutes": 5,
                "icon": "inspect",
            },
            {
                "title": "Descales and scrubs tiles, floor, toilet, wash basin and fittings",
                "duration_minutes": 30,
                "icon": "mop",
            },
            {
                "title": "Cleans mirrors, glass and the exhaust fan cover",
                "duration_minutes": 10,
                "icon": "sparkles",
            },
            {
                "title": "Rinses and dries all surfaces and clears away waste",
                "duration_minutes": 10,
                "icon": "finish",
            },
        ],
        "exclusions": [
            "Does not include repair or replacement of fittings, tiles or grout",
            "Does not include removing permanent stains or rust from damaged surfaces",
            "Does not include cleaning the ceiling or walls above head height",
        ],
        "included_items": [
            "Tiles, floor and walls up to head height",
            "Toilet, wash basin, taps and fittings",
            "Mirrors, glass and exhaust fan cover",
            "Cleaning chemicals, brushes and scrubbing machine brought by the Pro",
        ],
        "faqs": [
            {
                "question": "Do I need to provide anything?",
                "answer": (
                    "No. The Pro brings all chemicals and equipment. Just keep water and a "
                    "power point available."
                ),
            },
            {
                "question": "Are the chemicals safe?",
                "answer": (
                    "The Pro uses standard bathroom-cleaning chemicals. Keep children and pets "
                    "out of the bathroom while it's being cleaned, and tell the Pro about any "
                    "surfaces that need gentle handling."
                ),
            },
            {
                "question": "Will old stains come off?",
                "answer": (
                    "Most dirt and hard-water marks come off. Permanent stains, rust or "
                    "damaged surfaces may not."
                ),
            },
        ],
    },
    ("salon-women", "Facial"): {
        "steps": [
            {
                "title": "Professional arrives with a sanitised kit and single-use products",
                "duration_minutes": 5,
                "icon": "sparkles",
            },
            {
                "title": "Discusses your skin type and any allergies before starting",
                "duration_minutes": 5,
                "icon": "inspect",
            },
            {
                "title": "Cleanses, exfoliates and steams the face",
                "duration_minutes": 20,
                "icon": "sparkles",
            },
            {
                "title": "Applies the face pack and gives a relaxing face massage",
                "duration_minutes": 25,
                "icon": "sparkles",
            },
            {
                "title": "Finishes with toner and moisturiser and cleans up the area",
                "duration_minutes": 5,
                "icon": "finish",
            },
        ],
        "exclusions": [
            "Does not include treatment of skin conditions such as acne or rashes",
            "Does not include threading, waxing or bleach",
            "Does not include products for you to keep after the session",
        ],
        "included_items": [
            "Cleansing, exfoliation and steam",
            "Face pack and face massage",
            "Toner and moisturiser",
            "Sanitised kit and single-use products",
        ],
        "faqs": [
            {
                "question": "I have sensitive skin. Is it safe?",
                "answer": (
                    "Tell the professional about any allergies or sensitivities before "
                    "starting, and they'll adjust the products. If you have an active skin "
                    "condition, check with a doctor first."
                ),
            },
            {
                "question": "What do I need to arrange?",
                "answer": (
                    "A chair near a power point and a little space. The professional brings "
                    "everything else."
                ),
            },
            {
                "question": "Can I get threading or waxing in the same visit?",
                "answer": "Not as part of this service. Book those separately.",
            },
        ],
    },
    ("salon-men", "Men's haircut"): {
        "steps": [
            {
                "title": "Professional arrives with sanitised tools and a fresh cape",
                "duration_minutes": 5,
                "icon": "sparkles",
            },
            {
                "title": "Discusses the style and length you want",
                "duration_minutes": 5,
                "icon": "inspect",
            },
            {
                "title": "Cuts and shapes the hair to your chosen style",
                "duration_minutes": 35,
                "icon": "sparkles",
            },
            {
                "title": "Tidies the neckline and sideburns",
                "duration_minutes": 10,
                "icon": "sparkles",
            },
            {
                "title": "Cleans up the hair and the area after the cut",
                "duration_minutes": 5,
                "icon": "finish",
            },
        ],
        "exclusions": [
            "Does not include beard trim or styling",
            "Does not include hair wash, colour or treatments",
            "Does not include styling products for you to keep",
        ],
        "included_items": [
            "Haircut and shaping to your chosen style",
            "Neckline and sideburn tidy-up",
            "Sanitised tools and a fresh cape",
            "Clean-up of hair and the area afterwards",
        ],
        "faqs": [
            {
                "question": "Is a beard trim included?",
                "answer": "No. Beard trim and styling aren't part of this service.",
            },
            {
                "question": "Where will the haircut happen?",
                "answer": (
                    "Anywhere with a chair and some floor space. The professional cleans up "
                    "the hair afterwards."
                ),
            },
            {
                "question": "Is hair wash included?",
                "answer": "No. Hair wash, colour and treatments aren't included.",
            },
        ],
    },
    ("plumber", "Drain blockage removal"): {
        "steps": [
            {
                "title": "Pro arrives with drain-cleaning tools",
                "duration_minutes": 5,
                "icon": "search",
            },
            {
                "title": "Locates the blocked drain and checks the cause",
                "duration_minutes": 10,
                "icon": "inspect",
            },
            {
                "title": "Clears the blockage manually or with a drain snake",
                "duration_minutes": 30,
                "icon": "sparkles",
            },
            {
                "title": "Runs water to confirm the drain flows freely",
                "duration_minutes": 10,
                "icon": "sparkles",
            },
            {
                "title": "Cleans up the work area",
                "duration_minutes": 5,
                "icon": "finish",
            },
        ],
        "exclusions": [
            "Does not include replacement of broken or damaged pipes",
            "Does not include blockages inside walls, underground or in the main sewer line",
            "Does not include civil work such as breaking or re-laying tiles",
        ],
        "included_items": [
            "Locating and checking the blocked drain",
            "Clearing the blockage manually or with a drain snake",
            "Flow test after clearing",
            "Clean-up of the work area",
        ],
        "faqs": [
            {
                "question": "What if the pipe is broken?",
                "answer": (
                    "Replacing broken or damaged pipes isn't included. The Pro will tell you "
                    "what's needed before doing any extra work."
                ),
            },
            {
                "question": "Can you clear the main sewer line?",
                "answer": (
                    "No. Blockages inside walls, underground or in the main sewer line are "
                    "outside this service."
                ),
            },
            {
                "question": "Will you break tiles?",
                "answer": "No. Civil work such as breaking or re-laying tiles isn't included.",
            },
        ],
    },
    # Step times pending real durations from Vipul: title + icon only.
    ("ac", "Split AC foam-jet service"): {
        "steps": [
            {"title": "Pro arrives with a foam-jet kit and protective covers", "icon": "wind"},
            {
                "title": "Checks the AC's cooling and covers the wall and floor below the unit",
                "icon": "inspect",
            },
            {
                "title": "Cleans the filters, cooling coil and fan with foam and water jet",
                "icon": "wind",
            },
            {"title": "Flushes the drain pipe and checks for leaks", "icon": "search"},
            {"title": "Reassembles the unit, tests cooling and cleans up", "icon": "finish"},
        ],
        "exclusions": [
            "Does not include gas refilling or top-up",
            "Does not include repair or replacement of parts such as PCB, motor or compressor",
            "Does not include cleaning of the outdoor unit",
        ],
        "included_items": [
            "Foam and water-jet cleaning of filters, cooling coil and fan",
            "Drain pipe flush and leak check",
            "Wall and floor covers during the service",
            "Cooling test after reassembly",
        ],
        "faqs": [
            {
                "question": "Is gas refilling included?",
                "answer": "No. If the Pro finds low gas, they'll tell you before any extra work.",
            },
            {
                "question": "Is the outdoor unit cleaned?",
                "answer": "No, this service covers the indoor unit.",
            },
            {
                "question": "Will water spill in my room?",
                "answer": "The Pro covers the wall and floor below the unit before the jet wash.",
            },
        ],
    },
    ("electrician", "Ceiling fan installation"): {
        "steps": [
            {
                "title": "Electrician arrives with tools and a tester",
                "duration_minutes": 5,
                "icon": "search",
            },
            {
                "title": "Checks the ceiling hook and the wiring point",
                "duration_minutes": 10,
                "icon": "inspect",
            },
            {
                "title": "Assembles the fan and fixes the blades",
                "duration_minutes": 20,
                "icon": "sparkles",
            },
            {
                "title": "Mounts the fan and connects the wiring and regulator",
                "duration_minutes": 20,
                "icon": "sparkles",
            },
            {
                "title": "Tests the fan at all speeds and cleans up",
                "duration_minutes": 5,
                "icon": "finish",
            },
        ],
        "exclusions": [
            "Does not include the fan, regulator or any other materials",
            "Does not include installing a new ceiling hook or new wiring",
            "Does not include removal of an existing fan",
        ],
        "included_items": [
            "Checking the ceiling hook and wiring point",
            "Fan assembly and blade fitting",
            "Mounting, wiring and regulator connection",
            "Test at all speeds",
        ],
        "faqs": [
            {
                "question": "Do I need to buy the fan?",
                "answer": "Yes. The fan, regulator and any other materials aren't included.",
            },
            {
                "question": "What if there's no ceiling hook?",
                "answer": "Installing a new hook or new wiring isn't part of this service.",
            },
            {
                "question": "Can you remove my old fan?",
                "answer": "Removing an existing fan isn't included.",
            },
        ],
    },
    # Step times pending real durations from Vipul: title + icon only.
    ("pest-control", "Cockroach control"): {
        "steps": [
            {"title": "Pro arrives with odourless gel and spray treatment", "icon": "sparkles"},
            {
                "title": "Inspects the kitchen and bathrooms for activity and hiding spots",
                "icon": "inspect",
            },
            {"title": "Applies gel in cracks, crevices and cabinet hinges", "icon": "kitchen"},
            {"title": "Sprays drains and corners where needed", "icon": "bath"},
            {"title": "Explains the precautions to follow after treatment", "icon": "review"},
        ],
        "exclusions": [
            "Does not include treatment for termites, bed bugs or rodents",
            "Does not include moving heavy furniture or appliances",
            "Does not include cleaning of utensils or cabinets before or after treatment",
        ],
        "included_items": [
            "Inspection of kitchen and bathrooms",
            "Odourless gel in cracks, crevices and cabinet hinges",
            "Spray treatment for drains and corners where needed",
            "Aftercare precautions explained",
        ],
        "faqs": [
            {
                "question": "Do I need to leave the house?",
                "answer": (
                    "The Pro will explain the precautions for your home during the visit, "
                    "including for children and pets."
                ),
            },
            {
                "question": "Should I empty the kitchen cabinets?",
                "answer": (
                    "Clearing the area helps. Cleaning utensils or cabinets before or after "
                    "treatment isn't included."
                ),
            },
            {
                "question": "Does it cover termites or bed bugs?",
                "answer": "No. Termites, bed bugs and rodents need a different treatment.",
            },
        ],
    },
    # Step times pending real durations from Vipul: title + icon only.
    ("appliances", "Washing machine repair"): {
        "steps": [
            {"title": "Technician arrives with diagnostic tools", "icon": "search"},
            {"title": "Inspects the machine and identifies the fault", "icon": "inspect"},
            {"title": "Shares the repair estimate before starting any work", "icon": "review"},
            {"title": "Repairs the fault once you approve", "icon": "sparkles"},
            {"title": "Runs a test cycle to confirm it works", "icon": "finish"},
        ],
        "exclusions": [
            "Does not include the cost of spare parts — charged separately at actuals",
            "Does not include repairs that need the machine taken to a workshop",
            "Does not include installation or uninstallation",
        ],
        "included_items": [
            "Inspection and fault diagnosis",
            "Repair estimate shared before any work",
            "Repair once you approve",
            "Test cycle afterwards",
        ],
        "faqs": [
            {
                "question": "Are spare parts included in the price?",
                "answer": (
                    "No. Spare parts are charged separately at actuals, and installed parts "
                    "are non-refundable."
                ),
            },
            {
                "question": "What if it can't be fixed at home?",
                "answer": (
                    "Repairs that need a workshop aren't part of this service. The technician "
                    "will tell you before going further."
                ),
            },
            {
                "question": "Will I know the cost before the repair?",
                "answer": (
                    "Yes. The technician shares the estimate and only starts once you approve."
                ),
            },
        ],
    },
    ("carpenter", "Furniture assembly"): {
        "steps": [
            {
                "title": "Carpenter arrives with assembly tools",
                "duration_minutes": 5,
                "icon": "search",
            },
            {
                "title": "Checks all parts and fittings against the manual",
                "duration_minutes": 10,
                "icon": "inspect",
            },
            {
                "title": "Assembles the furniture as per the manufacturer's instructions",
                "duration_minutes": 35,
                "icon": "sparkles",
            },
            {
                "title": "Tightens all joints and checks stability",
                "duration_minutes": 5,
                "icon": "review",
            },
            {
                "title": "Clears packaging from the work area",
                "duration_minutes": 5,
                "icon": "finish",
            },
        ],
        "exclusions": [
            "Does not include missing or damaged parts and fittings",
            "Does not include wall mounting or drilling",
            "Does not include disposal of packaging outside your home",
        ],
        "included_items": [
            "Parts check against the manual",
            "Assembly to the manufacturer's instructions",
            "Joint tightening and stability check",
            "Packaging cleared from the work area",
        ],
        "faqs": [
            {
                "question": "What if parts are missing?",
                "answer": (
                    "Missing or damaged parts aren't included. The carpenter will point them "
                    "out at the parts check."
                ),
            },
            {
                "question": "Can you mount it on the wall?",
                "answer": "Wall mounting and drilling aren't part of this service.",
            },
            {
                "question": "Will you take the boxes away?",
                "answer": (
                    "The carpenter clears packaging from the work area. Disposal outside your "
                    "home isn't included."
                ),
            },
        ],
    },
    # Step times pending real durations from Vipul: title + icon only. "Is paint
    # included?" is left out until Vipul says who supplies the paint.
    ("painting", "Full room painting"): {
        "steps": [
            {
                "title": "Painter arrives with tools and covers for furniture and floor",
                "icon": "sparkles",
            },
            {
                "title": "Moves and covers furniture and masks switches and fittings",
                "icon": "inspect",
            },
            {"title": "Fills minor cracks and sands the walls", "icon": "dust"},
            {"title": "Applies primer and two coats of paint", "icon": "sparkles"},
            {"title": "Removes covers and masking and cleans up", "icon": "finish"},
        ],
        "exclusions": [
            "Does not include waterproofing or treatment of dampness",
            "Does not include painting of doors, windows, grills or furniture",
            "Does not include major wall repair or plastering",
        ],
        "included_items": [
            "Furniture and floor covered, switches and fittings masked",
            "Minor crack filling and sanding",
            "Primer plus two coats of paint",
            "Covers removed and clean-up",
        ],
        "faqs": [
            {
                "question": "Does it fix damp walls?",
                "answer": "No. Waterproofing and dampness treatment aren't included.",
            },
            {
                "question": "Are doors and windows painted?",
                "answer": ("No. Doors, windows, grills and furniture aren't part of this service."),
            },
        ],
    },
}

# Scoped by (category slug, service name) — ``services`` has no slug. Fixed
# values make a re-run a no-op; a key with no matching row updates nothing.
# hero_image_url and before_after_photos are deliberately not set (SVC-401).
UPDATE_SERVICE_DETAILS = """
    UPDATE services s
    SET steps = CAST(:steps AS jsonb),
        exclusions = CAST(:exclusions AS jsonb),
        included_items = CAST(:included_items AS jsonb),
        faqs = CAST(:faqs AS jsonb)
    FROM categories c
    WHERE c.id = s.category_id
      AND c.slug = :category_slug
      AND s.name = :name
"""

# Frozen: SVC-340 migration only; never edit. Migration 4d1cca7bc41d runs
# before included_items / faqs exist, so its statement must not name them. The
# SVC-400 content migration's downgrade reuses it to put the string steps back.
UPDATE_SERVICE_DETAILS_SVC_340 = """
    UPDATE services s
    SET steps = CAST(:steps AS jsonb),
        exclusions = CAST(:exclusions AS jsonb)
    FROM categories c
    WHERE c.id = s.category_id
      AND c.slug = :category_slug
      AND s.name = :name
"""


def service_details_params() -> list[dict]:
    """One bound-parameter set per seeded service, for UPDATE_SERVICE_DETAILS.

    COMMON_FAQS goes after each service's own FAQs."""
    return [
        {
            "category_slug": category_slug,
            "name": name,
            "steps": json.dumps(details["steps"]),
            "exclusions": json.dumps(details["exclusions"]),
            "included_items": json.dumps(details["included_items"]),
            "faqs": json.dumps(details["faqs"] + COMMON_FAQS),
        }
        for (category_slug, name), details in SERVICE_DETAILS.items()
    ]


def svc_340_details_params() -> list[dict]:
    """Parameters for UPDATE_SERVICE_DETAILS_SVC_340: the SVC-340 string steps
    (the step titles, which SVC-400 kept word for word) and the exclusions."""
    return [
        {
            "category_slug": category_slug,
            "name": name,
            "steps": json.dumps([step["title"] for step in details["steps"]]),
            "exclusions": json.dumps(details["exclusions"]),
        }
        for (category_slug, name), details in SERVICE_DETAILS.items()
    ]


async def apply_service_details(db) -> list[tuple[str, str]]:
    """Run UPDATE_SERVICE_DETAILS for every seeded service on an AsyncSession.

    For the seed scripts, after they have created services. Flushes first so
    rows still pending in the session are visible to the UPDATE. Does not
    commit. Returns the (category slug, name) keys that matched no row, so the
    caller can warn — a renamed service would otherwise lose its text silently.
    """
    await db.flush()
    unmatched = []
    for params in service_details_params():
        result = await db.execute(text(UPDATE_SERVICE_DETAILS), params)
        if result.rowcount == 0:
            unmatched.append((params["category_slug"], params["name"]))
    return unmatched
