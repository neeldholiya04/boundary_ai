from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boundary_agent.audit import AuditLogger
from boundary_agent.config import Settings
from boundary_agent.guarding import (
    GuardAdapter,
    GuardOutcome,
    redacted_tool_result,
    redaction_notice,
    secret_placeholder_message,
    secret_withheld_message,
    user_block_message,
    withheld_result,
)
from boundary_agent.llm import BasePlanner
from boundary_agent.mcp_manager import MCPManager
from boundary_agent.models import ApprovalRequest, Conversation, MCPServer, Message, Policy, Run
from boundary_agent.policy import PolicyEngine
from boundary_agent.schemas import ChatResponse
from boundary_agent.telemetry import DISABLED, RunHandle, Telemetry
from boundary_agent.types import ExecutedToolStep, PlannerMessage, ToolCall, ToolExecutionIntent
from boundary_guard import Action, Stage


class AgentRuntime:
    def __init__(
        self,
        settings: Settings,
        planner: BasePlanner,
        mcp_manager: MCPManager,
        policy_engine: PolicyEngine,
        audit_logger: AuditLogger,
        guard: GuardAdapter | None = None,
        telemetry: Telemetry = DISABLED,
    ) -> None:
        self.settings = settings
        self.planner = planner
        self.mcp_manager = mcp_manager
        self.policy_engine = policy_engine
        self.audit_logger = audit_logger
        self.guard = guard
        self.telemetry = telemetry

    async def handle_chat(
        self,
        session: AsyncSession,
        user_message: str,
        conversation_id: str | None,
        response_schema: str | None = None,
    ) -> ChatResponse:
        conversation, created = await self._get_or_create_conversation(session, conversation_id)
        pending_approval = None
        if conversation_id:
            await self.expire_pending_approvals(session, conversation_id=conversation.id)
            pending_approval = await session.scalar(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.conversation_id == conversation.id,
                    ApprovalRequest.status == "pending",
                )
                .order_by(ApprovalRequest.created_at.desc())
                .limit(1)
            )
        if pending_approval is not None:
            # The run's user-facing line, not the approval's reason (which is for the reviewer and can
            # name guard policies and scores).
            paused = await session.get(Run, pending_approval.run_id)
            waiting_for = (paused.paused_reason if paused else None) or "a person's approval"
            return ChatResponse(
                conversation_id=conversation.id,
                run_id=pending_approval.run_id,
                status="waiting_approval",
                assistant_message=f"This conversation is still waiting: {waiting_for}",
                approval_request_id=pending_approval.id,
            )

        run = Run(conversation_id=conversation.id, status="running", response_schema=response_schema)
        session.add(run)
        await session.flush()

        with self.telemetry.run(run.id, name="chat", conversation_id=conversation.id) as trace:
            response = await self._start_run(session, conversation, run, user_message, created, trace)
            trace.finish(response.status, response.assistant_message)
            return response

    async def _start_run(
        self,
        session: AsyncSession,
        conversation: Conversation,
        run: Run,
        user_message: str,
        created: bool,
        trace: RunHandle,
    ) -> ChatResponse:
        # Guard the request before it is stored anywhere: the title, the message history, the
        # audit log, the planner and the trace all get the guard's (possibly redacted) text.
        outcome = await self._guard(session, Stage.USER_INPUT, user_message, conversation, run)
        user_text = outcome.text if outcome else user_message
        trace.set_input(user_text)
        if created:
            conversation.title = (
                user_text.strip().splitlines()[0][:60] if user_text.strip() else "New conversation"
            )
        session.add(Message(conversation_id=conversation.id, role="user", content=user_text))
        await self.audit_logger.record(
            session,
            "chat.user_message",
            {"conversation_id": conversation.id, "message": user_text},
            conversation_id=conversation.id,
            run_id=run.id,
        )
        # A secret was removed from the request. The model only has a placeholder, so anything it did
        # with "the key" would be wrong: writing the placeholder over the real value, then saying it
        # wrote the key. Stop here and say so plainly instead of running the planner.
        redacted_by_secrets = (
            self.guard is not None
            and outcome is not None
            and bool(set(outcome.policies(Action.REDACT)) & self.guard.secret_policies)
        )
        if redacted_by_secrets:
            # Only placeholders the guard just put in. Counted, not looked up: a user quoting
            # `<OPENAI_KEY_1>` from an earlier answer next to a new key must not hide the new one.
            new = [
                p
                for p in self.guard.secret_placeholders(user_text)
                if user_text.count(p) > user_message.count(p)
            ]
            if withheld := new:
                # Checked before a block or a review: those stop the run too, but the answer should
                # say what happened to the key. The other policies are named after it.
                stopped_by = outcome.policies(Action.BLOCK) + outcome.policies(Action.ESCALATE)
                await self.audit_logger.record(
                    session,
                    "guard.secret_withheld",
                    {"stage": "user_input", "placeholders": withheld},
                    conversation_id=conversation.id,
                    run_id=run.id,
                )
                response = self._blocked_response(
                    conversation_id=conversation.id,
                    run_id=run.id,
                    run=run,
                    executed_steps=[],
                    session=session,
                    assistant_message=secret_withheld_message(withheld, also_stopped=bool(stopped_by)),
                )
                await session.commit()
                return response

        if outcome is not None and outcome.action is Action.BLOCK:
            response = self._blocked_response(
                conversation_id=conversation.id,
                run_id=run.id,
                run=run,
                executed_steps=[],
                session=session,
                assistant_message=user_block_message(self.guard, outcome, Stage.USER_INPUT, run_id=run.id),
            )
            await session.commit()
            return response
        if outcome is not None and outcome.action is Action.ESCALATE:
            response = await self._request_content_review(
                session, conversation, run, Stage.USER_INPUT, outcome, user_text, executed_steps=[]
            )
            await session.commit()
            return response
        tools = await self.mcp_manager.list_tools(session, refresh=False)
        await self.audit_logger.record(
            session,
            "mcp.tools_discovered",
            {"count": len(tools), "tools": [asdict(tool) for tool in tools]},
            conversation_id=conversation.id,
            run_id=run.id,
        )

        response = await self._run_planner_loop(session, conversation, run, user_text, tools, [])
        if notice := redaction_notice(outcome, user_message):
            response.guard_notices.append(notice)
        await session.commit()
        return response

    async def decide_approval(
        self,
        session: AsyncSession,
        approval_id: str,
        decision: str,
        comment: str | None,
    ) -> ChatResponse:
        approval = await session.get(ApprovalRequest, approval_id)
        if approval is None:
            raise ValueError("Approval request not found.")
        if approval.status != "pending":
            raise ValueError("Approval request is no longer pending.")

        run = await session.get(Run, approval.run_id)
        conversation = await session.get(Conversation, approval.conversation_id)
        if run is None or conversation is None:
            raise ValueError("Approval request is missing its run context.")

        # Same trace id as the run that paused, so the whole story is one trace.
        with self.telemetry.run(run.id, name="approval", conversation_id=conversation.id) as trace:
            trace.set_input({"decision": decision, "kind": approval.kind, "tool_name": approval.tool_name})
            response = await self._decide(session, approval, run, conversation, decision, comment)
            trace.finish(response.status, response.assistant_message)
            return response

    async def _decide(
        self,
        session: AsyncSession,
        approval: ApprovalRequest,
        run: Run,
        conversation: Conversation,
        decision: str,
        comment: str | None,
    ) -> ChatResponse:
        if self._as_utc(approval.expires_at) < datetime.now(UTC):
            await self._expire_approval(session, approval, run, conversation)
            await session.commit()
            return ChatResponse(
                conversation_id=conversation.id,
                run_id=run.id,
                status="denied",
                assistant_message=run.latest_response,
                approval_request_id=approval.id,
            )

        approval.status = decision
        approval.decision_comment = comment
        approval.decided_at = datetime.utcnow()
        await self.audit_logger.record(
            session,
            f"approval.{decision}",
            {"approval_request_id": approval.id, "comment": comment},
            conversation_id=conversation.id,
            run_id=run.id,
        )

        if approval.kind == "content_review":
            response = await self._resume_content_review(session, approval, run, conversation, decision)
            await session.commit()
            return response

        if decision == "denied":
            run.status = "denied"
            run.latest_response = "Tool call denied by human approval."
            session.add(
                Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content=run.latest_response,
                    metadata_json={"notice": "blocked"},
                )
            )
            await session.commit()
            return ChatResponse(
                conversation_id=conversation.id,
                run_id=run.id,
                status="denied",
                assistant_message=run.latest_response,
                approval_request_id=approval.id,
            )

        run.status = "running"
        stored_context = self._extract_approval_context(approval.arguments_json)
        executed_steps = [self._executed_step_from_dict(item) for item in stored_context["executed_steps"]]
        tool_call = ToolCall(
            server_id=approval.server_id,
            tool_name=approval.tool_name,
            arguments=stored_context["tool_arguments"],
        )
        tools = await self.mcp_manager.list_tools(session, refresh=False)
        step_result = await self._evaluate_tool_call(
            session=session,
            conversation=conversation,
            run=run,
            user_message=stored_context["user_message"],
            tool_call=tool_call,
            executed_steps=executed_steps,
            approval_context=approval,
        )
        if isinstance(step_result, ChatResponse):
            await session.commit()
            return step_result

        executed_steps.append(step_result)
        response = await self._run_planner_loop(
            session,
            conversation,
            run,
            stored_context["user_message"],
            tools,
            executed_steps,
        )
        await session.commit()
        return response

    async def _run_planner_loop(
        self,
        session: AsyncSession,
        conversation: Conversation,
        run: Run,
        user_message: str,
        tools: list,
        executed_steps: list[ExecutedToolStep],
    ) -> ChatResponse:
        for _ in range(self.settings.max_tool_steps):
            budget_response = await self._enforce_conversation_budgets(
                session, conversation, run, executed_steps
            )
            if budget_response is not None:
                return budget_response

            conversation_history = await self._get_conversation_history(session, conversation.id)
            try:
                plan = await self.planner.plan(user_message, tools, executed_steps, conversation_history)
            except Exception as exc:
                run.status = "failed"
                run.latest_response = str(exc)
                await self.audit_logger.record(
                    session,
                    "agent.planner_error",
                    {"error": str(exc)},
                    conversation_id=conversation.id,
                    run_id=run.id,
                )
                return ChatResponse(
                    conversation_id=conversation.id,
                    run_id=run.id,
                    status="failed",
                    assistant_message=f"Planner error: {exc}",
                    executed_tool_calls=self._serialize_steps(executed_steps),
                )

            conversation.spent_tokens += plan.usage_tokens
            conversation.spent_cost += plan.usage_cost

            if plan.tool_call is None:
                assistant_message = plan.assistant_message or self._fallback_summary(executed_steps)
                outcome = await self._guard(
                    session,
                    Stage.FINAL_OUTPUT,
                    assistant_message,
                    conversation,
                    run,
                    references=[self._tool_result_text(step.result) for step in executed_steps],
                    response_schema=run.response_schema,
                )
                if outcome is not None and outcome.action is Action.BLOCK:
                    return self._blocked_response(
                        conversation_id=conversation.id,
                        run_id=run.id,
                        run=run,
                        executed_steps=executed_steps,
                        session=session,
                        assistant_message=user_block_message(
                            self.guard, outcome, Stage.FINAL_OUTPUT, run_id=run.id
                        ),
                    )
                if outcome is not None and outcome.action is Action.ESCALATE:
                    return await self._request_content_review(
                        session, conversation, run, Stage.FINAL_OUTPUT, outcome, user_message, executed_steps
                    )
                if outcome is not None:
                    assistant_message = outcome.text
                return await self._complete_run(session, conversation, run, assistant_message, executed_steps)

            budget_response = await self._enforce_conversation_budgets(
                session,
                conversation,
                run,
                executed_steps,
                tool_call=plan.tool_call,
            )
            if budget_response is not None:
                return budget_response

            tool_response = await self._evaluate_tool_call(
                session,
                conversation,
                run,
                user_message,
                plan.tool_call,
                executed_steps,
            )
            if isinstance(tool_response, ChatResponse):
                return tool_response
            executed_steps.append(tool_response)

        assistant_message = "Stopped after reaching the maximum number of tool steps for this run."
        session.add(
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content=assistant_message,
                metadata_json={"notice": "stopped"},
            )
        )
        run.status = "failed"
        run.latest_response = assistant_message
        await self.audit_logger.record(
            session,
            "agent.max_steps_reached",
            {"max_tool_steps": self.settings.max_tool_steps},
            conversation_id=conversation.id,
            run_id=run.id,
        )
        return ChatResponse(
            conversation_id=conversation.id,
            run_id=run.id,
            status="failed",
            assistant_message=assistant_message,
            executed_tool_calls=self._serialize_steps(executed_steps),
        )

    async def _evaluate_tool_call(
        self,
        session: AsyncSession,
        conversation: Conversation,
        run: Run,
        user_message: str,
        tool_call: ToolCall,
        executed_steps: list[ExecutedToolStep],
        approval_context: ApprovalRequest | None = None,
    ) -> ChatResponse | ExecutedToolStep:
        server = await session.get(MCPServer, tool_call.server_id)
        if server is None:
            run.status = "failed"
            run.latest_response = "Selected MCP server could not be found."
            return ChatResponse(
                conversation_id=conversation.id,
                run_id=run.id,
                status="failed",
                assistant_message=run.latest_response,
                executed_tool_calls=self._serialize_steps(executed_steps),
            )

        # A secret's placeholder used as a value in a tool call (`OPENAI_API_KEY=<OPENAI_KEY_1>`) is
        # the model writing or sending "the key" it never had. Refuse before anything runs. A
        # placeholder merely mentioned (a note about a redacted log) goes through.
        args_text = json.dumps(tool_call.arguments, sort_keys=True, ensure_ascii=False)
        held = (
            self.guard.assigned_secret_placeholders(args_text)
            if self.guard is not None and self.guard.secrets_enforced
            else []
        )
        if held:
            if approval_context is not None:
                approval_context.status = "superseded"
                approval_context.decided_at = datetime.utcnow()
                approval_context.decision_comment = "Tool call carried a secret placeholder."
            await self.audit_logger.record(
                session,
                "guard.secret_placeholder_blocked",
                {"tool_name": tool_call.tool_name, "placeholders": held},
                conversation_id=conversation.id,
                run_id=run.id,
            )
            return self._blocked_response(
                conversation_id=conversation.id,
                run_id=run.id,
                run=run,
                executed_steps=executed_steps,
                tool_call=tool_call,
                session=session,
                assistant_message=secret_placeholder_message(tool_call.tool_name, held),
            )

        outcome = await self._guard(
            session,
            Stage.TOOL_ARGS,
            args_text,
            conversation,
            run,
            tool_name=tool_call.tool_name,
            tool_args=tool_call.arguments,
        )
        # Arguments are never rewritten, so a redaction verdict blocks the call too.
        if outcome is not None and outcome.action in (Action.BLOCK, Action.REDACT):
            if approval_context is not None:
                approval_context.status = "superseded"
                approval_context.decided_at = datetime.utcnow()
                approval_context.decision_comment = "Blocked by the guard on resume."
            return self._blocked_response(
                conversation_id=conversation.id,
                run_id=run.id,
                run=run,
                executed_steps=executed_steps,
                tool_call=tool_call,
                session=session,
                assistant_message=user_block_message(
                    self.guard, outcome, Stage.TOOL_ARGS, run_id=run.id, tool_name=tool_call.tool_name
                ),
            )
        guard_escalation = outcome is not None and outcome.action is Action.ESCALATE

        intent = self._build_intent(conversation, run, server, tool_call)
        policies = await self._load_policies(session)
        decision = self.policy_engine.evaluate(intent, policies)
        await self._record_policy_decision(
            session,
            conversation_id=conversation.id,
            run_id=run.id,
            tool_call=tool_call,
            decision=decision,
            source="approval_resume" if approval_context is not None else "planner",
        )

        if decision.verdict == "block":
            if approval_context is not None:
                approval_context.status = "superseded"
                approval_context.decided_at = datetime.utcnow()
                if approval_context.decision_comment:
                    approval_context.decision_comment = (
                        f"{approval_context.decision_comment}\nPolicy changed after approval."
                    )
                else:
                    approval_context.decision_comment = "Policy changed after approval."
                await self.audit_logger.record(
                    session,
                    "approval.invalidated",
                    {
                        "approval_request_id": approval_context.id,
                        "reason": decision.reason,
                    },
                    conversation_id=conversation.id,
                    run_id=run.id,
                )
                return self._blocked_response(
                    conversation_id=conversation.id,
                    run_id=run.id,
                    run=run,
                    executed_steps=executed_steps,
                    tool_call=tool_call,
                    session=session,
                    assistant_message=(
                        f"Approved tool call was blocked because policy changed: {decision.reason}"
                    ),
                )
            return self._blocked_response(
                conversation_id=conversation.id,
                run_id=run.id,
                run=run,
                executed_steps=executed_steps,
                tool_call=tool_call,
                session=session,
                assistant_message=f"Tool call blocked: {decision.reason}",
            )

        needs_approval = decision.verdict == "require_approval" or guard_escalation
        if needs_approval and approval_context is None:
            reason = (
                decision.reason if decision.verdict == "require_approval" else f"Guard: {outcome.reason()}"
            )
            approval = ApprovalRequest(
                run_id=run.id,
                conversation_id=conversation.id,
                server_id=tool_call.server_id,
                tool_name=tool_call.tool_name,
                arguments_json=self._build_approval_context(tool_call, user_message, executed_steps),
                status="pending",
                reason=reason,
                expires_at=datetime.utcnow() + timedelta(seconds=self.settings.approval_ttl_seconds),
            )
            session.add(approval)
            await session.flush()
            run.status = "waiting_approval"
            # The approval keeps the full reason for whoever decides; the user gets a plain line. A tool
            # policy's reason is written by the operator for users; the guard's names scores.
            assistant_message = (
                f"{tool_call.tool_name} needs a person's approval before it runs: "
                f"{decision.user_reason or decision.reason}"
                if decision.verdict == "require_approval"
                else f"{tool_call.tool_name} needs a person's approval before it runs."
            )
            run.paused_reason = assistant_message[:255]
            session.add(
                Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content=assistant_message,
                    metadata_json={"notice": "waiting"},
                )
            )
            await self.audit_logger.record(
                session,
                "approval.requested",
                {
                    "approval_request_id": approval.id,
                    "kind": "tool_call",
                    "tool_name": tool_call.tool_name,
                    "reason": reason,
                },
                conversation_id=conversation.id,
                run_id=run.id,
            )
            return ChatResponse(
                conversation_id=conversation.id,
                run_id=run.id,
                status="waiting_approval",
                assistant_message=assistant_message,
                tool_call=self._serialize_tool_call(tool_call),
                executed_tool_calls=self._serialize_steps(executed_steps),
                approval_request_id=approval.id,
            )

        return await self._execute_allowed_tool_step(
            session, conversation, run, tool_call, executed_steps, user_message
        )

    async def expire_pending_approvals(
        self, session: AsyncSession, conversation_id: str | None = None
    ) -> int:
        now = datetime.now(UTC)
        query = select(ApprovalRequest).where(ApprovalRequest.status == "pending")
        if conversation_id is not None:
            query = query.where(ApprovalRequest.conversation_id == conversation_id)
        approvals = (await session.scalars(query)).all()

        expired = 0
        for approval in approvals:
            if self._as_utc(approval.expires_at) >= now:
                continue
            run = await session.get(Run, approval.run_id)
            conversation = await session.get(Conversation, approval.conversation_id)
            if run is None or conversation is None:
                approval.status = "expired"
                approval.decided_at = datetime.utcnow()
                expired += 1
                continue
            await self._expire_approval(session, approval, run, conversation)
            expired += 1
        return expired

    async def _execute_allowed_tool_step(
        self,
        session: AsyncSession,
        conversation: Conversation,
        run: Run,
        tool_call: ToolCall,
        executed_steps: list[ExecutedToolStep],
        user_message: str,
    ) -> ChatResponse | ExecutedToolStep:
        # The span holds the call and the tool-output guard check; its output is what the planner
        # was actually given (redacted or withheld), never the raw tool result.
        with self.telemetry.observe(
            f"tool.{tool_call.tool_name}", as_type="tool", input=tool_call.arguments
        ) as span:
            step = await self._call_and_guard_tool(
                session, conversation, run, tool_call, executed_steps, user_message
            )
            if isinstance(step, ExecutedToolStep):
                span.update(output=step.result, metadata={"is_error": step.is_error})
            else:
                span.update(output=step.assistant_message, metadata={"status": step.status}, level="WARNING")
            return step

    async def _call_and_guard_tool(
        self,
        session: AsyncSession,
        conversation: Conversation,
        run: Run,
        tool_call: ToolCall,
        executed_steps: list[ExecutedToolStep],
        user_message: str,
    ) -> ChatResponse | ExecutedToolStep:
        try:
            tool_result = await self.mcp_manager.call_tool(
                session, tool_call.server_id, tool_call.tool_name, tool_call.arguments
            )
        except Exception as exc:
            run.status = "failed"
            run.latest_response = f"Tool execution failed: {exc}"
            session.add(
                Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content=run.latest_response,
                    metadata_json={"notice": "stopped"},
                )
            )
            await self.audit_logger.record(
                session,
                "mcp.tool_failed",
                {"tool_name": tool_call.tool_name, "error": str(exc)},
                conversation_id=conversation.id,
                run_id=run.id,
            )
            return ChatResponse(
                conversation_id=conversation.id,
                run_id=run.id,
                status="failed",
                assistant_message=run.latest_response,
                tool_call=self._serialize_tool_call(tool_call),
                executed_tool_calls=self._serialize_steps(executed_steps),
            )

        is_error = bool(tool_result.get("raw", {}).get("isError")) if isinstance(tool_result, dict) else False
        raw_text = self._tool_result_text(tool_result)
        outcome = await self._guard(
            session,
            Stage.TOOL_OUTPUT,
            raw_text,
            conversation,
            run,
            tool_name=tool_call.tool_name,
            tool_args=tool_call.arguments,
        )
        if outcome is not None:
            if outcome.taint_reason and not run.tainted:
                run.tainted = True
                run.taint_reason = outcome.taint_reason
                await self.audit_logger.record(
                    session,
                    "guard.run_tainted",
                    {"tool_name": tool_call.tool_name, "reason": outcome.taint_reason},
                    conversation_id=conversation.id,
                    run_id=run.id,
                )
            if outcome.action is Action.BLOCK:
                if self.settings.guard_tool_output_on_block == "halt":
                    return self._blocked_response(
                        conversation_id=conversation.id,
                        run_id=run.id,
                        run=run,
                        executed_steps=executed_steps,
                        tool_call=tool_call,
                        session=session,
                        assistant_message=user_block_message(
                            self.guard,
                            outcome,
                            Stage.TOOL_OUTPUT,
                            run_id=run.id,
                            tool_name=tool_call.tool_name,
                        ),
                    )
                # Drop-and-continue: the model learns the content was withheld and carries on.
                tool_result = withheld_result(outcome)
            elif outcome.action is Action.ESCALATE:
                return await self._request_content_review(
                    session,
                    conversation,
                    run,
                    Stage.TOOL_OUTPUT,
                    outcome,
                    user_message,
                    executed_steps,
                    tool_call=tool_call,
                    result_shape="text" if self._is_text_result(tool_result) else "json",
                    is_error=is_error,
                )
            elif outcome.text != raw_text:
                tool_result = redacted_tool_result(tool_result, outcome.text)

        return await self._record_tool_step(session, conversation, run, tool_call, tool_result, is_error)

    async def _record_tool_step(
        self,
        session: AsyncSession,
        conversation: Conversation,
        run: Run,
        tool_call: ToolCall,
        tool_result: Any,
        is_error: bool,
    ) -> ExecutedToolStep:
        # Only the guarded result is persisted and published; a withheld or redacted result
        # never reaches the Message table or the audit log in its raw form.
        session.add(
            Message(
                conversation_id=conversation.id,
                role="tool",
                content=json.dumps(tool_result, indent=2),
                metadata_json={"tool_name": tool_call.tool_name, "server_id": tool_call.server_id},
            )
        )
        await self.audit_logger.record(
            session,
            "mcp.tool_succeeded",
            {"tool_name": tool_call.tool_name, "result": tool_result},
            conversation_id=conversation.id,
            run_id=run.id,
        )
        return ExecutedToolStep(tool_call=tool_call, result=tool_result, is_error=is_error)

    # ---- guard helpers --------------------------------------------------------------------

    async def _guard(
        self,
        session: AsyncSession,
        stage: Stage,
        text: str,
        conversation: Conversation,
        run: Run,
        **kwargs: Any,
    ) -> GuardOutcome | None:
        if self.guard is None:
            return None
        return await self.guard.check(session, stage, text, conversation=conversation, run=run, **kwargs)

    def _is_text_result(self, tool_result: Any) -> bool:
        return isinstance(tool_result, dict) and isinstance(tool_result.get("content"), str)

    def _tool_result_text(self, tool_result: Any) -> str:
        # MCPManager returns either structured content (a dict) or {"content": text, "raw": ...}.
        # Check only the text the planner will see; `raw` repeats it.
        if self._is_text_result(tool_result):
            return tool_result["content"]
        return json.dumps(tool_result, ensure_ascii=False)

    async def _complete_run(
        self,
        session: AsyncSession,
        conversation: Conversation,
        run: Run,
        assistant_message: str,
        executed_steps: list[ExecutedToolStep],
    ) -> ChatResponse:
        session.add(Message(conversation_id=conversation.id, role="assistant", content=assistant_message))
        run.status = "completed"
        run.paused_reason = None
        run.latest_response = assistant_message
        await self.audit_logger.record(
            session,
            "agent.response",
            {"message": assistant_message},
            conversation_id=conversation.id,
            run_id=run.id,
        )
        return ChatResponse(
            conversation_id=conversation.id,
            run_id=run.id,
            status="completed",
            assistant_message=assistant_message,
            executed_tool_calls=self._serialize_steps(executed_steps),
        )

    async def _request_content_review(
        self,
        session: AsyncSession,
        conversation: Conversation,
        run: Run,
        stage: Stage,
        outcome: GuardOutcome,
        user_message: str,
        executed_steps: list[ExecutedToolStep],
        *,
        tool_call: ToolCall | None = None,
        result_shape: str = "text",
        is_error: bool = False,
    ) -> ChatResponse:
        """Pause the run until a human decides whether the guard-escalated content may pass.
        The stored content is the guard's output (already redacted where a policy redacted it)."""
        reason = f"Guard escalated {stage.value.replace('_', ' ')}: {outcome.reason()}"
        approval = ApprovalRequest(
            run_id=run.id,
            conversation_id=conversation.id,
            kind="content_review",
            stage=stage.value,
            server_id=tool_call.server_id if tool_call else None,
            tool_name=tool_call.tool_name if tool_call else f"guard:{stage.value}",
            arguments_json={
                "__content__": outcome.text,
                "__user_message__": user_message,
                "__executed_steps__": self._serialize_steps(executed_steps),
                "__tool_call__": self._serialize_tool_call(tool_call) if tool_call else None,
                "__result_shape__": result_shape,
                "__is_error__": is_error,
            },
            status="pending",
            reason=reason,
            expires_at=datetime.utcnow() + timedelta(seconds=self.settings.approval_ttl_seconds),
        )
        session.add(approval)
        await session.flush()
        run.status = "waiting_approval"
        # Full reason on the approval (for the reviewer); a plain line for the user.
        assistant_message = "This needs a person's review before I can continue."
        run.paused_reason = assistant_message
        session.add(
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content=assistant_message,
                metadata_json={"notice": "waiting"},
            )
        )
        await self.audit_logger.record(
            session,
            "approval.requested",
            {
                "approval_request_id": approval.id,
                "kind": "content_review",
                "stage": stage.value,
                "tool_name": approval.tool_name,
                "reason": reason,
            },
            conversation_id=conversation.id,
            run_id=run.id,
        )
        return ChatResponse(
            conversation_id=conversation.id,
            run_id=run.id,
            status="waiting_approval",
            assistant_message=assistant_message,
            tool_call=self._serialize_tool_call(tool_call) if tool_call else None,
            executed_tool_calls=self._serialize_steps(executed_steps),
            approval_request_id=approval.id,
        )

    async def _resume_content_review(
        self,
        session: AsyncSession,
        approval: ApprovalRequest,
        run: Run,
        conversation: Conversation,
        decision: str,
    ) -> ChatResponse:
        stored = approval.arguments_json or {}
        stage = Stage(approval.stage)
        content = stored.get("__content__", "")
        user_message = stored.get("__user_message__", "")
        executed_steps = [
            self._executed_step_from_dict(item) for item in stored.get("__executed_steps__", [])
        ]
        approved = decision == "approved"

        if stage is Stage.USER_INPUT:
            if not approved:
                return self._denied_response(session, conversation, run, "Request denied in content review.")
            run.status = "running"
            tools = await self.mcp_manager.list_tools(session, refresh=False)
            return await self._run_planner_loop(session, conversation, run, content, tools, executed_steps)

        if stage is Stage.FINAL_OUTPUT:
            if not approved:
                return self._blocked_response(
                    conversation_id=conversation.id,
                    run_id=run.id,
                    run=run,
                    executed_steps=executed_steps,
                    session=session,
                    assistant_message="The answer was withheld after content review.",
                )
            return await self._complete_run(session, conversation, run, content, executed_steps)

        # Tool output: either pass the held content on, or withhold it; the run continues either way.
        call = stored["__tool_call__"]
        tool_call = ToolCall(
            server_id=call["server_id"], tool_name=call["tool_name"], arguments=call["arguments"]
        )
        if approved:
            if stored.get("__result_shape__") == "json":
                try:
                    tool_result: Any = json.loads(content)
                except json.JSONDecodeError:
                    tool_result = {"content": content, "reviewed_by_human": True}
            else:
                tool_result = {"content": content, "reviewed_by_human": True}
        else:
            tool_result = {
                "withheld_by_guard": {
                    "policies": [],
                    "reason": "denied in content review",
                    "note": (
                        "The tool returned content the guard would not pass to the model. "
                        "Continue without it."
                    ),
                }
            }
        step = await self._record_tool_step(
            session, conversation, run, tool_call, tool_result, bool(stored.get("__is_error__"))
        )
        executed_steps.append(step)
        run.status = "running"
        tools = await self.mcp_manager.list_tools(session, refresh=False)
        return await self._run_planner_loop(session, conversation, run, user_message, tools, executed_steps)

    def _denied_response(
        self, session: AsyncSession, conversation: Conversation, run: Run, message: str
    ) -> ChatResponse:
        run.status = "denied"
        run.latest_response = message
        session.add(
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content=message,
                metadata_json={"notice": "blocked"},
            )
        )
        return ChatResponse(
            conversation_id=conversation.id, run_id=run.id, status="denied", assistant_message=message
        )

    async def _enforce_conversation_budgets(
        self,
        session: AsyncSession,
        conversation: Conversation,
        run: Run,
        executed_steps: list[ExecutedToolStep],
        tool_call: ToolCall | None = None,
    ) -> ChatResponse | None:
        if conversation.token_budget is not None and conversation.spent_tokens >= conversation.token_budget:
            await self.audit_logger.record(
                session,
                "policy.budget_blocked",
                {
                    "kind": "token",
                    "spent_tokens": conversation.spent_tokens,
                    "token_budget": conversation.token_budget,
                },
                conversation_id=conversation.id,
                run_id=run.id,
            )
            return self._blocked_response(
                conversation_id=conversation.id,
                run_id=run.id,
                run=run,
                executed_steps=executed_steps,
                tool_call=tool_call,
                session=session,
                assistant_message="Execution blocked: conversation token budget exceeded.",
            )
        if conversation.cost_budget is not None and conversation.spent_cost >= conversation.cost_budget:
            await self.audit_logger.record(
                session,
                "policy.budget_blocked",
                {
                    "kind": "cost",
                    "spent_cost": conversation.spent_cost,
                    "cost_budget": conversation.cost_budget,
                },
                conversation_id=conversation.id,
                run_id=run.id,
            )
            return self._blocked_response(
                conversation_id=conversation.id,
                run_id=run.id,
                run=run,
                executed_steps=executed_steps,
                tool_call=tool_call,
                session=session,
                assistant_message="Execution blocked: conversation cost budget exceeded.",
            )
        return None

    async def _expire_approval(
        self,
        session: AsyncSession,
        approval: ApprovalRequest,
        run: Run,
        conversation: Conversation,
    ) -> None:
        approval.status = "expired"
        approval.decided_at = datetime.utcnow()
        run.status = "denied"
        run.latest_response = "Approval expired before anyone reviewed it."
        session.add(
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content=run.latest_response,
                metadata_json={"notice": "stopped"},
            )
        )
        await self.audit_logger.record(
            session,
            "approval.expired",
            {"approval_request_id": approval.id},
            conversation_id=conversation.id,
            run_id=run.id,
        )

    async def _load_policies(self, session: AsyncSession) -> list[Policy]:
        return (await session.scalars(select(Policy).where(Policy.enabled.is_(True)))).all()

    def _build_intent(
        self,
        conversation: Conversation,
        run: Run,
        server: MCPServer,
        tool_call: ToolCall,
    ) -> ToolExecutionIntent:
        return ToolExecutionIntent(
            conversation_id=conversation.id,
            run_id=run.id,
            server_id=server.id,
            server_name=server.name,
            tool_name=tool_call.tool_name,
            arguments=tool_call.arguments,
            token_budget=conversation.token_budget,
            cost_budget=conversation.cost_budget,
            spent_tokens=conversation.spent_tokens,
            spent_cost=conversation.spent_cost,
            run_tainted=bool(run.tainted),
            taint_reason=run.taint_reason,
        )

    async def _record_policy_decision(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
        run_id: str,
        tool_call: ToolCall,
        decision,
        source: str,
    ) -> None:
        await self.audit_logger.record(
            session,
            "policy.decision",
            {
                "tool_name": tool_call.tool_name,
                "server_id": tool_call.server_id,
                "arguments": tool_call.arguments,
                "verdict": decision.verdict,
                "reason": decision.reason,
                "matched_rule_ids": decision.matched_rule_ids,
                "source": source,
                # Shadow policies: what they would have decided, not acted on.
                **({"shadow": decision.shadow} if decision.shadow else {}),
            },
            conversation_id=conversation_id,
            run_id=run_id,
        )

    def _blocked_response(
        self,
        *,
        conversation_id: str,
        run_id: str,
        run: Run,
        executed_steps: list[ExecutedToolStep],
        session: AsyncSession,
        assistant_message: str,
        tool_call: ToolCall | None = None,
    ) -> ChatResponse:
        # Tagged so the chat shows it as a notice, not as an answer.
        session.add(
            Message(
                conversation_id=conversation_id,
                role="assistant",
                content=assistant_message,
                metadata_json={"notice": "blocked"},
            )
        )
        run.status = "blocked"
        run.latest_response = assistant_message
        return ChatResponse(
            conversation_id=conversation_id,
            run_id=run_id,
            status="blocked",
            assistant_message=assistant_message,
            tool_call=self._serialize_tool_call(tool_call) if tool_call else None,
            executed_tool_calls=self._serialize_steps(executed_steps),
        )

    async def _get_or_create_conversation(
        self,
        session: AsyncSession,
        conversation_id: str | None,
    ) -> tuple[Conversation, bool]:
        """Returns (conversation, created). The caller sets a new conversation's title from the
        guarded text, so an unredacted first message never becomes a title."""
        if conversation_id:
            conversation = await session.get(Conversation, conversation_id)
            if conversation:
                return conversation, False
        conversation = Conversation(title="New conversation")
        session.add(conversation)
        await session.flush()
        return conversation, True

    async def _get_conversation_history(
        self,
        session: AsyncSession,
        conversation_id: str,
        limit: int = 12,
    ) -> list[PlannerMessage]:
        rows = (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc())
                .limit(limit)
            )
        ).all()
        return [PlannerMessage(role=message.role, content=message.content) for message in reversed(rows)]

    def _serialize_tool_call(self, tool_call: ToolCall) -> dict[str, Any]:
        return {
            "server_id": tool_call.server_id,
            "tool_name": tool_call.tool_name,
            "arguments": tool_call.arguments,
        }

    def _serialize_steps(self, executed_steps: list[ExecutedToolStep]) -> list[dict[str, Any]]:
        return [
            {
                "server_id": step.tool_call.server_id,
                "tool_name": step.tool_call.tool_name,
                "arguments": step.tool_call.arguments,
                "result": step.result,
                "is_error": step.is_error,
            }
            for step in executed_steps
        ]

    def _fallback_summary(self, executed_steps: list[ExecutedToolStep]) -> str:
        if not executed_steps:
            return "No tool call was required."
        return "Completed the requested tool actions."

    def _build_approval_context(
        self,
        tool_call: ToolCall,
        user_message: str,
        executed_steps: list[ExecutedToolStep],
    ) -> dict[str, Any]:
        return {
            "__tool_arguments__": tool_call.arguments,
            "__user_message__": user_message,
            "__executed_steps__": self._serialize_steps(executed_steps),
        }

    def _extract_approval_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        tool_arguments = payload.get("__tool_arguments__", payload)
        return {
            "tool_arguments": tool_arguments,
            "user_message": payload.get("__user_message__", "approved tool execution"),
            "executed_steps": payload.get("__executed_steps__", []),
        }

    def _executed_step_from_dict(self, payload: dict[str, Any]) -> ExecutedToolStep:
        return ExecutedToolStep(
            tool_call=ToolCall(
                server_id=payload["server_id"],
                tool_name=payload["tool_name"],
                arguments=payload.get("arguments", {}),
            ),
            result=payload.get("result", {}),
            is_error=payload.get("is_error", False),
        )

    def _as_utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
