from __future__ import annotations

import json
from pathlib import Path

from .models import OrderStatusInput


def main() -> None:
    target = Path(__file__).resolve().parents[2] / "config" / "schemas" / "order.get_status.json"
    target.write_text(json.dumps(OrderStatusInput.model_json_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
