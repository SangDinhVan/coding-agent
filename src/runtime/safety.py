"""Controller-owned safety facts and fail-closed approval routing."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from runtime.models import PolicyDecision


class DisclosureDenied(RuntimeError):
    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(reason_code)


def screen_artifact(content: str, generation_id: str, known_secrets=()) -> dict:
    if not isinstance(content, str) or len(content.encode()) > 32 * 1024**2:
        raise DisclosureDenied('artifact_budget')
    injection = any(pattern in content.casefold() for pattern in ('ignore previous instructions', 'ignore all previous', 'system prompt', 'exfiltrate', 'disable safety'))
    secret = any(value and value in content for value in known_secrets) or bool(re.search(
        r'(?:sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?:API_KEY|PASSWORD|ACCESS_TOKEN|SECRET)\s*[=:]\s*[\"\x27]?[A-Za-z0-9_/-]{8,})', content, re.IGNORECASE))
    return {'untrusted_content_seen': True, 'injection_flags': ['instruction_pattern'] if injection else [],
            'secret_exposure_detected': secret, 'provenance_generation_ids': [generation_id] if generation_id else []}


class DisclosureGate:
    SINKS = frozenset({'main_model', 'reviewer', 'summarizer', 'memory', 'ui', 'audit_text', 'trace', 'patch_export'})

    def __init__(self, known_secrets=()):
        self.known_secrets = tuple(value for value in known_secrets if isinstance(value, str) and value)

    def check(self, content, sink, authority, security_state, provenance):
        from runtime.models import SecurityState
        if sink not in self.SINKS or not isinstance(authority, AuthorityRecord) or sink not in authority.allowed_sinks or not authority.authority_id:
            raise DisclosureDenied('missing_disclosure_authority')
        if not isinstance(security_state, SecurityState) or not provenance or any(not isinstance(x, str) or not x for x in provenance):
            raise DisclosureDenied('missing_disclosure_provenance')
        if security_state.secret_exposure_detected or screen_artifact(content, provenance[-1], self.known_secrets)['secret_exposure_detected']:
            raise DisclosureDenied('secret_exposure')
        return content


class ApprovalMode(str, Enum):
    ASK_ALL = 'ask_all'
    ASK_ON_ESCALATION = 'ask_on_escalation'
    AUTO_REVIEW = 'auto_review'


class InteractionMode(str, Enum):
    INTERACTIVE = 'interactive'
    HEADLESS = 'headless'


def digest_json(value) -> str:
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class SafetyConfig:
    approval_mode: ApprovalMode = ApprovalMode.ASK_ON_ESCALATION
    interaction_mode: InteractionMode = InteractionMode.INTERACTIVE
    policy_revision: str = 'safety-v1'
    allowed_modes: tuple[ApprovalMode, ...] = tuple(ApprovalMode)
    pinned_mode: ApprovalMode | None = None
    profile_id: str = 'shadow'
    constraints_digest: str = digest_json({})
    excluded_paths: tuple[str, ...] = ()
    human_gate_paths: tuple[str, ...] = ()

    def __post_init__(self):
        try:
            object.__setattr__(self, 'approval_mode', ApprovalMode(self.approval_mode))
            object.__setattr__(self, 'interaction_mode', InteractionMode(self.interaction_mode))
            object.__setattr__(self, 'allowed_modes', tuple(ApprovalMode(x) for x in self.allowed_modes))
            if self.pinned_mode is not None:
                object.__setattr__(self, 'pinned_mode', ApprovalMode(self.pinned_mode))
            if not all(isinstance(x, str) and x for x in (self.policy_revision, self.profile_id, self.constraints_digest)):
                raise ValueError()
            for name in ('excluded_paths', 'human_gate_paths'):
                object.__setattr__(self, name, tuple(sorted(set(getattr(self, name)))))
        except (TypeError, ValueError):
            raise ValueError('invalid_approval_configuration') from None
        if self.approval_mode not in self.allowed_modes or (
            self.pinned_mode is not None and self.approval_mode != self.pinned_mode
        ):
            raise ValueError('approval_mode_not_permitted')

    @property
    def retained_constraints(self):
        return {'excluded_paths': list(self.excluded_paths), 'human_gate_paths': list(self.human_gate_paths)}


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyDecision
    reason_code: str
    mandatory_human_approval: bool = False
    binding: object = None

    def __post_init__(self):
        object.__setattr__(self, 'decision', PolicyDecision(self.decision))


@dataclass(frozen=True)
class ReviewVerdict:
    decision: str
    reason_code: str

    def __post_init__(self):
        if self.decision not in {'allow', 'ask', 'deny'}:
            raise ValueError('reviewer_invalid_response')


def route_approval(result: PolicyResult, config: SafetyConfig, verdict: ReviewVerdict | None = None) -> str:
    if result.decision == PolicyDecision.DENY:
        return 'deny'
    if result.mandatory_human_approval or result.decision == PolicyDecision.ASK or config.approval_mode == ApprovalMode.ASK_ALL:
        route = 'human'
    elif result.decision == PolicyDecision.REVIEW:
        if config.approval_mode != ApprovalMode.AUTO_REVIEW:
            route = 'human'
        elif verdict is None:
            return 'reviewer'
        else:
            route = {'allow': 'execute', 'ask': 'human', 'deny': 'deny'}[verdict.decision]
    else:
        route = 'execute'
    return 'deny' if route == 'human' and config.interaction_mode == InteractionMode.HEADLESS else route


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _read_config(path: Path | None, keys: set[str]) -> dict:
    if path is None:
        return {}
    from core.paths import open_directory
    parent = None
    try:
        parent = open_directory(path.parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ValueError('invalid_approval_configuration') from None
    finally:
        if parent is not None:
            os.close(parent)
    try:
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('invalid config file')
            data = file.read(65537)
            after = os.fstat(file.fileno())
            if (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink) != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink):
                raise ValueError('config_changed')
        if len(data) > 65536:
            raise ValueError('oversized config')
        config = json.loads(data, object_pairs_hook=_unique_object)
        if not isinstance(config, dict) or config.keys() - keys:
            raise ValueError('invalid config keys')
        return config
    except (OSError, ValueError, TypeError):
        raise ValueError('invalid_approval_configuration') from None


def canonical_path(value: str, *, allow_root=False) -> str:
    if not isinstance(value, str) or not value or '\x00' in value or '\\' in value:
        raise ValueError('invalid_path')
    if allow_root and value == '.':
        return value
    if value.startswith('/') or any(part in {'', '.', '..'} for part in value.split('/')):
        raise ValueError('invalid_path')
    return value


def load_safety_config(host_path, repo_path, requested_mode, interaction_mode, retained_constraints):
    try:
        host_path = Path(host_path) if host_path is not None else None
        repo_path = Path(repo_path) if repo_path is not None else None
        if host_path is not None and repo_path is not None and host_path.resolve().is_relative_to(repo_path.parent.resolve()):
            raise ValueError('host policy inside source')
        restrictions = {'excluded_paths', 'human_gate_paths'}
        if host_path is not None and not host_path.exists():
            raise ValueError('missing host policy')
        host = _read_config(host_path, restrictions | {'allowed_modes', 'default_mode', 'pinned_mode', 'profile_id'})
        repo = _read_config(repo_path, restrictions)
        merged = {}
        for key in restrictions:
            values = []
            for source in (host, repo, retained_constraints):
                items = source.get(key, [])
                if not isinstance(items, (list, tuple)):
                    raise ValueError('invalid paths')
                values.extend(canonical_path(item) for item in items)
            merged[key] = tuple(sorted(set(values)))
        allowed = host.get('allowed_modes', list(ApprovalMode))
        if not isinstance(allowed, list):
            raise ValueError('invalid modes')
        return SafetyConfig(
            approval_mode=requested_mode or host.get('pinned_mode') or host.get('default_mode', 'ask_on_escalation'),
            interaction_mode=interaction_mode,
            allowed_modes=tuple(allowed), pinned_mode=host.get('pinned_mode'),
            profile_id=host.get('profile_id', 'shadow'),
            policy_revision=digest_json(host), constraints_digest=digest_json(merged), **merged,
        )
    except (OSError, TypeError, ValueError) as error:
        if str(error) == 'approval_mode_not_permitted':
            raise
        raise ValueError('invalid_approval_configuration') from None


@dataclass(frozen=True)
class AuthorityRecord:
    authority_id: str
    session_id: str
    source_selection_digest: str
    private_scope: str
    allowed_operations: tuple[str, ...]
    allowed_sinks: tuple[str, ...]


@dataclass(frozen=True)
class NormalizedAction:
    execution_id: str
    session_id: str
    tool_name: str
    arguments_json: str
    argument_digest: str
    generation_digest: str
    path: str | None
    cwd: str
    scope: str
    path_role: str
    before_digest: str
    content_digest: str
    opaque: bool
    egress: bool
    deletion: bool
    shell_facts: dict = field(default_factory=dict)


def normalize_action(execution, tool, context: dict) -> NormalizedAction:
    from tools.base import validate_schema

    arguments = execution.arguments
    if not isinstance(arguments, dict):
        raise ValueError('invalid_arguments')
    try:
        serialized = json.dumps(arguments, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(serialized.encode()) > 32 * 1024**2:
            raise ValueError('oversized_arguments')
    except (TypeError, ValueError):
        raise ValueError('invalid_arguments') from None
    schema = tool.parameters
    if execution.tool_name == 'update_plan':
        schema = tool.schema(context.get('runtime_state'))['function']['parameters']
    if validate_schema(arguments, schema):
        raise ValueError('invalid_arguments')
    path = canonical_path(arguments['path']) if 'path' in arguments else None
    cwd = canonical_path(arguments.get('cwd', '.'), allow_root=True)
    from runtime.shell import analyze_shell
    shell_facts = analyze_shell(arguments['command'], cwd) if execution.tool_name == 'bash' else {}
    if shell_facts.get('kind') == 'static_readonly':
        shell_facts['input_roles'] = {
            item: context['classify_path'](item) if context.get('classify_path') else context.get('path_roles', {}).get(item, 'ordinary')
            for item in shell_facts['input_paths']
        }
    content = {key: arguments[key] for key in ('content', 'old_string', 'new_string') if key in arguments}
    return NormalizedAction(
        execution.execution_id, execution.session_id, execution.tool_name, serialized,
        digest_json(arguments), context.get('generation_digest', ''), path, cwd,
        context.get('scope', ''), context['classify_path'](path) if context.get('classify_path') else context.get('path_roles', {}).get(path, 'ordinary'),
        digest_json(context['before_states'].get(path, {'kind': 'absent'})) if 'before_states' in context else context.get('before_digest', ''),
        digest_json(content) if content else '',
        execution.tool_name == 'bash' and shell_facts['kind'] == 'opaque', execution.tool_name not in {'read', 'write', 'edit', 'bash', 'update_plan'}
        and execution.tool_name not in context.get('capabilities', {}).get('internal_tools', ()), False, shell_facts,
    )


def _under(path, roots):
    return any(path == root or path.startswith(root + '/') for root in roots)


def _sha256(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def evaluate_policy(action, authority, config, capabilities, security_state) -> PolicyResult:
    def deny(reason):
        return PolicyResult(PolicyDecision.DENY, reason)

    if action.tool_name == 'update_plan':
        return PolicyResult(PolicyDecision.ALLOW, 'internal_state')
    if not isinstance(authority, AuthorityRecord) or authority.session_id != action.session_id:
        return deny('missing_authority')
    if not authority.authority_id or not _sha256(authority.source_selection_digest):
        return deny('missing_authority')
    if not _sha256(action.generation_digest):
        return deny('missing_generation')
    if action.scope != authority.private_scope or capabilities.get('scope') != authority.private_scope:
        return deny('forbidden_scope')
    if capabilities.get('profile_id') != config.profile_id or config.profile_id != 'shadow':
        return deny('unsupported_profile')
    if action.tool_name not in authority.allowed_operations or action.egress or action.deletion:
        return deny('forbidden_operation')
    if security_state.get('secret_exposure_detected') or security_state.get('quarantined'):
        return deny('security_state_blocked')
    if action.path_role in {'trusted', 'secret', 'excluded'} or (action.path and _under(action.path, config.excluded_paths)):
        return deny('protected_path')
    if action.tool_name == 'bash':
        if not capabilities.get('opaque_ro') or capabilities.get('opaque_rw'):
            return deny('unsupported_profile')
        if action.shell_facts.get('kind') == 'static_readonly':
            from sandbox.workspace import _HARD_PATTERNS, _matches
            if not action.shell_facts.get('scope_valid'):
                return deny('forbidden_scope')
            if any(_matches(path, _HARD_PATTERNS) or _under(path, config.excluded_paths)
                   or action.shell_facts['input_roles'].get(path) in {'trusted', 'secret', 'excluded'}
                   for path in action.shell_facts['input_paths']):
                return deny('protected_path')
            return PolicyResult(PolicyDecision.ALLOW, 'static_readonly')
        if action.shell_facts.get('kind') == 'project_execution':
            return PolicyResult(PolicyDecision.REVIEW, 'project_execution')
        return PolicyResult(PolicyDecision.REVIEW, 'opaque_execution')
    if action.tool_name in {'read', 'write', 'edit'}:
        if not capabilities.get('file_adapter'):
            return deny('missing_enforcement')
        mandatory = action.tool_name in {'write', 'edit'} and (
            action.path_role == 'project_control' or _under(action.path, config.human_gate_paths)
        )
        if mandatory and (not action.before_digest or not action.content_digest):
            return deny('missing_before_image')
        return PolicyResult(PolicyDecision.ALLOW, 'scoped_file', mandatory)
    if action.tool_name in capabilities.get('internal_tools', ()):
        return PolicyResult(PolicyDecision.ALLOW, 'internal_state')
    return deny('unsupported_tool')


@dataclass(frozen=True)
class ActionBinding:
    execution_id: str
    session_id: str
    tool_name: str
    argument_digest: str
    generation_digest: str
    image_digest: str
    environment_digest: str
    cwd: str
    profile_id: str
    scope_digest: str
    authority_digest: str
    policy_revision: str
    security_digest: str
    approval_mode: str
    interaction_mode: str
    constraints_digest: str
    decision_digest: str
    before_digest: str
    content_digest: str

    @property
    def digest(self):
        return digest_json(asdict(self))


def bind_action(action, authority, config, context, result):
    return ActionBinding(
        action.execution_id, action.session_id, action.tool_name, action.argument_digest,
        action.generation_digest, context.get('image_digest', ''), context.get('environment_digest', ''),
        action.cwd, config.profile_id, digest_json(context.get('capabilities', {})),
        digest_json(asdict(authority)) if authority is not None else '', config.policy_revision,
        digest_json(context.get('security_state', {})), config.approval_mode.value,
        config.interaction_mode.value, config.constraints_digest,
        digest_json({'decision': result.decision.value, 'mandatory': result.mandatory_human_approval, 'reason': result.reason_code}),
        action.before_digest, action.content_digest,
    )


class CoreLLMReviewer:
    def __init__(self, model=None, base_url=None, api_key=None, disclosure_check=None):
        self.model, self.base_url, self.api_key = model, base_url, api_key
        self.disclosure_check = disclosure_check

    def review(self, facts: dict) -> ReviewVerdict:
        from model import llm

        keys = {'goal', 'analysis_complete', 'action', 'scope', 'security_state'}
        if not isinstance(facts, dict) or facts.keys() != keys or facts.get('analysis_complete') is not True:
            return ReviewVerdict('ask', 'incomplete_analysis')
        if not callable(self.disclosure_check):
            return ReviewVerdict('ask', 'reviewer_disclosure_unavailable')
        try:
            content = json.dumps(facts, sort_keys=True, allow_nan=False)
            if len(content.encode()) > 65536:
                return ReviewVerdict('ask', 'incomplete_analysis')
            self.disclosure_check(content, 'reviewer')
            response = llm.complete(
                messages=[{'role': 'system', 'content': (
                    'Review only the authenticated controller facts. You cannot grant scope. '
                    'Return exactly JSON with decision (allow, ask, deny) and reason_code '
                    '(reviewer_allow, reviewer_ask, reviewer_deny). Missing facts require ask.'
                )}, {'role': 'user', 'content': content}],
                tools=None, stream=False, timeout=30, max_retries=0,
                model=self.model, base_url=self.base_url, api_key=self.api_key,
                disclosure_check=self.disclosure_check, disclosure_sink='reviewer',
            )
        except Exception as error:
            timeout = isinstance(error, TimeoutError) or type(error).__name__ == 'APITimeoutError'
            return ReviewVerdict('ask', 'reviewer_timeout' if timeout else 'reviewer_error')
        try:
            message = response.choices[0].message
            if getattr(message, 'tool_calls', None):
                raise ValueError('tool calls')
            if not isinstance(message.content, str) or len(message.content.encode()) > 4096:
                raise ValueError('invalid content')
            self.disclosure_check(message.content, 'reviewer')
            data = json.loads(message.content, object_pairs_hook=_unique_object)
            if not isinstance(data, dict) or data.keys() != {'decision', 'reason_code'}:
                raise ValueError('schema')
            if data['reason_code'] != {'allow': 'reviewer_allow', 'ask': 'reviewer_ask', 'deny': 'reviewer_deny'}.get(data['decision']):
                raise ValueError('reason')
            return ReviewVerdict(**data)
        except (ValueError, TypeError, AttributeError, IndexError, KeyError):
            return ReviewVerdict('ask', 'reviewer_invalid_response')
