"""Pydantic models for agent outputs.

String fields get truncated on input so text copied from a document can't push long
content into later agents.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ROOT_CAUSES = ("timezone_boundary", "unit_mismatch", "refunds_not_subtracted", "join_fanout", "stale_data",
               "chart_table_mismatch", "wrong_period", "extraction_error", "other")
RootCause = Literal["timezone_boundary", "unit_mismatch", "refunds_not_subtracted", "join_fanout", "stale_data",
                    "chart_table_mismatch", "wrong_period", "extraction_error", "other"]


def _clip(limit: int):
    def validator(cls, v):
        if isinstance(v, str):
            return v[:limit]
        return v
    return validator


class ReportedFigure(BaseModel):
    figure_id: str
    artifact_id: str
    location: str = Field(description="Where it appears, e.g. 'page 1 key metrics table' or 'KPI tile'")
    label: str = Field(description="Label exactly as shown, including any unit hint like ($K) or footnote marker")
    displayed_text: str = Field(description="The number exactly as displayed, e.g. '$591,620' or '1,105' or '$246.5K'")
    value: float = Field(description="Numeric value as displayed, before unit scaling: '$246.5K' -> 246.5")
    unit_label: str = Field(description="Displayed unit: '$', '$K', '$M', '%', or '' for plain counts. "
                                        "Take it from the label if the label carries it, e.g. 'Refunds ($K)' -> '$K'")
    display_decimals: int = Field(ge=0, le=4, description="Decimals shown: '$300.35' -> 2, '5.1%' -> 1, '1,105' -> 0")
    period_label: str | None = Field(default=None, description="Period the artifact states for this number")
    notes: str | None = Field(default=None, description="Footnotes or caveats attached to this number")

    _c1 = field_validator("location", "label", mode="before")(_clip(80))
    _c2 = field_validator("displayed_text", "unit_label", mode="before")(_clip(24))
    _c3 = field_validator("period_label", mode="before")(_clip(40))
    _c4 = field_validator("notes", mode="before")(_clip(160))


class SecurityNote(BaseModel):
    artifact_id: str
    description: str
    _c = field_validator("description", mode="before")(_clip(300))


class ExtractionOutput(BaseModel):
    figures: list[ReportedFigure]
    security_notes: list[SecurityNote] = Field(default_factory=list)
    unreadable: list[str] = Field(default_factory=list, description="Numbers you saw but could not read reliably")


class PlannedCheck(BaseModel):
    check_id: str
    figure_id: str
    metric_id: str
    dimension_value: str | None = Field(default=None, description="Required for CATEGORY_REVENUE, e.g. 'Electronics'")
    period: str = Field(description="YYYY-MM")
    reason: str = ""
    _c = field_validator("reason", mode="before")(_clip(200))


class SkippedFigure(BaseModel):
    figure_id: str
    reason: str
    _c = field_validator("reason", mode="before")(_clip(200))


class Plan(BaseModel):
    checks: list[PlannedCheck]
    skipped: list[SkippedFigure] = Field(default_factory=list)


class Finding(BaseModel):
    finding_id: str
    check_id: str
    root_cause: RootCause
    explanation: str
    evidence_sql: list[str] = Field(default_factory=list, description="Up to 3 SQL queries you ran that support the cause")
    evidence_summary: str = ""
    confidence: Literal["high", "medium", "low"]
    _c1 = field_validator("explanation", mode="before")(_clip(500))
    _c2 = field_validator("evidence_summary", mode="before")(_clip(300))

    @field_validator("evidence_sql", mode="before")
    @classmethod
    def _sql(cls, v):
        return [str(q)[:800] for q in (v or [])][:3]


class InvestigationOutput(BaseModel):
    findings: list[Finding]


class Verdict(BaseModel):
    finding_id: str
    verdict: Literal["confirmed", "rejected", "uncertain"]
    reason: str
    _c = field_validator("reason", mode="before")(_clip(300))


class CriticOutput(BaseModel):
    verdicts: list[Verdict]


class SingleAgentIssue(BaseModel):
    artifact_id: str
    label: str
    displayed_text: str
    metric_id: str
    dimension_value: str | None = None
    root_cause: RootCause
    explanation: str
    _c = field_validator("explanation", mode="before")(_clip(500))


class SingleAgentOutput(BaseModel):
    issues: list[SingleAgentIssue]
    checks_passed: int = 0
    security_notes: list[SecurityNote] = Field(default_factory=list)
