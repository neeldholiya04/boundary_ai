export type Conversation = {
  id: string;
  title: string;
  token_budget: number | null;
  cost_budget: number | null;
  spent_tokens: number;
  spent_cost: number;
  created_at: string;
  updated_at: string;
  latest_run_status: string;
  pending_approval: boolean;
  pending_approval_reason: string | null;
  latest_message_preview: string;
};

export type Message = {
  id: string;
  role: string;
  content: string;
  metadata: Record<string, unknown> | null;
  created_at: string;
};

export type ChatResponse = {
  conversation_id: string;
  run_id: string;
  status: string;
  assistant_message: string;
  tool_call?: {
    server_id: string;
    tool_name: string;
    arguments: Record<string, unknown>;
  } | null;
  executed_tool_calls: {
    server_id: string;
    tool_name: string;
    arguments: Record<string, unknown>;
    result: Record<string, unknown>;
    is_error: boolean;
  }[];
  approval_request_id?: string | null;
  trace_url?: string | null;
  // What the guard removed from the user's own message (e.g. a pasted key), to show next to the reply.
  guard_notices?: string[];
};

export type Policy = {
  id: string;
  name: string;
  rule_type: string;
  enabled: boolean;
  priority: number;
  target_tool: string | null;
  target_server_id: string | null;
  conditions: Record<string, unknown> | null;
  action: Record<string, unknown> | null;
  created_at: string;
};

export type Approval = {
  id: string;
  run_id: string;
  conversation_id: string;
  server_id: string | null;
  tool_name: string;
  // "tool_call": policy-engine or tool-argument approval; "content_review": guard escalation.
  kind: "tool_call" | "content_review";
  stage: "user_input" | "tool_output" | "final_output" | null;
  // Held content for content reviews (already redacted where a policy redacted it).
  content: string | null;
  arguments: Record<string, unknown>;
  status: string;
  reason: string;
  expires_at: string;
  comment: string | null;
  created_at: string;
};

export type AuditEvent = {
  id: string;
  conversation_id: string | null;
  run_id: string | null;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type MCPServer = {
  id: string;
  name: string;
  transport: string;
  enabled: boolean;
  config: Record<string, unknown>;
  last_error: string | null;
  last_discovered_at: string | null;
  tool_count: number;
  status: string;
};

export type MCPTool = {
  server_id: string;
  server_name: string;
  transport: string;
  name: string;
  description: string | null;
  input_schema: Record<string, unknown> | null;
};

export type GuardPolicyStatus = {
  id: string;
  // file: the reviewed baseline in policies/guard.yaml; rule: written in the dashboard.
  origin: "file" | "rule";
  tools: string[] | null;
  stages: string[];
  detector: string;
  action: string;
  mode: "enforce" | "shadow" | "off";
  execution: "blocking" | "async";
  detects: string[];
  threshold: number | null;
};

export type GuardStatus = {
  enabled: boolean;
  policy_file?: string;
  version?: number;
  config_hash?: string;
  modes?: Record<string, number>;
  policies?: GuardPolicyStatus[];
  rule_errors?: Record<string, string>;
  dropped_async?: number;
};

// ---- Guard rules (written in the dashboard) --------------------------------------------------

export type RuleCheckType = "keywords" | "pattern" | "topic" | "llm_judge" | "always";
export type RuleAction = "flag" | "redact" | "escalate" | "block";
export type GuardMode = "off" | "shadow" | "enforce";

export type RuleCheck = {
  type: RuleCheckType;
  keywords?: string[];
  case_sensitive?: boolean;
  whole_word?: boolean;
  allow_examples?: string[];
  margin?: number;
  patterns?: string[];
  flags?: string[];
  examples?: string[];
  policy?: string;
  model?: string;
  threshold?: number;
  label?: string;
};

export type RuleSpec = {
  name: string;
  description?: string | null;
  stages: GuardStage[];
  tools?: string[] | null;
  check: RuleCheck;
  action: RuleAction;
  mode: GuardMode;
  execution?: "blocking" | "async";
  on_error?: "fail_open" | "fail_closed" | null;
  timeout_ms?: number | null;
  taints_run?: boolean;
  tests: { should_fire: string[]; should_pass: string[] };
};

export type GuardRule = {
  id: string;
  policy_id: string;
  version: number;
  spec: RuleSpec;
  active: boolean;
  error: string | null;
  created_at: string;
  updated_at: string;
};

export type CheckTypeInfo = { type: RuleCheckType; label: string; can_redact: boolean; hint: string };
export type CheckTypes = { stages: GuardStage[]; tool_stages: GuardStage[]; judge_model: string; checks: CheckTypeInfo[] };

export type RuleVerdict = {
  fired: boolean;
  action: string;
  reasons: string[];
  error: string | null;
  redacted: string | null;
  latency_ms: number;
};

export type RuleDryRun = {
  passed: boolean;
  examples: (RuleVerdict & { text: string; expected: "fire" | "pass"; ok: boolean })[];
  benign: { checked: number; fired: number; rate: number; samples: { record_id: string; text: string; reasons: string[] }[] } | null;
  elapsed_ms: number;
};

export type GuardStat = {
  policy_id: string;
  mode: string;
  execution: string;
  action: string;
  checks: number;
  would_fire: number;
  would_actions: Record<string, number>;
  errors: number;
  would_fire_rate: number | null;
  p50_ms: number | null;
  p99_ms: number | null;
};

export type GuardStats = {
  enabled: boolean;
  sampled?: number;
  config_hash?: string;
  policies?: GuardStat[];
};

// ---- Playground -------------------------------------------------------------------------------

export type GuardStage = "user_input" | "tool_args" | "tool_output" | "final_output";

export type ScanSpan = { start: number; end: number; label: string };

export type ScanPolicy = {
  policy_id: string;
  mode: string;
  execution: string;
  detector: string;
  action: string;
  would_action: string;
  score: number | null;
  threshold: number | null;
  reasons: string[];
  spans: ScanSpan[];
  latency_ms: number;
  error: string | null;
};

export type ScanResult = {
  stage: GuardStage;
  action: string;
  would_action: string;
  text: string;
  latency_ms: number;
  config_hash: string;
  pending_async: string[];
  policies: ScanPolicy[];
};

export type PlaygroundScenario = {
  id: string;
  split: string;
  user_task: string;
  notes: string;
  page: string;
};

export type PlaygroundScenarios = {
  scenarios: PlaygroundScenario[];
  custom_task: string;
  live_runs: boolean;
  max_input_chars: number;
};

export type AttackToolCall = { tool: string; arguments: Record<string, unknown> };

export type AttackRun = {
  config: string;
  label: string;
  status: string;
  final_message: string;
  tool_calls: AttackToolCall[];
  guard_flags: { stage: string; policy: string; mode: string; action: string; would_action: string; tool: string }[];
  unexpected_actions: AttackToolCall[];
  attack_success: boolean | null;
  task_success: boolean;
  steps: number;
  cost_usd: number;
  run_id: string;
  trace_url: string | null;
};

export type AttackResult = {
  scenario_id: string;
  user_task: string;
  source: "replay" | "live";
  runs: AttackRun[];
};

