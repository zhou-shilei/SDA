"""Replaceable table-selection decision providers.

The graph does not depend on LangChain or on a particular model SDK.  For the
first deployment, Codex can emit a small JSON decision file.  Later this class
can be replaced by an OpenAI/Codex or local-model adapter without changing the
LangGraph state machine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


VALID_ROLES = {
    "sequence_reference",
    "sequence_variant",
    "binding_assay_ic50",
    "continuation",
    "other",
    "unknown",
}


class DecisionProvider(Protocol):
    def decide(
        self,
        *,
        table_id: str,
        image: str,
        default_roles: list[str],
        default_score: float,
        default_reasons: list[str],
    ) -> dict[str, Any]: ...


class EmptyDecisionProvider:
    """No model call: preserve the deterministic triage result."""

    def decide(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "roles": kwargs["default_roles"],
            "selection_score": kwargs["default_score"],
            "reasons": kwargs["default_reasons"],
        }


class JsonDecisionProvider:
    """Read Codex-produced decisions keyed by image name or table id."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        self.decisions = payload if isinstance(payload, dict) else {}

    def decide(self, **kwargs: Any) -> dict[str, Any]:
        decision = self.decisions.get(kwargs["image"], self.decisions.get(kwargs["table_id"], {}))
        if not isinstance(decision, dict):
            decision = {}
        raw_roles = decision.get("roles", kwargs["default_roles"])
        if not isinstance(raw_roles, list):
            raw_roles = kwargs["default_roles"]
        roles = [role for role in raw_roles if role in VALID_ROLES]
        raw_reasons = decision.get("reasons", kwargs["default_reasons"])
        if not isinstance(raw_reasons, list):
            raw_reasons = kwargs["default_reasons"]
        reasons = [str(reason) for reason in raw_reasons]
        if decision:
            reasons.append("codex_decision")
        try:
            score = float(decision.get("selection_score", kwargs["default_score"]))
        except (TypeError, ValueError):
            score = kwargs["default_score"]
        return {
            "roles": roles or kwargs["default_roles"],
            "selection_score": score,
            "reasons": reasons,
        }


def get_decision_provider(path: str | None) -> DecisionProvider:
    return JsonDecisionProvider(path) if path else EmptyDecisionProvider()
