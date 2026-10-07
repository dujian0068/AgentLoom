import { describe, expect, it } from "vitest";
import {
  contextPolicyError,
  defaultContextPolicy,
  modelBudget,
  modelBudgetError,
  prepareAgentDraft,
} from "../src/domain/contextPolicy";
import {
  applyModelSelection,
  defaultModelMetadata,
  invalidateModelSelection,
  modelSelectionError,
} from "../src/domain/modelCatalog";
import type { Agent, DiscoveredModel } from "../src/domain/types";

describe("context configuration", () => {
  it("opts a legacy draft in without changing the published object", () => {
    const original: Agent = {
      name: "old",
      model: "model",
      prompt: "",
      mode: "react",
      skills: [],
      tools: [],
      wiki: [],
      subs: [],
      published: 1,
    };
    const draft = prepareAgentDraft(original);
    expect(draft.context_policy.context_ratio).toBe(0.8);
    draft.skills.push("new");
    draft.context_policy.user_turns = 10;
    expect(original.context_policy).toBeUndefined();
    expect(original.skills).toEqual([]);
    expect(prepareAgentDraft(original).context_policy.user_turns).toBeNull();
  });

  it("validates OR thresholds and output reservations", () => {
    expect(contextPolicyError(defaultContextPolicy())).toBeNull();
    expect(
      contextPolicyError({ ...defaultContextPolicy(), target_ratio: 0.8 }),
    ).toBeTruthy();
    expect(
      contextPolicyError({ ...defaultContextPolicy(), model_steps: 1.5 }),
    ).toBeTruthy();
    expect(
      contextPolicyError({ ...defaultContextPolicy(), user_turns: 0 }),
    ).toBeTruthy();
    expect(
      contextPolicyError({ ...defaultContextPolicy(), user_turns: null }),
    ).toBeNull();
    expect(modelBudgetError(modelBudget())).toBeNull();
    expect(
      modelBudgetError({ ...modelBudget(), context_window: 5120 }),
    ).toBeTruthy();
  });
});

describe("model discovery selection", () => {
  const known: DiscoveredModel = {
    id: "known",
    name: "Known",
    purpose: "chat",
    context_window: 128000,
    max_output_tokens: 4096,
    safety_margin_tokens: 1024,
    metadata: {
      ...defaultModelMetadata(),
      source: "provider",
      provider_max_output_tokens: 16000,
      chat_compatible: true,
    },
  };

  it("applies metadata and clears it when the provider or key changes", () => {
    const form: Record<string, any> = { name: "", purpose: "chat" };
    applyModelSelection(form, known);
    expect(form.name).toBe("Known");
    expect(form.context_window).toBe(128000);
    form.metadata.source = "manual";
    expect(known.metadata.source).toBe("provider");
    invalidateModelSelection(form);
    expect(form.model_id).toBe("");
    expect(form.context_window).toBe(32768);
    expect(form.metadata.source).toBe("platform_default");
    expect(modelSelectionError(form)).toBeTruthy();
  });

  it("does not retain a previous model's larger window on fallback", () => {
    const form: Record<string, any> = { name: "custom name", purpose: "chat" };
    applyModelSelection(form, known);
    applyModelSelection(form, {
      ...known,
      id: "unknown",
      ...modelBudget(),
      metadata: defaultModelMetadata(),
    });
    expect(form.context_window).toBe(32768);
    expect(form.name).toBe("custom name");
  });

  it("rejects unsupported protocols and configured output beyond provider caps", () => {
    const form: Record<string, any> = { purpose: "chat" };
    applyModelSelection(form, known);
    expect(modelSelectionError(form)).toBeNull();
    form.max_output_tokens = 20000;
    expect(modelSelectionError(form)).toBeTruthy();
    form.max_output_tokens = 4096;
    form.metadata.chat_compatible = false;
    expect(modelSelectionError(form)).toBeTruthy();
  });
});
