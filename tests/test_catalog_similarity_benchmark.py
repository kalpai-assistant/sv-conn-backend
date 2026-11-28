"""SVC-437 benchmark: ``POST /catalog/similarity`` p95 < 100 ms on a 500-service catalog.

Opt-in. ``stability`` marks it as long-running, but that marker skips nothing on
its own (CI runs plain ``pytest tests/``), so it also needs
``RUN_SIMILARITY_BENCHMARK=1``. A slow shared runner can't flake CI on a timing.

    RUN_SIMILARITY_BENCHMARK=1 uv run pytest tests/test_catalog_similarity_benchmark.py -s

The search is unfiltered (``category_id`` null), so it scans every active
service in the test DB: the 500 seeded here plus any rows earlier tests left.
"""

import itertools
import os
import statistics
import time
import uuid
from decimal import Decimal

import pytest

from app.models.category import Category
from app.models.provider_profile import ProviderProfile
from app.models.service import Service
from app.models.service_request import ServiceRequest
from tests.factories.user_factory import UserFactory

pytestmark = [
    pytest.mark.stability,
    pytest.mark.skipif(
        not os.getenv("RUN_SIMILARITY_BENCHMARK"),
        reason="opt-in timing benchmark: set RUN_SIMILARITY_BENCHMARK=1",
    ),
]

URL = "/api/v1/catalog/similarity"
SERVICES = 500
WARMUP = 5
TIMED = 60
P95_BUDGET_MS = 100

_ITEMS = [
    "AC", "sofa", "kitchen", "bathroom", "window", "geyser", "chimney", "water tank",
    "carpet", "mattress", "fan", "washing machine", "fridge", "RO purifier", "balcony",
    "wardrobe", "door lock", "tap", "sink", "wall",
]  # fmt: skip
_ACTIONS = [
    "cleaning", "repair", "installation", "deep cleaning", "servicing", "painting",
    "polishing", "replacement", "inspection", "uninstallation", "gas refill", "shampooing",
    "waterproofing", "fitting", "dismantling",
]  # fmt: skip
_QUERIES = [
    "Clean windows", "AC servicing", "sofa shampoo", "kitchen chimney cleaning",
    "fridge repairing", "geyser install", "bathroom deep clean", "wall paint",
]  # fmt: skip


def _p(samples: list[float], pct: int) -> float:
    return statistics.quantiles(samples, n=100, method="inclusive")[pct - 1]


async def test_similarity_endpoint_p95_under_100ms_on_500_services(
    client, db_session, redis_client, auth_headers
):
    """FAILURE CAUGHT: a query change (a lost LIMIT, a per-row subquery, an N+1
    in the composite) pushes the endpoint past the ticket's 100 ms p95."""
    tag = uuid.uuid4().hex[:6]
    l1 = Category(name=f"svc437-bench-{tag}", slug=f"svc437-bench-{tag}")
    db_session.add(l1)
    await db_session.flush()
    children = [
        Category(name=f"svc437-bench-{tag}-{i}", slug=f"svc437-bench-{tag}-{i}", parent_id=l1.id)
        for i in range(10)
    ]
    db_session.add_all(children)
    await db_session.flush()

    names = itertools.cycle(itertools.product(_ITEMS, _ACTIONS))
    for i in range(SERVICES):
        item, action = next(names)
        db_session.add(
            Service(
                category_id=children[i % len(children)].id,
                name=f"{item} {action} {i}".capitalize(),
                # A third have a description, like beta (45 of 363).
                description=f"Professional {action} for your {item}." if i % 3 == 0 else None,
                base_price=Decimal("499.00"),
            )
        )
    caller = await UserFactory.create(db_session, role="provider")
    other = await UserFactory.create(db_session, role="provider")
    db_session.add_all([ProviderProfile(user_id=caller.id), ProviderProfile(user_id=other.id)])
    await db_session.flush()
    for i in range(20):
        db_session.add(
            ServiceRequest(
                sp_id=other.id,
                proposed_name=f"{_ITEMS[i]} {_ACTIONS[i % len(_ACTIONS)]} pending",
                proposed_description="Pending proposal.",
                proposed_category_id=children[i % len(children)].id,
                proposed_price=Decimal("399.00"),
                proposed_price_unit="PER_VISIT",
            )
        )
    await db_session.flush()

    headers = auth_headers(str(caller.id), "provider")
    rate_key = f"rl:catalog_similarity:{caller.id}"
    samples: list[float] = []
    for i in range(WARMUP + TIMED):
        await redis_client.delete(rate_key)  # outside the timed window
        body = {"proposed_name": _QUERIES[i % len(_QUERIES)], "proposed_description": ""}
        start = time.perf_counter()
        response = await client.post(URL, json=body, headers=headers)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert response.status_code == 200
        assert response.json(), body  # a real search, not an empty short-circuit
        if i >= WARMUP:
            samples.append(elapsed_ms)

    p50, p95 = _p(samples, 50), _p(samples, 95)
    print(f"\nSVC-437 similarity benchmark: n={TIMED} p50={p50:.1f} ms p95={p95:.1f} ms")
    assert p95 < P95_BUDGET_MS
