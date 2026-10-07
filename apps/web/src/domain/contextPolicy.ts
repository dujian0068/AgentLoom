import type { Agent, AgentDraft, ContextPolicy, ModelBudget } from "./types";

export function defaultContextPolicy(): ContextPolicy {
  return {
    context_ratio: 0.8,
    target_ratio: 0.6,
    user_turns: null,
    model_steps: null,
    max_compaction_calls: 4,
  };
}

export function prepareAgentDraft(agent: Agent): AgentDraft {
  const draft = JSON.parse(JSON.stringify(agent));
  draft.context_policy = {
    ...defaultContextPolicy(),
    ...draft.context_policy,
  };
  return draft;
}

export function modelBudget(value: Partial<ModelBudget> = {}): ModelBudget {
  return {
    context_window: value.context_window ?? 32768,
    max_output_tokens: value.max_output_tokens ?? 4096,
    safety_margin_tokens: value.safety_margin_tokens ?? 1024,
  };
}

export function contextPolicyError(policy: ContextPolicy): string | null {
  if (
    !Number.isFinite(policy.context_ratio) ||
    !Number.isFinite(policy.target_ratio) ||
    policy.target_ratio <= 0 ||
    policy.context_ratio > 1 ||
    policy.target_ratio >= policy.context_ratio
  )
    return "压缩比例需满足：0 < 目标比例 < 触发比例 ≤ 100%";
  for (const count of [policy.user_turns, policy.model_steps]) {
    if (count !== null && (!Number.isInteger(count) || count <= 0))
      return "轮次与步骤阈值请填写正整数，留空表示不启用";
  }
  if (
    !Number.isInteger(policy.max_compaction_calls) ||
    policy.max_compaction_calls < 1 ||
    policy.max_compaction_calls > 16
  )
    return "每次压缩最多调用模型次数需为 1 至 16 的整数";
  return null;
}

export function modelBudgetError(budget: ModelBudget): string | null {
  if (
    !Number.isInteger(budget.context_window) ||
    budget.context_window <= 0 ||
    !Number.isInteger(budget.max_output_tokens) ||
    budget.max_output_tokens <= 0 ||
    !Number.isInteger(budget.safety_margin_tokens) ||
    budget.safety_margin_tokens < 0
  )
    return "上下文窗口和最大输出需为正整数，安全边距需为非负整数";
  if (
    budget.max_output_tokens + budget.safety_margin_tokens >=
    budget.context_window
  )
    return "输出预留与安全边距之和必须小于模型上下文窗口";
  return null;
}
