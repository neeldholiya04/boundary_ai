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
  dropped_async?: number;
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
