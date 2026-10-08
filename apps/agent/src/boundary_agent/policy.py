from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from boundary_agent.models import Policy
from boundary_agent.types import PolicyDecision, ToolExecutionIntent

VERDICT_WEIGHT = {
    "allow": 0,
    "require_approval": 1,
    "block": 2,
}


@dataclass(slots=True)
class EvaluatedRule:
    policy_id: str
    verdict: str
    reason: str
    specificity: int
    priority: int
    # What the user may be told: the reason without guard internals (the flagged run's detail names
    # a policy). Defaults to `reason`.
    user_reason: str | None = None


def policy_mode(policy: Policy) -> str:
    """off / shadow / enforce. `enabled` predates modes and still switches a policy off."""
    if not policy.enabled:
        return "off"
    return getattr(policy, "mode", None) or "enforce"


class PolicyEngine:
    def evaluate(self, intent: ToolExecutionIntent, policies: Iterable[Policy]) -> PolicyDecision:
        evaluated: list[EvaluatedRule] = []

        if intent.token_budget is not None and intent.spent_tokens >= intent.token_budget:
            evaluated.append(
                EvaluatedRule(
                    policy_id="conversation-token-budget",
                    verdict="block",
                    reason="Conversation token budget exceeded.",
                    specificity=3,
                    priority=10_000,
                )
            )
        if intent.cost_budget is not None and intent.spent_cost >= intent.cost_budget:
            evaluated.append(
                EvaluatedRule(
                    policy_id="conversation-cost-budget",
                    verdict="block",
                    reason="Conversation cost budget exceeded.",
                    specificity=3,
                    priority=10_000,
                )
            )

        # Shadow policies are evaluated the same way, but their verdicts are only reported (logged as
        # what they would have done), never acted on. Off, or disabled, means not evaluated.
        enforced: list[Policy] = []
        shadowed: list[Policy] = []
        for policy in policies:
            mode = policy_mode(policy)
            if mode == "off" or not self._matches_scope(policy, intent):
                continue
            (shadowed if mode == "shadow" else enforced).append(policy)

        shadow = [
            {"policy_id": rule.policy_id, "verdict": rule.verdict, "reason": rule.reason}
            for rule in self._evaluate_policies(intent, shadowed)
        ]
        evaluated.extend(self._evaluate_policies(intent, enforced))

        if not evaluated:
            return PolicyDecision(
                verdict="allow",
                reason="No matching policy blocked the call.",
                matched_rule_ids=[],
                shadow=shadow,
            )

        winner = max(
            evaluated, key=lambda item: (VERDICT_WEIGHT[item.verdict], item.specificity, item.priority)
        )
        return PolicyDecision(
            verdict=winner.verdict,
            reason=winner.reason,
            user_reason=winner.user_reason or winner.reason,
            matched_rule_ids=[winner.policy_id],
            requires_approval=winner.verdict == "require_approval",
            shadow=shadow,
        )

    def _evaluate_policies(
        self, intent: ToolExecutionIntent, policies: Iterable[Policy]
    ) -> list[EvaluatedRule]:
        """The verdict each policy reaches on this call (policies in scope only)."""
        evaluated: list[EvaluatedRule] = []
        for policy in policies:
            if policy.rule_type == "block_tool":
                evaluated.append(
                    EvaluatedRule(
                        policy_id=policy.id,
                        verdict="block",
                        reason=(policy.action_json or {}).get("reason", "Blocked by policy."),
                        specificity=self._specificity(policy),
                        priority=policy.priority,
                    )
                )
                continue

            if policy.rule_type == "require_approval":
                evaluated.append(
                    EvaluatedRule(
                        policy_id=policy.id,
                        verdict="require_approval",
                        reason=(policy.action_json or {}).get("reason", "Tool requires human approval."),
                        specificity=self._specificity(policy),
                        priority=policy.priority,
                    )
                )
                continue

            if policy.rule_type == "validate_args":
                valid, reason = self._validate_args(policy.conditions_json or {}, intent.arguments)
                if not valid:
                    evaluated.append(
                        EvaluatedRule(
                            policy_id=policy.id,
                            verdict="block",
                            reason=reason,
                            specificity=self._specificity(policy),
                            priority=policy.priority,
                        )
                    )
                continue

            if policy.rule_type == "guard_signal":
                # Reacts to guard verdicts recorded on the run. Today: `run_tainted`, set when the
                # run has read content the guard flagged as injection (enforced or shadow).
                conditions = policy.conditions_json or {}
                if conditions.get("run_tainted", True) and intent.run_tainted:
                    action = policy.action_json or {}
                    verdict = action.get("verdict", "require_approval")
                    if verdict not in VERDICT_WEIGHT:
                        verdict = "require_approval"
                    base = action.get("reason", "Run read content the guard flagged as possible injection.")
                    reason = f"{base} ({intent.taint_reason})" if intent.taint_reason else base
                    evaluated.append(
                        EvaluatedRule(
                            policy_id=policy.id,
                            verdict=verdict,
                            reason=reason,
                            user_reason=base,
                            specificity=self._specificity(policy),
                            priority=policy.priority,
                        )
                    )
                continue

            if policy.rule_type == "token_budget":
                budget = (policy.conditions_json or {}).get("max_tokens")
                if budget is not None and intent.spent_tokens >= int(budget):
                    evaluated.append(
                        EvaluatedRule(
                            policy_id=policy.id,
                            verdict="block",
                            reason=f"Policy token budget exceeded ({budget}).",
                            specificity=self._specificity(policy),
                            priority=policy.priority,
                        )
                    )
                continue

            if policy.rule_type == "cost_budget":
                budget = (policy.conditions_json or {}).get("max_cost")
                if budget is not None and intent.spent_cost >= float(budget):
                    evaluated.append(
                        EvaluatedRule(
                            policy_id=policy.id,
                            verdict="block",
                            reason=f"Policy cost budget exceeded ({budget}).",
                            specificity=self._specificity(policy),
                            priority=policy.priority,
                        )
                    )

        return evaluated

    def _matches_scope(self, policy: Policy, intent: ToolExecutionIntent) -> bool:
        if policy.target_tool and self._normalize_tool_name(policy.target_tool) != self._normalize_tool_name(
            intent.tool_name
        ):
            return False
        return not (policy.target_server_id and policy.target_server_id != intent.server_id)

    def _specificity(self, policy: Policy) -> int:
        score = 0
        if policy.target_server_id:
            score += 1
        if policy.target_tool:
            score += 1
        return score

    def _validate_args(self, conditions: dict[str, Any], arguments: dict[str, Any]) -> tuple[bool, str]:
        path_arg = conditions.get("path_arg")
        if path_arg:
            path_value = str(arguments.get(path_arg, ""))
            normalized_path, error = self._normalize_relative_path(path_value)
            if error is not None:
                return False, f"Argument '{path_arg}' {error}"

            normalized_prefixes: list[str] = []
            for prefix in conditions.get("allow_prefixes", []):
                normalized_prefix, prefix_error = self._normalize_relative_path(str(prefix), allow_empty=True)
                if prefix_error is not None:
                    continue
                normalized_prefixes.append(normalized_prefix)
                if normalized_prefix == ".":
                    return True, "Path prefix allowed."
                if normalized_path == normalized_prefix or normalized_path.startswith(
                    f"{normalized_prefix}/"
                ):
                    return True, "Path prefix allowed."
            return False, (
                f"Argument '{path_arg}' must stay under one of "
                f"{normalized_prefixes or conditions.get('allow_prefixes', [])}."
            )

        blocked_args = conditions.get("blocked_values", {})
        for arg_name, blocked_values in blocked_args.items():
            if arguments.get(arg_name) in blocked_values:
                return False, f"Argument '{arg_name}' contains a blocked value."

        return True, "Arguments validated."

    def _normalize_tool_name(self, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
        return normalized.strip("_")

    def _normalize_relative_path(self, value: str, *, allow_empty: bool = False) -> tuple[str, str | None]:
        candidate = value.replace("\\", "/").strip()
        if not candidate:
            if allow_empty:
                return ".", None
            return "", "must not be empty."
        if candidate.startswith("/") or re.match(r"^[a-zA-Z]:", candidate):
            return "", "must be a relative POSIX-style path."

        parts: list[str] = []
        for part in PurePosixPath(candidate).parts:
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    return "", "must not escape the allowed directory."
                parts.pop()
                continue
            parts.append(part)

        normalized = "/".join(parts) or "."
        return normalized, None
