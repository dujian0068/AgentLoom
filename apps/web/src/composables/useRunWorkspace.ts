import { ref, computed, type Ref } from "vue";
import { api, post } from "../api";
import type {
  Agent,
  RunState,
  RunResponse,
  RunEvent,
  Message,
} from "../domain/types";
import { statusNames } from "../domain/labels";
import { conversationFromRun } from "../domain/conversation";

type Options = {
  action: (fn: () => Promise<void>) => Promise<void>;
  notify: (message: string) => void;
  copy: (text: string) => Promise<void>;
  saving: Ref<boolean>;
  onOpen: () => void;
};

export function useRunWorkspace(options: Options) {
  const { action, notify, copy, saving } = options;
  const activeAgent = ref<Agent | null>(null);
  const published = ref<any>(null);
  const versions = ref<{ version: number; created: number }[]>([]);
  const chosenVersion = ref(0);
  const question = ref("");
  const conversation = ref<Message[]>([]);
  const session = ref<string | null>(null);
  const activeRun = ref<RunState | null>(null);
  const retryUnknownModels = ref(false);
  const running = ref(false);
  const events = ref<RunEvent[]>([]);
  const runTab = ref("chat");
  const savedRuns = ref<{ id: string; input: string; status: string }[]>([]);
  const selectedRun = ref("");
  const connectionNotice = ref("");
  let stream: EventSource | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let generation = 0;
  const currentPlan = computed(
    () =>
      [...events.value]
        .reverse()
        .find(
          (e) =>
            ["plan.created", "plan.updated"].includes(e.kind) &&
            e.instance === "main",
        )?.steps || [],
  );

  function stopStream() {
    generation += 1;
    stream?.close();
    stream = null;
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = null;
    connectionNotice.value = "";
  }

  async function loadRuns() {
    if (activeAgent.value?.id)
      savedRuns.value = await api(
        `/v1/agents/${activeAgent.value.id}/runs?version=${chosenVersion.value}`,
      );
  }

  async function loadVersion() {
    if (!activeAgent.value?.id) return;
    stopStream();
    published.value = await api(
      `/agents/${activeAgent.value.id}/versions/${chosenVersion.value}`,
    );
    conversation.value = [];
    session.value = null;
    activeRun.value = null;
    retryUnknownModels.value = false;
    events.value = [];
    selectedRun.value = "";
    runTab.value = "chat";
    await loadRuns();
  }

  async function openUse(agent: Agent) {
    if (running.value) return;
    activeAgent.value = agent;
    versions.value = await api("/agents/" + agent.id + "/versions");
    chosenVersion.value = agent.published || versions.value[0]?.version;
    await loadVersion();
    options.onOpen();
  }

  async function finishRun(id: string) {
    const row = await api<RunState>("/v1/runs/" + id);
    if (activeRun.value?.id !== id) return;
    activeRun.value = row;
    retryUnknownModels.value = false;
    events.value = row.events;
    running.value = ["queued", "running"].includes(row.status);
    if (!running.value) {
      const bubble = [...conversation.value]
        .reverse()
        .find((x) => x.run_id === id);
      if (bubble) {
        bubble.text = row.output || row.error || "任务已取消";
        bubble.artifacts = row.artifacts;
        bubble.citations =
          [...row.events]
            .reverse()
            .find((e) => ["run.completed", "run.blocked"].includes(e.kind))
            ?.citations || [];
      }
      stopStream();
      await loadRuns();
    }
  }

  function watchRun(id: string, after = 0, retry = 0) {
    stopStream();
    const current = generation;
    stream = new EventSource(`/api/v1/runs/${id}/events?after=${after}`);
    stream.onmessage = async (message) => {
      if (current !== generation || activeRun.value?.id !== id) return;
      try {
        const event: RunEvent = JSON.parse(message.data);
        if (events.value.some((x) => x.seq === event.seq)) return;
        events.value.push(event);
        connectionNotice.value = "";
        if (event.kind === "run.started") activeRun.value.status = "running";
        if (
          [
            "run.completed",
            "run.blocked",
            "run.failed",
            "run.cancelled",
          ].includes(event.kind)
        )
          await finishRun(id);
      } catch (error: any) {
        notify(error.message);
      }
    };
    stream.onerror = async () => {
      if (current !== generation) return;
      stream?.close();
      try {
        await finishRun(id);
        if (!running.value || current !== generation) return;
      } catch {
        if (current !== generation) return;
      }
      if (retry >= 5) {
        connectionNotice.value = "执行记录连接暂时中断，可刷新状态或停止任务。";
        return;
      }
      connectionNotice.value = "正在重新连接执行记录，任务仍在后台运行…";
      retryTimer = setTimeout(
        () => {
          if (current === generation)
            watchRun(id, events.value.at(-1)?.seq || after, retry + 1);
        },
        Math.min(1000 * 2 ** retry, 10000),
      );
    };
  }

  async function run() {
    const text = question.value.trim();
    if (!text || running.value) return;
    await action(async () => {
      const result = (await post(`/v1/agents/${activeAgent.value!.id}/runs`, {
        input: text,
        version: chosenVersion.value,
        session_id: session.value,
        stream: false,
      })) as RunResponse;
      session.value = result.session_id;
      activeRun.value = {
        id: result.run_id,
        status: "queued",
        input: text,
        session_id: result.session_id,
        output: "",
        error: null,
        resumable: false,
        requires_model_retry: false,
        events: [],
        artifacts: [],
      };
      selectedRun.value = result.run_id;
      retryUnknownModels.value = false;
      question.value = "";
      running.value = true;
      conversation.value.push(
        { role: "user", text },
        { role: "assistant", text: "", run_id: result.run_id },
      );
      events.value = [];
      watchRun(result.run_id);
      await loadRuns();
    });
  }

  async function resumeRun() {
    if (!activeRun.value?.resumable || running.value) return;
    if (activeRun.value.requires_model_retry && !retryUnknownModels.value) {
      notify("请核对模型请求状态后，明确勾选是否允许重试未确认请求。");
      return;
    }
    await action(async () => {
      const id = activeRun.value!.id;
      const text = question.value.trim();
      const result = (await post(`/v1/runs/${id}/resume`, {
        input: text,
        stream: false,
        retry_unknown_models:
          activeRun.value!.requires_model_retry && retryUnknownModels.value,
      })) as RunResponse;
      question.value = "";
      retryUnknownModels.value = false;
      running.value = true;
      activeRun.value = {
        ...activeRun.value!,
        status: "queued",
        resumable: false,
        requires_model_retry: false,
      };
      conversation.value.push(
        { role: "user", text: text || "继续任务" },
        { role: "assistant", text: "", run_id: id },
      );
      watchRun(id, result.after);
      await loadRuns();
    });
  }

  async function restoreRun() {
    if (!selectedRun.value || running.value) return;
    await action(async () => {
      const row = await api<RunState>("/v1/runs/" + selectedRun.value);
      activeRun.value = row;
      retryUnknownModels.value = false;
      session.value = row.session_id;
      conversation.value = conversationFromRun(row);
      events.value = row.events;
      running.value = ["queued", "running"].includes(row.status);
      if (running.value) watchRun(row.id, row.events.at(-1)?.seq || 0);
    });
  }

  async function cancelRun() {
    if (activeRun.value)
      await action(async () => {
        await post("/v1/runs/" + activeRun.value!.id + "/cancel");
      });
  }

  async function refreshRun() {
    if (activeRun.value)
      await action(async () => {
        const id = activeRun.value!.id;
        await finishRun(id);
        if (running.value) watchRun(id, events.value.at(-1)?.seq || 0);
      });
  }

  function newSession() {
    stopStream();
    session.value = null;
    conversation.value = [];
    events.value = [];
    activeRun.value = null;
    retryUnknownModels.value = false;
    selectedRun.value = "";
  }

  const apiExample = computed(
    () =>
      `curl -X POST "${location.origin}/api/v1/agents/${activeAgent.value?.id}/runs" \\\n  -H "Authorization: Bearer $AGENT_LOOM_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '{"input":"整理系统架构","version":${chosenVersion.value},"stream":true}'`,
  );

  function eventDetail(event: RunEvent) {
    if (["plan.created", "plan.updated"].includes(event.kind))
      return [
        event.explanation,
        ...event.steps.map(
          (step: any) =>
            `${statusNames[step.status] || step.status} · ${step.step}`,
        ),
      ]
        .filter(Boolean)
        .join("\n");
    if (event.kind === "context.compacted")
      return `${event.before_chars} → ${event.after_chars} 字符`;
    if (event.kind === "completion.checked")
      return [event.decision, event.reason, event.next_action]
        .filter(Boolean)
        .join(" · ");
    if (event.kind === "tool.completed")
      return JSON.stringify(event.result, null, 2);
    return [
      event.name,
      event.task,
      event.error,
      event.output,
      event.query,
      event.model,
      event.purpose,
      event.input,
      event.index !== undefined ? "第 " + (event.index + 1) + " 步" : "",
    ]
      .filter(Boolean)
      .join(" · ");
  }

  return {
    activeAgent,
    published,
    versions,
    chosenVersion,
    question,
    conversation,
    session,
    activeRun,
    retryUnknownModels,
    running,
    events,
    runTab,
    savedRuns,
    selectedRun,
    currentPlan,
    connectionNotice,
    loadVersion,
    openUse,
    run,
    resumeRun,
    restoreRun,
    cancelRun,
    refreshRun,
    newSession,
    stopStream,
    apiExample,
    eventDetail,
    action,
    copy,
    saving,
  };
}
