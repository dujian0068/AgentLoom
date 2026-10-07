import { modelBudget } from "./contextPolicy";
import type { DiscoveredModel, ModelMetadata } from "./types";

export function defaultModelMetadata(): ModelMetadata {
  return {
    source: "platform_default",
    source_url: "",
    verified_at: "",
    provider_max_output_tokens: null,
    chat_compatible: null,
  };
}

export function invalidateModelSelection(form: Record<string, any>) {
  form.model_id = "";
  Object.assign(form, modelBudget(), { metadata: defaultModelMetadata() });
}

export function applyModelSelection(
  form: Record<string, any>,
  model: DiscoveredModel,
) {
  if (!form.name || form.name === form.model_id) form.name = model.name;
  Object.assign(form, modelBudget(model), {
    model_id: model.id,
    metadata: { ...model.metadata },
  });
}

export function modelSelectionError(form: Record<string, any>): string | null {
  if (!form.model_id?.trim())
    return "请先获取并选择模型，或在高级设置中手动填写";
  if (form.purpose === "chat" && form.metadata?.chat_compatible === false)
    return "该模型不支持当前 Agent 使用的聊天接口，请选择其他模型";
  if (
    form.metadata?.provider_max_output_tokens &&
    form.max_output_tokens > form.metadata.provider_max_output_tokens
  )
    return "最大输出不能超过已识别的供应商输出上限";
  return null;
}
