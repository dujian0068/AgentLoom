import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ref } from "vue";
import { conversationFromRun } from "../src/domain/conversation";
import { useRunWorkspace } from "../src/composables/useRunWorkspace";
import type { RunState } from "../src/domain/types";
import { api, post } from "../src/api";
vi.mock("../src/api", () => ({ api: vi.fn(), post: vi.fn() }));
const apiMock = vi.mocked(api),
  postMock = vi.mocked(post);
const sources: FakeSource[] = [];
class FakeSource {
  onmessage: ((event: { data: string }) => Promise<void>) | null = null;
  onerror: (() => Promise<void>) | null = null;
  closed = false;
  constructor(public url: string) {
    sources.push(this);
  }
  close() {
    this.closed = true;
  }
}
function row(overrides: Partial<RunState> = {}): RunState {
  return {
    id: "run",
    input: "生成报告",
    status: "running",
    output: "",
    error: null,
    session_id: "session",
    resumable: false,
    events: [],
    artifacts: [],
    ...overrides,
  };
}
function workspace() {
  return useRunWorkspace({
    action: async (fn) => {
      await fn();
    },
    notify: vi.fn(),
    copy: async () => {},
    saving: ref(false),
    onOpen: () => {},
  });
}
function setAgent(w: ReturnType<typeof workspace>) {
  w.activeAgent.value = {
    id: "agent",
    name: "test",
    model: "model",
    prompt: "",
    mode: "plan",
    skills: [],
    tools: [],
    wiki: [],
    subs: [],
  };
}
beforeEach(() => {
  sources.length = 0;
  vi.clearAllMocks();
  vi.stubGlobal("EventSource", FakeSource);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("run conversation", () => {
  it("preserves blocked answers and user supplements when reloaded", () => {
    const history = row({
      status: "succeeded",
      output: "已完成",
      events: [
        { seq: 1, kind: "run.blocked", output: "需要项目名" },
        { seq: 2, kind: "run.resumed", input: "项目叫织点" },
        { seq: 3, kind: "run.completed", output: "已完成" },
      ],
      artifacts: ["report.md"],
    });
    const messages = conversationFromRun(history);
    expect(messages.map((x) => x.text)).toEqual([
      "生成报告",
      "需要项目名",
      "项目叫织点",
      "已完成",
    ]);
    expect(messages.at(-1)?.artifacts).toEqual(["report.md"]);
  });
  it("restores an active task with a pending assistant bubble", () => {
    const messages = conversationFromRun(
      row({ events: [{ seq: 1, kind: "run.resumed", input: "" }] }),
    );
    expect(messages.at(-1)).toMatchObject({
      role: "assistant",
      text: "",
      run_id: "run",
    });
    expect(messages[1].text).toBe("继续任务");
  });
});

describe("execution stream", () => {
  it("reconnects after the latest persisted event rather than replaying the whole stream", async () => {
    vi.useFakeTimers();
    apiMock.mockImplementation(async (path) =>
      path === "/v1/runs/run"
        ? row({ events: [{ seq: 7, kind: "tool.completed" }] })
        : [],
    );
    postMock.mockResolvedValue({
      run_id: "run",
      session_id: "session",
      version: 1,
    });
    const w = workspace();
    setAgent(w);
    w.question.value = "生成报告";
    await w.run();
    await sources[0].onerror?.();
    expect(w.connectionNotice.value).toContain("重新连接");
    await vi.advanceTimersByTimeAsync(1000);
    expect(sources[1].url).toContain("after=7");
    expect(sources[0].closed).toBe(true);
    w.stopStream();
  });
  it("drops duplicate and stale stream events", async () => {
    apiMock.mockResolvedValue([]);
    postMock.mockResolvedValue({
      run_id: "run",
      session_id: "session",
      version: 1,
    });
    const w = workspace();
    setAgent(w);
    w.question.value = "task";
    await w.run();
    const old = sources[0];
    const event = { data: JSON.stringify({ seq: 1, kind: "model.started" }) };
    await old.onmessage?.(event);
    await old.onmessage?.(event);
    expect(w.events.value).toHaveLength(1);
    w.stopStream();
    await old.onmessage?.({
      data: JSON.stringify({ seq: 2, kind: "tool.started" }),
    });
    expect(w.events.value).toHaveLength(1);
  });
  it("stops reconnecting when a terminal state is discovered", async () => {
    vi.useFakeTimers();
    apiMock.mockImplementation(async (path) =>
      path === "/v1/runs/run"
        ? row({
            status: "failed",
            error: "provider unavailable",
            resumable: true,
          })
        : [],
    );
    postMock.mockResolvedValue({
      run_id: "run",
      session_id: "session",
      version: 1,
    });
    const w = workspace();
    setAgent(w);
    w.question.value = "task";
    await w.run();
    await sources[0].onerror?.();
    await vi.advanceTimersByTimeAsync(15000);
    expect(sources).toHaveLength(1);
    expect(w.running.value).toBe(false);
    expect(w.conversation.value.at(-1)?.text).toBe("provider unavailable");
    expect(w.activeRun.value?.resumable).toBe(true);
  });
});
