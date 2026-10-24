"""链路配置及其子结构的 Pydantic 模型。"""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Layer = Literal["price", "inv", "fin", "demand", "supply", "margin"]
Lead = Literal["lead", "sync", "lag"]
RuleDirection = Literal["above", "below"]
ChokeType = Literal[
    "technology", "resource", "capacity", "channel", "regulation", "brand", "ecosystem", "crowded"
]
ChokeLevel = Literal["high", "medium", "low"]
ChokeState = Literal["tight", "balanced", "loose"]

# cond 中的英文标识符（如 wholesale_price）视为 trigger 引用，大小写不敏感
TRIGGER_REF_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OPERATOR_WORDS = frozenset({"AND", "OR", "NOT"})


def _touch(a: float, b: float) -> bool:
    """判断两个端点是否重合。"""
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


class Trigger(BaseModel):
    id: str = Field(min_length=1)
    name: str
    layer: Layer
    lead: Lead
    freq: str
    src: str
    unit: str
    normal_range: tuple[float, float]
    warn_range: tuple[float, float]
    crit_range: tuple[float, float]
    rule: RuleDirection
    meaning: str
    definition: str = ""
    source_note: str = ""
    lag_days: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_ranges(self) -> "Trigger":
        ranges = {
            "normal_range": self.normal_range,
            "warn_range": self.warn_range,
            "crit_range": self.crit_range,
        }
        for name, (low, high) in ranges.items():
            if not low < high:
                raise ValueError(f"{name} 下界必须小于上界，当前为 [{low}, {high}]")

        normal, warn, crit = self.normal_range, self.warn_range, self.crit_range
        if self.rule == "above":
            ok = (
                _touch(normal[1], warn[0])
                and _touch(warn[1], crit[0])
                and warn[0] > normal[0]
                and crit[0] > warn[0]
            )
            detail = (
                "normal_range/warn_range/crit_range 在 rule=above 时必须连续且递增"
                "（normal_range.hi == warn_range.lo，warn_range.hi == crit_range.lo）"
            )
        else:
            ok = (
                _touch(normal[0], warn[1])
                and _touch(warn[0], crit[1])
                and warn[1] < normal[1]
                and crit[1] < warn[1]
            )
            detail = (
                "normal_range/warn_range/crit_range 在 rule=below 时必须连续且递减"
                "（normal_range.lo == warn_range.hi，warn_range.lo == crit_range.hi）"
            )
        if not ok:
            raise ValueError(f"{detail}，当前 normal={normal}, warn={warn}, crit={crit}")
        return self


class Rule(BaseModel):
    cond: str = Field(min_length=1)
    res: str = Field(min_length=1)
    rationale: str


class Agent(BaseModel):
    name: str
    role: str
    triggers: list[str] = Field(default_factory=list)


class Stage(BaseModel):
    yr: int
    t: str
    d: str
    current: bool = False


class ChokeScore(BaseModel):
    switchability: int = Field(ge=0, le=3)
    concentration: int = Field(ge=0, le=3)
    replicability: int = Field(ge=0, le=3)
    total: int

    @model_validator(mode="after")
    def _check_total(self) -> "ChokeScore":
        expected = self.switchability + self.concentration + self.replicability
        if self.total != expected:
            raise ValueError(
                f"total({self.total}) 必须等于 "
                f"switchability+concentration+replicability({expected})"
            )
        return self


class ChokePoint(BaseModel):
    node: str
    type: ChokeType
    score: ChokeScore
    level: ChokeLevel
    current_state: ChokeState
    profit_share_pct: float = Field(ge=0, le=100)
    value_accumulation_trigger: str
    value_transfer_trigger: str
    tracking_triggers: list[str] = Field(default_factory=list)
    paradox_flag: bool
    paradox_note: str | None = None


class SkeletonNode(BaseModel):
    name: str
    key: bool = False
    note: str | None = None


class ProfitPoolItem(BaseModel):
    link: str
    share_pct: float = Field(ge=0, le=100)
    basis: str


class Transmission(BaseModel):
    chain: list[str] = Field(min_length=1)
    note: str


class ChainConfig(BaseModel):
    chain_name: str = Field(min_length=1)
    wealth_logic: str
    skeleton: list[SkeletonNode]
    profit_pool: list[ProfitPoolItem]
    transmission: Transmission
    triggers: list[Trigger]
    rules: list[Rule]
    agents: list[Agent]
    stages: list[Stage]
    choke_points: list[ChokePoint]
    data_gaps: list[str] = Field(default_factory=list)

    @field_validator("triggers")
    @classmethod
    def _check_unique_trigger_ids(cls, triggers: list[Trigger]) -> list[Trigger]:
        ids = [trigger.id for trigger in triggers]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"triggers 存在重复 id: {duplicates}")
        return triggers

    @model_validator(mode="after")
    def _check_references(self) -> "ChainConfig":
        valid_ids = {trigger.id for trigger in self.triggers}
        valid_ids_upper = {trigger_id.upper() for trigger_id in valid_ids}

        for index, rule in enumerate(self.rules):
            referenced = {
                token.upper()
                for token in TRIGGER_REF_RE.findall(rule.cond)
                if token.upper() not in _OPERATOR_WORDS
            }
            missing = sorted(referenced - valid_ids_upper)
            if missing:
                raise ValueError(f"rules[{index}].cond 引用了不存在的 trigger id: {missing}")

        for index, agent in enumerate(self.agents):
            missing = sorted(set(agent.triggers) - valid_ids)
            if missing:
                raise ValueError(f"agents[{index}].triggers 引用了不存在的 trigger id: {missing}")

        for index, choke_point in enumerate(self.choke_points):
            missing = sorted(set(choke_point.tracking_triggers) - valid_ids)
            if missing:
                raise ValueError(
                    f"choke_points[{index}].tracking_triggers 引用了不存在的 trigger id: {missing}"
                )
        return self
