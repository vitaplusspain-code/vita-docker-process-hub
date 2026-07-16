from __future__ import annotations

from datetime import time
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator, model_validator


class Zone(BaseModel):
    type: str
    polygon: list[list[float]]

    @field_validator("polygon")
    @classmethod
    def polygon_has_at_least_3_points(cls, value: list[list[float]]) -> list[list[float]]:
        if len(value) < 3:
            raise ValueError("polygon must have at least 3 points")
        for point in value:
            if len(point) != 2:
                raise ValueError("each polygon point must be [x, y]")
        return value


class Camera(BaseModel):
    zones: dict[str, Zone]


class ScheduleWindow(BaseModel):
    start: time
    end: time


class Schedules(BaseModel):
    expected_activity: ScheduleWindow
    sleep_window: ScheduleWindow


class Thresholds(BaseModel):
    inactivity_minutes: int

    @field_validator("inactivity_minutes")
    @classmethod
    def must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("inactivity_minutes must be positive")
        return value


class RulesConfig(BaseModel):
    hub_id: str
    user_id: str
    cameras: dict[str, Camera]
    schedules: Schedules
    thresholds: Thresholds

    @model_validator(mode="after")
    def schedules_must_not_be_degenerate(self) -> "RulesConfig":
        for name, window in (
            ("expected_activity", self.schedules.expected_activity),
            ("sleep_window", self.schedules.sleep_window),
        ):
            if window.start == window.end:
                raise ValueError(f"schedules.{name}: start and end must differ")
        return self


def load_rules_config(path: str | Path) -> RulesConfig:
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return RulesConfig.model_validate(raw)
