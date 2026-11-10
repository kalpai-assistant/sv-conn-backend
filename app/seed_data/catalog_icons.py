"""SVC-430: the icon keys an admin can pick for a category or service.

Each is a kebab-case Lucide icon name, checked against the lucide-react the
frontend ships (1.27.0). The first 16 are the icons the consumer app already
draws for its categories (src/lib/category-icons.ts); the rest are common
home-service icons.

There is no DB CHECK on ``icon_key``, so adding a key here needs no migration.
Removing one would leave rows pointing at it, so don't. tests/test_admin_catalog.py
pins this tuple.
"""

CATALOG_ICON_KEYS: tuple[str, ...] = (
    # In use by the consumer app today.
    "sparkles",
    "bug",
    "air-vent",
    "layout-grid",
    "zap",
    "wrench",
    "hammer",
    "paintbrush",
    "scissors",
    "brush",
    "hand-heart",
    "droplets",
    "sofa",
    "car",
    "bath",
    "camera",
    # Home services.
    "house",
    "shower-head",
    "plug",
    "lightbulb",
    "fan",
    "refrigerator",
    "washing-machine",
    "microwave",
    "paint-roller",
    "drill",
    "cctv",
    "sprout",
    "truck",
    "shirt",
)
