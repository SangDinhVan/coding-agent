"""Durable orchestration around existing tool implementations."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Callable
from uuid import uuid4

from memory.event_store import EventStore
from runtime.models import ApprovalDecision, ApprovalRequest, PolicyDecision, RecoveryDecision, ReplayPolicy, ToolExecutionStatus
from runtime.reducer import replay, retry_constraints, retry_intent
from runtime.safety import (
    InteractionMode, PolicyResult, ReviewVerdict, SafetyConfig, bind_action,
    evaluate_policy, normalize_action, route_approval,
)
from tools.base import BaseTool, ToolResult



def _walk_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)
    else:
        yield value


def _error(message: str) -> ToolResult:
    return ToolResult(raw=message, compact=f"Error: {message}", success=False)


class ToolExecutor:
    def __init__(
        self,
        store: EventStore,
        *,
        get_tool: Callable[[str], BaseTool | None],
        policy: Callable | None = None,
        approval_handler: Callable[[ApprovalRequest], ApprovalDecision] | None = None,
        safety_config: SafetyConfig | None = None,
        get_security_context: Callable | None = None,
        reviewer=None,
    ):
        self.store = store
        self.get_tool = get_tool
        self.policy = policy
        self.approval_handler = approval_handler
        self.safety_config = safety_config or SafetyConfig()
        self.get_security_context = get_security_context
        self.reviewer = reviewer
        self._transient_arguments: dict[str, dict] = {}

    def _screen(self, content, generation_id='', modified=False):
        from runtime.safety import screen_artifact
        from core.config import API_KEY
        annotations = screen_artifact(content, generation_id, (API_KEY,))
        annotations['agent_modified_content'] = modified
        self.store.append_event('SecurityStateUpdated', 'session', self.store.session_id, annotations)

    def request_batch(self, tool_calls: list, turn_id: str) -> list[str]:
        execution_ids = []
        for tool_call in tool_calls:
            execution_id = str(uuid4())
            execution_ids.append(execution_id)
            tool = self.get_tool(tool_call.function.name)
            replay_policy = tool.replay_policy if tool else ReplayPolicy.MANUAL
            try:
                from runtime.safety import _unique_object
                if len(tool_call.function.arguments.encode()) > 32 * 1024**2:
                    raise ValueError('oversized arguments')
                arguments = json.loads(tool_call.function.arguments, object_pairs_hook=_unique_object)
                if not isinstance(arguments, dict):
                    raise ValueError('arguments must be an object')
                parse_error = None
            except (ValueError, TypeError, AttributeError) as error:
                arguments = {}
                parse_error = str(error)
            self._transient_arguments[execution_id] = arguments
            from runtime.safety import screen_artifact
            from core.config import API_KEY
            if screen_artifact(json.dumps(arguments), '', (API_KEY,))['secret_exposure_detected']:
                self.store.append_event('SecurityStateUpdated', 'session', self.store.session_id, {'secret_exposure_detected': True})
                journal_arguments = {}
                parse_error = 'disclosure_denied'
            else:
                journal_arguments = arguments
            self.store.append_event(
                "ToolRequested",
                "tool_execution",
                execution_id,
                {
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_call.function.name,
                    "arguments": journal_arguments,
                    "parse_error": parse_error,
                    "replay_policy": replay_policy.value,
                    "attempt": 0,
                },
                turn_id=turn_id,
                causation_id=tool_call.id,
                correlation_id=turn_id,
            )
        return execution_ids

    def execute(self, execution_id: str) -> ToolResult:
        state = replay(self.store.read_all(), current_runtime_instance_id=self.store.runtime_instance_id)
        execution = state.executions[execution_id]
        if execution.status != ToolExecutionStatus.PENDING:
            return _error(f"execution {execution_id} is already {execution.status.value}")
        tool = self.get_tool(execution.tool_name)
        requested = next(
            event["payload"]
            for event in self.store.read_all()
            if event.get("aggregate_id") == execution_id and event.get("event_type") == "ToolRequested"
        )
        if tool is None:
            return self._fail(execution, f"Unknown tool: {execution.tool_name}", "unknown_tool")
        if requested.get("parse_error"):
            return self._fail(execution, "tool call arguments are not valid JSON", "invalid_json")
        arguments = self._transient_arguments.get(execution_id)
        if arguments is None:
            arguments = execution.arguments
            if any(value == "[REDACTED]" for value in _walk_values(arguments)):
                return self._fail(execution, "redacted arguments cannot be replayed after restart", "redacted_arguments")
        if state.security_state.secret_exposure_detected:
            return self._deny(execution, 'security_state_blocked')
        validation_error = tool.validate(arguments)
        if validation_error:
            return self._fail(execution, validation_error, "validation")
        self.store.append_event(
            "ToolValidated", "tool_execution", execution_id, {"arguments": arguments},
            turn_id=execution.turn_id, correlation_id=execution.turn_id,
        )
        # Never use redacted journal arguments in policy or the effect binding.
        execution.arguments = arguments
        constraint = next((item for item in retry_constraints(state)
                           if item['intent'] == retry_intent(execution)), None)
        if constraint:
            self._trace_event(execution, 'retry_forbidden', constraint,
                              {'disclosure_check': self.store.disclosure_check})
            return self._deny(execution, 'retry_forbidden', constraint['reason_code'])
        result, action, binding, context = self._evaluate(execution, tool)
        if result.decision == PolicyDecision.DENY:
            self._trace_event(execution, 'approval_route', {
                'decision': result.decision.value, 'reason_code': result.reason_code, 'route': 'deny',
            }, context)
            return self._deny(execution, result.reason_code)
        self.store.append_event(
            'ToolAuthorizationEvaluated', 'tool_execution', execution_id,
            {'binding_digest': binding.digest, 'binding': asdict(binding),
             'decision': result.decision.value, 'reason_code': result.reason_code},
            turn_id=execution.turn_id, correlation_id=execution.turn_id,
        )
        route = route_approval(result, self.safety_config)
        if execution.approval and execution.approval.approved and execution.approval.binding_digest == binding.digest:
            route = 'execute'
        elif execution.approval and execution.approval.binding_digest:
            return self._deny(execution, 'authorization_changed')
        verdict = None
        if route == 'reviewer':
            analysis = context.get('trusted_analysis', {})
            facts = {
                'goal': context.get('authenticated_goal', ''),
                'analysis_complete': analysis.get('complete') is True or action.shell_facts.get('analysis_complete') is True,
                'action': {'tool_name': action.tool_name, 'cwd': action.cwd,
                           'argument_digest': action.argument_digest, 'generation_digest': action.generation_digest,
                           'shell_facts': {key: value for key, value in action.shell_facts.items()
                                           if key in {'kind', 'executable', 'input_scope', 'analysis_complete'}}},
                'scope': {'profile_id': self.safety_config.profile_id, 'capabilities': context.get('capabilities', {})},
                'security_state': context.get('security_state', {}),
            }
            try:
                if not facts['analysis_complete']:
                    verdict = ReviewVerdict('ask', 'incomplete_analysis')
                else:
                    if context.get('disclosure_check'):
                        context['disclosure_check'](json.dumps(facts), 'reviewer')
                    verdict = self.reviewer.review(facts) if self.reviewer else ReviewVerdict('ask', 'reviewer_unavailable')
                if not isinstance(verdict, ReviewVerdict):
                    raise ValueError('invalid verdict')
            except Exception:
                verdict = ReviewVerdict('ask', 'reviewer_error')
            route = route_approval(result, self.safety_config, verdict)
        self._trace_event(execution, 'approval_route', {
            'decision': result.decision.value, 'reason_code': result.reason_code,
            'approval_mode': self.safety_config.approval_mode.value, 'route': route,
            'reviewer': asdict(verdict) if verdict else None,
        }, context)
        if route == 'deny':
            reason = verdict.reason_code if verdict and verdict.decision == 'deny' else 'human_approval_unavailable'
            return self._deny(execution, reason, verdict.reason_code if verdict else result.reason_code)
        if route == 'human':
            self.store.append_event(
                "ToolApprovalRequested", "tool_execution", execution_id,
                {"reason": verdict.reason_code if verdict else result.reason_code, 'binding_digest': binding.digest},
                turn_id=execution.turn_id, correlation_id=execution.turn_id,
            )
            if self.approval_handler is None:
                return _error("tool execution is waiting for approval")
            approval = self.approval_handler(ApprovalRequest(
                execution_id, execution.tool_name, json.loads(action.arguments_json),
                verdict.reason_code if verdict else result.reason_code, binding.digest, asdict(binding),
            ))
            event_type = "ToolApproved" if approval.approved else "ToolRejected"
            self.store.append_event(
                event_type, "tool_execution", execution_id,
                {"approved_by": approval.approved_by, "note": approval.note, 'binding_digest': binding.digest},
                turn_id=execution.turn_id, correlation_id=execution.turn_id,
            )
            if not approval.approved:
                return _error("tool execution rejected by user")
        # Approval cannot extend physical scope. Re-evaluate immediately before effects.
        checked, current_action, current_binding, current_context = self._evaluate(execution, tool, phase='pre_effect')
        if checked.decision == PolicyDecision.DENY:
            return self._deny(execution, checked.reason_code)
        if current_binding.digest != binding.digest:
            return self._deny(execution, 'authorization_changed')
        try:
            checkpoint = current_context.get('checkpoint_action')
            checkpoint_digest = (current_action.argument_digest if execution.tool_name == 'update_plan'
                                 else checkpoint(current_action) if checkpoint else None)
            if not checkpoint_digest:
                return self._deny(execution, 'checkpoint_unavailable')
            recovery_metadata = tool.recovery_metadata(**arguments)
        except Exception as error:
            reason = str(error)
            return self._deny(execution, reason if reason in {'source_conflict', 'source_identity_changed'}
                              else 'checkpoint_unavailable')
        self.store.append_event(
            'ToolGrantClaimed', 'tool_execution', execution_id,
            {'binding_digest': binding.digest, 'checkpoint_digest': checkpoint_digest, 'max_uses': 1},
            turn_id=execution.turn_id, correlation_id=execution.turn_id,
        )
        self.store.append_event(
            "ToolStarted", "tool_execution", execution_id,
            {"recovery_metadata": recovery_metadata},
            turn_id=execution.turn_id, correlation_id=execution.turn_id,
        )
        try:
            if hasattr(tool, 'authorize_runtime'):
                tool.authorize_runtime(current_action, binding)
            if hasattr(tool, "execute_runtime"):
                result = tool.execute_runtime(execution_id, execution.turn_id, **arguments)
            else:
                result = tool.run(**arguments)
        except Exception as error:
            self._screen(str(error), current_action.generation_digest)
            if getattr(getattr(tool, 'sandbox', None), 'quarantined', False) is True:
                self.store.append_event('ToolRecoveryRequired', 'tool_execution', execution_id,
                    {'reason': 'file_effect_unknown'}, turn_id=execution.turn_id, correlation_id=execution.turn_id)
                return ToolResult('recovery_required', 'Error: recovery_required', False,
                                  metadata={'reason_code': 'recovery_required'})
            return self._fail(execution, 'tool_execution_error', "exception", type(error).__name__, require_running=True)
        if execution.tool_name != 'update_plan':
            self._screen(result.raw + '\n' + result.compact, current_action.generation_digest, execution.tool_name in {'write', 'edit'})
        if getattr(getattr(tool, 'sandbox', None), 'quarantined', False) is True:
            self.store.append_event('ToolRecoveryRequired', 'tool_execution', execution_id,
                {'reason': 'file_effect_unknown'}, turn_id=execution.turn_id, correlation_id=execution.turn_id)
            return ToolResult('recovery_required', 'Error: recovery_required', False,
                              metadata={'reason_code': 'recovery_required'})
        try:
            disclosure = current_context.get('disclosure_check')
            if disclosure:
                disclosure(result.raw + '\n' + result.compact, 'audit_text')
            elif replay(self.store.read_all()).security_state.secret_exposure_detected:
                raise ValueError('secret_exposure')
        except Exception:
            result = ToolResult('disclosure_denied', 'disclosure_denied', False, metadata={'reason_code': 'disclosure_denied'})
        event_type = "ToolCompleted" if result.success else "ToolFailed"
        payload = {"result": {
            "raw": result.raw,
            "compact": result.compact,
            "success": result.success,
            "exit_code": result.exit_code,
            "metadata": result.metadata,
        }}
        if not result.success:
            payload["error"] = {"category": "tool_result", "message": result.compact}
        self.store.append_event(
            event_type, "tool_execution", execution_id, payload,
            turn_id=execution.turn_id, correlation_id=execution.turn_id,
        )
        return result

    def _trace_event(self, execution, event, fields, context):
        from model import llm

        llm.append_trace_event(event, {
            'execution_id': execution.execution_id, 'turn_id': execution.turn_id,
            'tool_name': execution.tool_name, 'cwd': execution.arguments.get('cwd', '.'),
            **{key: execution.arguments[key] for key in ('command', 'path', 'intent')
               if key in execution.arguments},
            **fields,
        }, context.get('disclosure_check'))

    def _evaluate(self, execution, tool, *, phase='authorization'):
        try:
            context = dict(self.get_security_context() if self.get_security_context else {})
            context['runtime_state'] = replay(self.store.read_all(), current_runtime_instance_id=self.store.runtime_instance_id)
            security = dict(context.get('security_state', {}))
            persisted = context['runtime_state'].security_state
            for name in ('untrusted_content_seen', 'agent_modified_content', 'secret_exposure_detected'):
                security[name] = bool(security.get(name)) or getattr(persisted, name)
            security['security_state_version'] = max(security.get('security_state_version', 0), persisted.security_state_version)
            for name in ('injection_flags', 'provenance_generation_ids'):
                security[name] = sorted(set(security.get(name, ())) | set(getattr(persisted, name)))
            if any(item.execution_id != execution.execution_id and (
                item.status in {ToolExecutionStatus.RUNNING, ToolExecutionStatus.RECOVERY_REQUIRED}
                or (item.status == ToolExecutionStatus.PENDING and item.grant_claimed)
            ) for item in context['runtime_state'].executions.values()):
                security['quarantined'] = True
            context['security_state'] = security
            action = normalize_action(execution, tool, context)
            from runtime.safety import _sha256
            image = context.get('image_digest', '')
            if execution.tool_name != 'update_plan' and (not _sha256(context.get('environment_digest')) or not _sha256(image.rsplit('@sha256:', 1)[-1].removeprefix('sha256:'))):
                return PolicyResult(PolicyDecision.DENY, 'missing_enforcement'), action, None, context
            if self.policy is None:
                return PolicyResult(PolicyDecision.DENY, 'policy_unavailable'), action, None, context
            hard = evaluate_policy(action, context.get('authority'), self.safety_config,
                                   context.get('capabilities', {}), context.get('security_state', {}))
            self._trace_event(execution, 'hard_policy', {
                'phase': phase, 'decision': hard.decision.value, 'reason_code': hard.reason_code,
                'mandatory_human_approval': hard.mandatory_human_approval,
                **({'shell_facts': action.shell_facts} if action.shell_facts else {}),
            }, context)
            if hard.decision == PolicyDecision.DENY:
                return hard, action, None, context
            static = action.shell_facts.get('kind') == 'static_readonly'
            if action.tool_name == 'bash' and (not static or action.shell_facts.get('reads_file_contents')):
                from runtime.safety import screen_artifact
                from core.config import API_KEY
                preview = context.get('shell_input_preview' if static else 'opaque_input_preview')
                if not callable(preview):
                    return PolicyResult(PolicyDecision.DENY, 'missing_enforcement'), action, None, context
                try:
                    inputs = json.dumps(preview(action if static else action.generation_digest))
                    blocked = screen_artifact(inputs, action.generation_digest, (API_KEY,))['secret_exposure_detected']
                except Exception:
                    return PolicyResult(PolicyDecision.DENY, 'static_inputs_unavailable' if static else 'opaque_inputs_unavailable'), action, None, context
                if blocked:
                    # Trusted preflight has disclosed nothing; cancel this action without tainting the session.
                    return PolicyResult(PolicyDecision.DENY, 'static_inputs_not_disclosable' if static else 'opaque_inputs_not_disclosable'), action, None, context
            value = self.policy(execution)
            restriction = value if isinstance(value, PolicyResult) else PolicyResult(PolicyDecision(value), 'policy_' + PolicyDecision(value).value)
            order = {PolicyDecision.ALLOW: 0, PolicyDecision.REVIEW: 1, PolicyDecision.ASK: 2, PolicyDecision.DENY: 3}
            selected = restriction if order[restriction.decision] > order[hard.decision] else hard
            selected = PolicyResult(selected.decision, selected.reason_code,
                                    hard.mandatory_human_approval or restriction.mandatory_human_approval)
            binding = bind_action(action, context.get('authority'), self.safety_config, context, selected)
            return selected, action, binding, context
        except ValueError:
            return PolicyResult(PolicyDecision.DENY, 'invalid_action'), None, None, {}
        except Exception:
            return PolicyResult(PolicyDecision.DENY, 'policy_unavailable'), None, None, {}

    def _deny(self, execution, reason, review_reason=None):
        metadata = {'reason_code': reason}
        if review_reason:
            metadata['review_reason_code'] = review_reason
        guidance = {
            'static_inputs_not_disclosable': 'The selected command input cannot be safely disclosed. No command ran. '
                'Choose a specific non-sensitive file or another task step; do not filter the same secret-bearing input.',
            'static_inputs_unavailable': 'The selected command input is unavailable or exceeds the preview limit. No command ran. '
                'Use read for a smaller specific file.',
            'retry_forbidden': 'This intent already failed twice without progress. Choose another tool: '
                'use read to inspect or verify a file, write/edit for changes, or use successful file execution IDs as plan evidence.',
            'source_conflict': 'The project file changed outside this session. Nothing was overwritten. '
                'Start a new session to use the current project files.',
            'opaque_inputs_not_disclosable': 'The complete shell input cannot be safely disclosed. No command ran. '
                'Continue this task using read for specific files and write/edit for changes. '
                'Workspace file names are provided in the system context. Shell tests require a clean workspace.',
            'opaque_inputs_unavailable': 'The complete shell input is unavailable or exceeds the preview limit. No command ran. '
                'Continue using read for specific files and write/edit for changes.',
        }.get(reason)
        message = 'Error: ' + reason + ('. ' + guidance if guidance else '')
        result = ToolResult(message, message, False, metadata=metadata)
        self.store.append_event(
            'ToolCancelled', 'tool_execution', execution.execution_id,
            {'reason': reason, 'result': asdict(result)},
            turn_id=execution.turn_id, correlation_id=execution.turn_id,
        )
        return result

    def resolve_approval(
        self,
        execution_id: str,
        approved: bool,
        note: str,
        approved_by: str = "user",
    ) -> ToolResult:
        state = replay(self.store.read_all())
        execution = state.executions.get(execution_id)
        if execution is None or execution.status != ToolExecutionStatus.WAITING_APPROVAL:
            raise ValueError("execution is not waiting for approval")
        event_type = "ToolApproved" if approved else "ToolRejected"
        self.store.append_event(
            event_type, "tool_execution", execution_id,
            {"approved_by": approved_by, "note": note, 'binding_digest': execution.approval_binding_digest},
            turn_id=execution.turn_id, correlation_id=execution.turn_id,
        )
        if not approved:
            return _error("tool execution rejected by user")
        return self.execute(execution_id)

    def resolve_recovery(
        self,
        execution_id: str,
        decision: RecoveryDecision,
        note: str,
    ) -> ToolResult:
        if not note.strip():
            raise ValueError("recovery decision requires a note")
        state = replay(self.store.read_all())
        execution = state.executions.get(execution_id)
        if execution is None or execution.status != ToolExecutionStatus.RECOVERY_REQUIRED:
            raise ValueError("execution does not require recovery")
        decision = RecoveryDecision(decision)
        tool = self.get_tool(execution.tool_name)
        if tool is None:
            raise ValueError("recovery requires the original bound tool")
        lifecycle = [event['event_type'] for event in self.store.read_all()
                     if event.get('aggregate_id') == execution_id
                     and event.get('event_type') in {'ToolStarted', 'ToolGrantClaimed', 'ToolRecoveryRequired'}]
        if lifecycle and lifecycle[-1] != 'ToolRecoveryRequired':
            self.store.append_event(
                'ToolRecoveryRequired', 'tool_execution', execution_id,
                {'reason': 'authenticated_recovery'}, turn_id=execution.turn_id, correlation_id=execution.turn_id,
            )
        if decision == RecoveryDecision.RETRY:
            if execution.replay_policy == ReplayPolicy.MANUAL:
                raise ValueError("manual recovery policy forbids retry")
            if execution.replay_policy == ReplayPolicy.RECONCILABLE:
                guarded = 'before_state_digest' in execution.recovery_metadata
                current_hash = tool.recovery_state_fingerprint(execution.recovery_metadata) if guarded else tool.recovery_fingerprint(execution.recovery_metadata)
                if current_hash is None:
                    raise ValueError("sandbox recovery evidence is unavailable")
                if current_hash != execution.recovery_metadata.get('before_state_digest' if guarded else 'before_hash'):
                    raise ValueError("file state does not match the pre-effect hash")
            self.store.append_event(
                'ToolRecoveredAsFailed', 'tool_execution', execution_id,
                {'note': note, 'error': {'category': 'recovery_retry', 'message': 'superseded by authenticated retry'}},
                turn_id=execution.turn_id, correlation_id=execution.turn_id,
            )
            retry_id = str(uuid4())
            arguments = self._transient_arguments.get(execution_id, execution.arguments)
            self._transient_arguments[retry_id] = arguments
            self.store.append_event(
                'ToolRequested', 'tool_execution', retry_id,
                {'tool_call_id': execution.tool_call_id, 'tool_name': execution.tool_name,
                 'arguments': arguments, 'replay_policy': execution.replay_policy.value,
                 'attempt': execution.attempt + 1, 'retry_of_execution_id': execution_id},
                turn_id=execution.turn_id, correlation_id=execution.turn_id,
            )
            return self.execute(retry_id)
        if decision == RecoveryDecision.COMPLETED:
            if execution.replay_policy == ReplayPolicy.RECONCILABLE:
                guarded = 'expected_after_state_digest' in execution.recovery_metadata
                current_hash = tool.recovery_state_fingerprint(execution.recovery_metadata) if guarded else tool.recovery_fingerprint(execution.recovery_metadata)
                if current_hash is None:
                    raise ValueError("sandbox recovery evidence is unavailable")
                if current_hash != execution.recovery_metadata.get('expected_after_state_digest' if guarded else 'expected_after_hash'):
                    raise ValueError("file state does not match the expected result type/mode/hash")
            result = ToolResult(
                raw=f"Recovered as completed: {note}",
                compact=f"Recovered as completed: {note}",
                success=True,
            )
            self.store.append_event(
                "ToolRecoveredAsCompleted", "tool_execution", execution_id,
                {"note": note, "result": {"raw": result.raw, "compact": result.compact, "success": True, "exit_code": None}},
                turn_id=execution.turn_id, correlation_id=execution.turn_id,
            )
            return result
        result = _error(f"Recovered as failed: {note}")
        self.store.append_event(
            "ToolRecoveredAsFailed", "tool_execution", execution_id,
            {"note": note, "error": {"category": "recovery", "message": note}},
            turn_id=execution.turn_id, correlation_id=execution.turn_id,
        )
        return result

    def _fail(
        self,
        execution,
        message: str,
        category: str,
        exception_type: str | None = None,
        *,
        require_running: bool = False,
    ) -> ToolResult:
        from runtime.safety import screen_artifact
        from core.config import API_KEY
        if screen_artifact(message, '', (API_KEY,))['secret_exposure_detected']:
            self.store.append_event('SecurityStateUpdated', 'session', self.store.session_id, {'secret_exposure_detected': True})
            message = 'disclosure_denied'
        result = _error(message)
        self.store.append_event(
            "ToolFailed",
            "tool_execution",
            execution.execution_id,
            {
                "error": {
                    "category": category,
                    "message": message,
                    "exception_type": exception_type,
                    "retryable": False,
                    "details": {},
                },
                "result": {"raw": result.raw, "compact": result.compact, "success": False, "exit_code": None},
            },
            turn_id=execution.turn_id,
            correlation_id=execution.turn_id,
        )
        return result
