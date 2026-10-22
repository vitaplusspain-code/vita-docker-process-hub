"""Criterio de riesgo de una caída para decidir si se conserva su clip."""
from __future__ import annotations

from typing import Literal

from vitahub.clips.constants import RISK_MIN_DROP

Verdict = Literal["risk", "slow", "got_up"]


def assess(drop_speed: float | None, resolved_reason: str | None) -> Verdict:
    """Levantarse solo gana a todo; sin velocidad medida se asume riesgo (más seguro conservar)."""
    if resolved_reason == "upright":
        return "got_up"
    if drop_speed is not None and drop_speed < RISK_MIN_DROP:
        return "slow"
    return "risk"
