from __future__ import annotations

import json
from pathlib import Path

from .models import SignalIntent


def load_intent(path: str | Path) -> SignalIntent:
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    return SignalIntent.from_dict(data)
