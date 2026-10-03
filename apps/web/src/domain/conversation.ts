import type { Message, RunState } from "./types";

/** Rebuild every user supplement and terminal answer, preserving resumed turns. */
export function conversationFromRun(row: RunState): Message[] {
  const messages: Message[] = [{ role: "user", text: row.input }];
  for (const event of row.events) {
    if (event.kind === "run.resumed") {
      messages.push({ role: "user", text: event.input || "继续任务" });
    } else if (
      ["run.completed", "run.blocked", "run.failed", "run.cancelled"].includes(
        event.kind,
      )
    ) {
      messages.push({
        role: "assistant",
        text: event.output || event.error || "任务已取消",
        run_id: row.id,
        citations: event.citations || [],
      });
    }
  }
  if (["queued", "running"].includes(row.status)) {
    messages.push({ role: "assistant", text: "", run_id: row.id });
  } else if (messages.at(-1)?.role !== "assistant") {
    messages.push({
      role: "assistant",
      text: row.output || row.error || "任务已取消",
      run_id: row.id,
    });
  }
  const last = messages.at(-1);
  if (last?.role === "assistant") last.artifacts = row.artifacts;
  return messages;
}
