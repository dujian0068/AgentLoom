export type Resource = { id: string; name: string; [key: string]: any };
export type ContextPolicy = {
  context_ratio: number;
  target_ratio: number;
  user_turns: number | null;
  model_steps: number | null;
  max_compaction_calls: number;
};
export type ModelBudget = {
  context_window: number;
  max_output_tokens: number;
  safety_margin_tokens: number;
};
export type ModelMetadata = {
  source: "manual" | "provider" | "official_manifest" | "platform_default";
  source_url: string;
  verified_at: string;
  provider_max_output_tokens: number | null;
  chat_compatible: boolean | null;
};
export type DiscoveredModel = ModelBudget & {
  id: string;
  name: string;
  purpose: "chat" | "embedding";
  metadata: ModelMetadata;
};
export type Child = {
  id: string;
  name: string;
  description: string;
  prompt: string;
  skills: string[];
  tools: string[];
  wiki: string[];
};
export type HookBinding = {
  binding_id: string;
  hook_id: string;
  point: string;
  version?: string | null;
  priority?: number;
  timeout?: number;
  failure_policy?: "block" | "continue";
  config?: Record<string, unknown>;
  purposes?: string[] | null;
  instances?: ("main" | "child")[];
  targets?: string[];
};
export type Agent = {
  id?: string;
  name: string;
  model: string;
  prompt: string;
  mode: "react" | "plan";
  context_policy?: ContextPolicy;
  skills: string[];
  tools: string[];
  wiki: string[];
  subs: Child[];
  hooks?: HookBinding[];
  published?: number;
};
export type AgentDraft = Agent & { context_policy: ContextPolicy };
export type RunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted"
  | "needs_input";
export type RunEvent = {
  seq: number;
  kind: string;
  instance?: string;
  [key: string]: any;
};
export type Citation = {
  id: string;
  document_id: string;
  file: string;
  start_line: number;
  end_line: number;
};
export type RunState = {
  id: string;
  status: RunStatus;
  input: string;
  output: string;
  error: string | null;
  session_id: string;
  resumable: boolean;
  requires_model_retry: boolean;
  events: RunEvent[];
  artifacts: string[];
};
export type RunResponse = {
  run_id: string;
  session_id: string;
  version: number;
  after?: number;
};
export type Message = {
  role: "user" | "assistant";
  text: string;
  run_id?: string;
  citations?: Citation[];
  artifacts?: string[];
};
