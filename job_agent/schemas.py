"""v2 三个 Skill 的固定 JSON 结构；原文引用由宿主核对。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 2
Status = Literal["matched", "learning", "explicit_gap", "not_evidenced", "needs_confirmation", "not_required"]
Level = Literal["required", "preferred"]


class Structured(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Statement(Structured):
    text: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class TechnicalRequirement(Structured):
    skill: str = Field(min_length=1)
    level: Level
    evidence: str = Field(min_length=1)


class Qualification(Statement):
    level: Level


class JDOutput(Structured):
    company: Statement | None
    title: Statement | None
    responsibilities: list[Statement]
    technical_requirements: list[TechnicalRequirement]
    education_requirements: list[Qualification]
    experience_requirements: list[Qualification]
    other_requirements: list[Statement]
    keywords: list[str]
    limitations: list[str]


class TechnicalMatch(Structured):
    skill: str
    status: Status
    evidence_ids: list[str]
    reason: str


class QualificationMatch(Structured):
    requirement: str
    status: Status
    evidence_ids: list[str]
    reason: str


class MatchOutput(Structured):
    technical_matches: list[TechnicalMatch]
    education: list[QualificationMatch]
    experience: list[QualificationMatch]
    limitations: list[str]


class Preparation(Structured):
    skill: str
    preparation: str = Field(min_length=1)
    acceptance: str = Field(min_length=1)


class GapOutput(Structured):
    preparations: list[Preparation]
    note: str
