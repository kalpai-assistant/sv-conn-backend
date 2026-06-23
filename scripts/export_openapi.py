"""
Export the FastAPI OpenAPI schema to svc_connect.json (the frontend contract).

Run after changing any request/response schema or route so the frontend has the
updated contract:

    uv run python scripts/export_openapi.py
"""
import json
from pathlib import Path

from app.main import create_app

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "svc_connect.json"


def main() -> None:
    app = create_app()
    spec = app.openapi()
    OUTPUT_PATH.write_text(
        json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Wrote {OUTPUT_PATH.name}: {len(spec['paths'])} paths")


if __name__ == "__main__":
    main()
