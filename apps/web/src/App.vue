<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from "vue";
import {
  Layers,
  Bot,
  BookOpen,
  Blocks,
  Cable,
  Settings2,
  Users,
  Plus,
  Upload,
  X,
  Terminal,
  KeyRound,
  LogOut,
  Search,
} from "lucide-vue-next";
import { api, post, put } from "./api";
import type {
  Agent,
  AgentDraft,
  Child,
  DiscoveredModel,
  Resource,
} from "./domain/types";
import {
  contextPolicyError,
  defaultContextPolicy,
  modelBudget,
  modelBudgetError,
  prepareAgentDraft,
} from "./domain/contextPolicy";
import {
  applyModelSelection,
  defaultModelMetadata,
  invalidateModelSelection,
  modelSelectionError,
} from "./domain/modelCatalog";
import { titles, descriptions, statusNames } from "./domain/labels";
import { useRunWorkspace } from "./composables/useRunWorkspace";
import PublishedAgentView from "./components/PublishedAgentView.vue";
import AgentEditor from "./components/AgentEditor.vue";
const me = ref<any>(null),
  loading = ref(true),
  setup = ref(false),
  authMode = ref("login"),
  authForm = ref({ email: "", password: "", name: "", invite: "" }),
  authError = ref("");
const page = ref("agents"),
  agents = ref<Agent[]>([]),
  resources = ref<Record<string, Resource[]>>({
    models: [],
    skills: [],
    tools: [],
    wiki: [],
  }),
  filter = ref(""),
  draft = ref<AgentDraft | null>(null);
const modal = ref(""),
  form = ref<any>({}),
  files = ref<File[]>([]),
  saving = ref(false),
  error = ref(""),
  toast = ref(""),
  timer = ref<any>(null);
const modelCatalog = ref<DiscoveredModel[]>([]);
let catalogRevision = 0;
const modelChoices = computed(() =>
  modelCatalog.value.filter((model) => model.purpose === form.value.purpose),
);
const members = ref<any[]>([]),
  invite = ref(""),
  keys = ref<any[]>([]),
  keyResult = ref(""),
  resourceDetail = ref<any>(null),
  docs = ref<any[]>([]),
  searchQuery = ref(""),
  searchResults = ref<any[]>([]);
const navigation = [
  { id: "agents", name: "Agents", icon: Bot },
  { id: "skills", name: "Skills", icon: Blocks },
  { id: "tools", name: "工具", icon: Cable },
  { id: "models", name: "模型连接", icon: Settings2 },
  { id: "wiki", name: "知识库", icon: BookOpen },
  { id: "members", name: "空间成员", icon: Users },
];
const visibleAgents = computed(() =>
  agents.value.filter((x) =>
    x.name.toLowerCase().includes(filter.value.toLowerCase()),
  ),
);
const chats = computed(() =>
  resources.value.models.filter((x) => x.purpose === "chat"),
);
const embeddings = computed(() =>
  resources.value.models.filter((x) => x.purpose === "embedding"),
);
const workspace = useRunWorkspace({
  action,
  notify,
  copy,
  saving,
  onOpen: () => {
    page.value = "use";
  },
});
const { published, running, conversation, openUse, stopStream } = workspace;
function notify(message: string) {
  toast.value = message;
  clearTimeout(timer.value);
  timer.value = setTimeout(() => (toast.value = ""), 3500);
}
async function action(fn: () => Promise<void>) {
  if (saving.value) return;
  saving.value = true;
  error.value = "";
  try {
    await fn();
  } catch (e: any) {
    error.value = e.message;
    notify(e.message);
  } finally {
    saving.value = false;
  }
}
function emptyAgent(): AgentDraft {
  return {
    name: "",
    model: chats.value[0]?.id || "",
    prompt: "",
    mode: "react",
    context_policy: defaultContextPolicy(),
    skills: [],
    tools: [],
    wiki: [],
    subs: [],
  };
}
async function refresh() {
  const result = await Promise.all([
    api("/agents"),
    ...["models", "skills", "tools", "wiki"].map((k) => api("/resources/" + k)),
  ]);
  agents.value = result[0];
  ["models", "skills", "tools", "wiki"].forEach(
    (k, i) => (resources.value[k] = result[i + 1]),
  );
}
async function init() {
  loading.value = true;
  try {
    setup.value = (await api("/auth/status")).needs_setup;
    authMode.value = setup.value ? "setup" : "login";
    try {
      me.value = await api("/me");
      await refresh();
    } catch {
      me.value = null;
    }
  } finally {
    loading.value = false;
  }
}
async function authenticate() {
  await action(async () => {
    authError.value = "";
    try {
      await post("/auth/" + authMode.value, authForm.value);
      authForm.value.password = "";
      await init();
    } catch (e: any) {
      authError.value = e.message;
    }
  });
}
async function logout() {
  await action(async () => {
    await post("/auth/logout");
    stopStream();
    me.value = null;
    draft.value = null;
    conversation.value = [];
    page.value = "agents";
    await init();
  });
}
async function navigate(p: string) {
  if (running.value) {
    notify("请先停止当前运行");
    return;
  }
  page.value = p;
  filter.value = "";
  draft.value = null;
  await action(async () => {
    await refresh();
    if (p === "members") {
      members.value = await api("/members");
      if (me.value.role === "owner") {
        invite.value = (await api("/invite")).invite;
        keys.value = await api("/api-keys");
      }
    }
  });
}
function edit(a: Agent) {
  draft.value = prepareAgentDraft(a);
  page.value = "editor";
}
function create() {
  draft.value = emptyAgent();
  page.value = "editor";
}
function config() {
  if (!draft.value) throw new Error("没有配置");
  const { id, published, ...data } = draft.value;
  return data;
}
async function saveDraft() {
  if (!draft.value?.name.trim()) throw new Error("请填写 Agent 名称");
  const policyError = contextPolicyError(draft.value.context_policy);
  if (policyError) throw new Error(policyError);
  const result = draft.value.id
    ? await put("/agents/" + draft.value.id, config())
    : await post("/agents", config());
  draft.value = prepareAgentDraft(result);
  await refresh();
  return result;
}
async function saveAgent() {
  await action(async () => {
    await saveDraft();
    notify("草稿已保存");
  });
}
async function publishAgent() {
  await action(async () => {
    const a = await saveDraft();
    const result = await post("/agents/" + a.id + "/publish");
    notify("已发布 v" + result.version);
    modal.value = "";
    await openUse({ ...a, published: result.version });
  });
}
function openModal(kind: string, value: any = {}) {
  modal.value = kind;
  form.value = JSON.parse(JSON.stringify(value));
  modelCatalog.value = [];
  catalogRevision++;
  if (kind === "model")
    Object.assign(form.value, modelBudget(value), {
      metadata: value.metadata || defaultModelMetadata(),
    });
  files.value = [];
  error.value = "";
  keyResult.value = "";
}
function closeModal() {
  if (saving.value) return;
  modal.value = "";
  form.value = {};
  modelCatalog.value = [];
  catalogRevision++;
  files.value = [];
  error.value = "";
  keyResult.value = "";
}
function resetModelDiscovery() {
  modelCatalog.value = [];
  catalogRevision++;
  invalidateModelSelection(form.value);
}
function changeModelProvider() {
  form.value.base_url =
    form.value.provider === "openai"
      ? "https://api.openai.com/v1"
      : "https://api.deepseek.com";
  form.value.api_key = "";
  resetModelDiscovery();
}
function selectDiscoveredModel(event: Event) {
  const selected = modelCatalog.value.find(
    (model) => model.id === (event.target as HTMLSelectElement).value,
  );
  if (selected) applyModelSelection(form.value, selected);
}
function manualModelMetadata() {
  Object.assign(form.value, modelBudget());
  form.value.metadata = { ...defaultModelMetadata(), source: "manual" };
}
function manualBudgetMetadata() {
  form.value.metadata = {
    ...form.value.metadata,
    source: "manual",
    source_url: "",
    verified_at: "",
  };
}
const metadataSourceLabels: Record<string, string> = {
  provider: "参数来自服务商模型接口",
  official_manifest: "参数来自已核对的官方模型文档",
  platform_default: "未取得窗口元数据，暂用平台 32K 默认值，请在高级设置核对",
  manual: "参数由管理员手动设置",
};
async function discoverModels() {
  await action(async () => {
    const revision = catalogRevision;
    const result = await post("/models/discover", {
      provider: form.value.provider,
      base_url: form.value.base_url,
      api_key: form.value.api_key || "",
      resource_id: form.value.id || null,
    });
    if (revision !== catalogRevision || modal.value !== "model") return;
    modelCatalog.value = result.models;
    const selected = modelChoices.value.find(
      (model) => model.id === form.value.model_id,
    );
    if (selected) applyModelSelection(form.value, selected);
    else if (modelChoices.value.length === 1)
      applyModelSelection(form.value, modelChoices.value[0]);
    notify("已获取 " + result.models.length + " 个模型");
  });
}
function picked(e: Event) {
  files.value = Array.from((e.target as HTMLInputElement).files || []);
}
function uploadData() {
  const data = new FormData();
  for (const f of files.value)
    data.append("files", f, f.webkitRelativePath || f.name);
  return data;
}
function addResource() {
  if (page.value === "models")
    openModal("model", {
      name: "",
      provider: "deepseek",
      model_id: "",
      base_url: "https://api.deepseek.com",
      api_key: "",
      purpose: "chat",
    });
  if (page.value === "skills")
    openModal("skill", { source: "upload", url: "", subdir: "", ref: "" });
  if (page.value === "tools")
    openModal("tool", {
      source: "external",
      name: "",
      endpoint: "",
      transport: "streamable-http",
      api_key: "",
      url: "",
      subdir: "",
      ref: "",
    });
  if (page.value === "wiki") openModal("kb", { name: "", embedding_id: "" });
}
async function submitModal() {
  await action(async () => {
    if (modal.value === "model") {
      const selectionError = modelSelectionError(form.value);
      if (selectionError) throw new Error(selectionError);
      const budget = modelBudget(form.value);
      const budgetError = modelBudgetError(budget);
      if (budgetError) throw new Error(budgetError);
      const data = {
        name: form.value.name,
        provider: form.value.provider,
        model_id: form.value.model_id,
        base_url: form.value.base_url,
        api_key: form.value.api_key || "",
        purpose: form.value.purpose,
        ...budget,
        metadata: form.value.metadata,
      };
      if (form.value.id) await put("/models/" + form.value.id, data);
      else await post("/models", data);
      form.value.api_key = "";
    } else if (modal.value === "skill") {
      if (form.value.source === "git")
        await post("/skills/git", {
          url: form.value.url,
          subdir: form.value.subdir,
          ref: form.value.ref,
        });
      else {
        if (!files.value.length) throw new Error("请选择技能目录");
        await api("/skills/upload", { method: "POST", body: uploadData() });
      }
    } else if (modal.value === "tool") {
      if (form.value.source === "external")
        await post("/tools/external", {
          name: form.value.name,
          endpoint: form.value.endpoint,
          transport: form.value.transport,
          api_key: form.value.api_key,
        });
      else if (form.value.source === "git")
        await post("/tools/git", {
          url: form.value.url,
          subdir: form.value.subdir,
          ref: form.value.ref,
          name: form.value.name,
        });
      else {
        if (!files.value.length) throw new Error("请选择工具目录");
        const data = uploadData();
        data.append("name", form.value.name);
        await api("/tools/upload", { method: "POST", body: data });
      }
    } else if (modal.value === "kb") await post("/wiki", form.value);
    else if (modal.value === "upload") {
      if (!files.value.length) throw new Error("请选择文件");
      const result = await api("/wiki/" + form.value.id + "/upload", {
        method: "POST",
        body: uploadData(),
      });
      const failed = result.filter((x: any) => x.status === "failed");
      await refresh();
      if (failed.length) {
        throw new Error(
          failed.map((x: any) => x.name + ": " + x.error).join("；"),
        );
      }
    } else if (modal.value === "child") {
      if (!form.value.name?.trim() || !form.value.description?.trim())
        throw new Error("请填写名称与职责");
      const child = { ...form.value, id: form.value.id || crypto.randomUUID() };
      const index = draft.value!.subs.findIndex((x) => x.id === child.id);
      if (index < 0) draft.value!.subs.push(child);
      else draft.value!.subs[index] = child;
    } else if (modal.value === "tooltest") {
      let argumentsValue;
      try {
        argumentsValue = JSON.parse(form.value.arguments);
      } catch {
        throw new Error("工具参数必须是 JSON 对象");
      }
      form.value.result = await post("/tools/" + form.value.id + "/test", {
        name: form.value.tool,
        arguments: argumentsValue,
      });
      return;
    } else if (modal.value === "apikey") {
      keyResult.value = (
        await post("/api-keys", { name: form.value.name })
      ).key;
      keys.value = await api("/api-keys");
      return;
    }
    await refresh();
    modal.value = "";
    form.value = {};
    files.value = [];
    notify("已保存");
  });
}
function childForm(child?: Child) {
  openModal(
    "child",
    child || {
      name: "",
      description: "",
      prompt: "",
      skills: [],
      tools: [],
      wiki: [],
    },
  );
}
function removeChild(id: string) {
  draft.value!.subs = draft.value!.subs.filter((x) => x.id !== id);
}
async function resourceAction(kind: string, r: Resource) {
  await action(async () => {
    if (kind === "modeltest") {
      await post("/models/" + r.id + "/test");
      notify("模型连接成功");
    }
    if (kind === "refresh") {
      await post("/tools/" + r.id + "/refresh");
      await refresh();
      notify("工具目录已刷新");
    }
    if (kind === "build") {
      await post("/tools/" + r.id + "/build");
      await refresh();
      notify("正在构建工具");
    }
    if (kind === "remove") {
      await api("/resources/" + r.id, { method: "DELETE" });
      modal.value = "";
      await refresh();
      notify("资源已移除");
    }
  });
}
async function details(r: Resource) {
  resourceDetail.value = r;
  searchResults.value = [];
  searchQuery.value = "";
  if (page.value === "wiki")
    docs.value = await api("/wiki/" + r.id + "/documents");
  openModal("detail");
}
async function searchKB() {
  await action(async () => {
    searchResults.value = await post(
      "/wiki/" + resourceDetail.value.id + "/search",
      { query: searchQuery.value },
    );
  });
}
async function showDocument(id: string) {
  await action(async () => {
    const doc = await api("/documents/" + id);
    openModal("document", doc);
  });
}
async function deleteDoc(id: string) {
  await action(async () => {
    await api("/documents/" + id, { method: "DELETE" });
    docs.value = await api("/wiki/" + resourceDetail.value.id + "/documents");
    searchResults.value = [];
    notify("文档已移除");
  });
}
function openToolTest(r: Resource) {
  openModal("tooltest", {
    id: r.id,
    tool: r.schemas?.[0]?.name || "",
    schemas: r.schemas || [],
    arguments: "{}",
    result: null,
  });
}
async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    notify("已复制");
  } catch {
    notify("请选择文本复制");
  }
}
function resourceName(kind: string, id: string) {
  return resources.value[kind].find((x) => x.id === id)?.name || id;
}
onMounted(init);
onUnmounted(() => {
  stopStream();
  clearTimeout(timer.value);
});
</script>

<template>
  <div v-if="loading" class="loading">
    <Layers :size="32" />
    <p>正在加载织点工作台…</p>
  </div>
  <div v-else-if="!me" class="authscreen">
    <div class="authbrand">
      <div class="brandmark">织</div>
      <h1>织点 <span>AgentLoom</span></h1>
      <p>团队的 Agent 开发工作台</p>
    </div>
    <form class="authcard" @submit.prevent="authenticate">
      <h2>
        {{
          authMode === "setup"
            ? "创建你的工作空间"
            : authMode === "register"
              ? "加入团队空间"
              : "登录工作台"
        }}
      </h2>
      <p class="muted">
        {{
          authMode === "setup"
            ? "设置管理员账号，开始配置第一个 Agent。"
            : "使用账号访问团队的 Agent 和资源。"
        }}
      </p>
      <label v-if="authMode !== 'login'" for="username">姓名</label
      ><input
        v-if="authMode !== 'login'"
        id="username"
        v-model="authForm.name"
        required
        autocomplete="name"
      /><label for="email">邮箱</label
      ><input
        id="email"
        v-model="authForm.email"
        type="email"
        required
        autocomplete="username"
      /><label for="password">密码</label
      ><input
        id="password"
        v-model="authForm.password"
        type="password"
        required
        minlength="8"
        :autocomplete="
          authMode === 'login' ? 'current-password' : 'new-password'
        "
        placeholder="至少 8 位"
      /><template v-if="authMode === 'register'"
        ><label for="invite">空间邀请码</label
        ><input id="invite" v-model="authForm.invite" required
      /></template>
      <p v-if="authError" class="error">{{ authError }}</p>
      <button class="primary full" :disabled="saving">
        {{
          saving
            ? "正在处理…"
            : authMode === "setup"
              ? "创建空间"
              : authMode === "register"
                ? "加入空间"
                : "登录"
        }}</button
      ><button
        v-if="!setup"
        type="button"
        class="textbutton full"
        @click="
          authMode = authMode === 'login' ? 'register' : 'login';
          authError = '';
        "
      >
        {{ authMode === "login" ? "通过邀请码加入空间" : "返回登录" }}
      </button>
    </form>
  </div>
  <template v-else>
    <aside>
      <div class="brand">
        <div class="brandmark">织</div>
        <div>织点<span class="studio">AGENTLOOM</span></div>
      </div>
      <div class="space"><Layers :size="17" />{{ me.space.name }}</div>
      <div class="navlabel">工作台</div>
      <nav>
        <button
          v-for="n in navigation"
          :key="n.id"
          :class="{
            active:
              page === n.id ||
              (n.id === 'agents' && ['editor', 'use'].includes(page)),
          }"
          @click="navigate(n.id)"
        >
          <component :is="n.icon" :size="19" />{{ n.name }}
        </button>
      </nav>
      <div class="sidebarfoot">
        <a href="/api/docs" target="_blank"><Terminal :size="16" />API 文档</a
        ><a
          href="https://github.com/dujian0068/AgentLoom/blob/master/docs/requirements-v0.2.md"
          target="_blank"
          rel="noopener noreferrer"
          ><BookOpen :size="16" />需求文档</a
        >
        <div class="user">
          <div class="avatar">{{ me.name.slice(0, 1) }}</div>
          <div>
            {{ me.name
            }}<small>{{
              me.role === "owner" ? "空间管理员" : "空间成员"
            }}</small>
          </div>
          <button class="iconbutton" aria-label="退出登录" @click="logout">
            <LogOut :size="17" />
          </button>
        </div>
      </div>
    </aside>
    <div class="shell">
      <header>
        <div>
          {{ me.space.name }} <span class="muted">/ {{ titles[page] }}</span>
        </div>
        <div class="headerend">
          <span class="tag versiontag">v0.2</span
          ><span class="muted">{{
            me.docker ? "容器环境可用" : "脚本 / 托管工具需 Docker"
          }}</span>
        </div>
      </header>
      <main>
        <template v-if="page === 'agents'"
          ><div class="pagehead">
            <div>
              <h1>Agents</h1>
              <p class="muted">{{ descriptions.agents }}</p>
            </div>
            <button class="primary" @click="create">
              <Plus :size="17" />创建 Agent
            </button>
          </div>
          <div class="toolbar">
            <div class="searchbox">
              <Search :size="17" /><input
                v-model="filter"
                placeholder="搜索 Agent 名称"
                aria-label="搜索 Agent"
              />
            </div>
            <span class="muted">{{ agents.length }} 个 Agent</span
            ><span class="tag">空间共享</span>
          </div>
          <div v-if="!agents.length" class="emptypanel">
            <div class="agenticon"><Bot /></div>
            <h2>创建第一个 Agent</h2>
            <p class="muted">先添加模型连接，再选择技能、工具和知识库。</p>
            <div class="actions">
              <button @click="navigate('models')">配置模型</button
              ><button class="primary" @click="create">创建 Agent</button>
            </div>
          </div>
          <div v-else class="cards">
            <article v-for="a in visibleAgents" :key="a.id" class="card">
              <div class="cardtop">
                <div class="agenticon"><Bot :size="25" /></div>
                <span class="tag" :class="{ green: a.published }">{{
                  a.published ? "已发布 v" + a.published : "草稿"
                }}</span>
              </div>
              <h3>{{ a.name }}</h3>
              <div class="tags">
                <span class="tag">{{
                  resourceName("models", a.model) || "未选模型"
                }}</span
                ><span class="tag purple">{{
                  a.mode === "plan" ? "Plan" : "ReAct"
                }}</span>
              </div>
              <div class="count">
                <span>{{ a.skills.length }} 技能</span
                ><span>{{ a.tools.length }} 工具</span
                ><span>{{ a.subs.length }} 子 Agent 配置</span>
              </div>
              <div class="cardfoot">
                <span class="tiny">团队共享</span>
                <div class="actions">
                  <button v-if="a.published" @click="action(() => openUse(a))">
                    使用</button
                  ><button @click="edit(a)">配置</button>
                </div>
              </div>
            </article>
          </div></template
        >
        <AgentEditor
          v-else-if="page === 'editor' && draft"
          :draft="draft"
          :resources="resources"
          :chats="chats"
          :saving="saving"
          @back="navigate('agents')"
          @save="saveAgent"
          @publish="openModal('publish')"
          @child="childForm"
          @remove-child="removeChild"
          @use="(a) => action(() => openUse(a))"
        />
        <PublishedAgentView
          v-else-if="page === 'use' && published"
          :workspace="workspace"
          @back="navigate('agents')"
          @edit="edit"
          @document="showDocument"
        />
        <template
          v-else-if="['skills', 'tools', 'models', 'wiki'].includes(page)"
          ><div class="pagehead">
            <div>
              <h1>{{ titles[page] }}</h1>
              <p class="muted">{{ descriptions[page] }}</p>
            </div>
            <div class="actions">
              <button @click="action(refresh)">刷新</button
              ><button
                class="primary"
                :disabled="page === 'models' && me.role !== 'owner'"
                @click="addResource"
              >
                <Plus :size="17" />{{
                  page === "skills"
                    ? "导入 Skill"
                    : page === "tools"
                      ? "添加工具"
                      : page === "models"
                        ? "添加模型"
                        : "创建知识库"
                }}
              </button>
            </div>
          </div>
          <div v-if="!resources[page].length" class="emptypanel">
            <component
              :is="navigation.find((n) => n.id === page)?.icon"
              :size="36"
            />
            <h2>还没有{{ titles[page] }}</h2>
            <p class="muted">添加资源后，可在 Agent 配置页面使用。</p>
          </div>
          <div v-else class="tablewrap">
            <table>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>
                    {{ page === "models" ? "供应商 / 模型" : "来源 / 类型" }}
                  </th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in resources[page]" :key="r.id">
                  <td>
                    <strong>{{ r.name }}</strong
                    ><small>{{
                      page === "skills"
                        ? r.description
                        : page === "models"
                          ? r.purpose === "embedding"
                            ? "向量化模型"
                            : "聊天模型"
                          : page === "wiki"
                            ? "索引版本 " + r.revision
                            : r.schemas?.length + " 个工具"
                    }}</small>
                  </td>
                  <td>
                    {{
                      page === "models"
                        ? r.provider + " / " + r.model_id
                        : page === "skills"
                          ? r.source === "git"
                            ? "Git 导入"
                            : "文件夹上传"
                          : page === "tools"
                            ? r.source === "hosted"
                              ? "平台托管"
                              : "外部 MCP"
                            : r.retrieval === "hybrid"
                              ? "关键词 + 向量"
                              : "关键词检索"
                    }}<small v-if="page === 'models'">{{ r.base_url }}</small>
                  </td>
                  <td>
                    <span
                      class="tag"
                      :class="{
                        green: r.status === 'ready',
                        red: r.status === 'failed',
                      }"
                      >{{ statusNames[r.status] || r.status }}</span
                    >
                  </td>
                  <td>
                    <div class="actions">
                      <button @click="action(() => details(r))">详情</button
                      ><template v-if="page === 'models' && me.role === 'owner'"
                        ><button
                          @click="openModal('model', { ...r, api_key: '' })"
                        >
                          编辑</button
                        ><button
                          :disabled="saving"
                          @click="resourceAction('modeltest', r)"
                        >
                          测试连接
                        </button></template
                      ><template v-if="page === 'tools'"
                        ><button
                          v-if="r.status === 'ready'"
                          @click="openToolTest(r)"
                        >
                          测试</button
                        ><button
                          v-else-if="r.source === 'hosted'"
                          :disabled="r.status === 'building' || saving"
                          @click="resourceAction('build', r)"
                        >
                          构建</button
                        ><button
                          v-else
                          :disabled="saving"
                          @click="resourceAction('refresh', r)"
                        >
                          重试连接
                        </button></template
                      ><button
                        v-if="page === 'wiki'"
                        @click="openModal('upload', r)"
                      >
                        <Upload :size="15" />上传
                      </button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="page === 'wiki'" class="notice spaced">
            不配置向量化模型时使用关键词检索；配置后上传文档建立混合索引。SQL
            文件作为资料，不执行 SQL。
          </div>
          <div v-if="page === 'skills'" class="notice spaced">
            导入保留完整目录。运行时按需加载；专属宿主工具与依赖需要适配，不能仅凭格式保证全部技能兼容。
          </div>
          <div v-if="page === 'tools' && !me.docker" class="notice spaced">
            外部 MCP 可直接连接；平台托管代码会保存，Docker
            就绪后才可构建和执行。
          </div></template
        >
        <template v-else-if="page === 'members'"
          ><div class="pagehead">
            <div>
              <h1>空间成员</h1>
              <p class="muted">{{ descriptions.members }}</p>
            </div>
          </div>
          <div v-if="me.role === 'owner'" class="panel spaced">
            <h2>邀请成员</h2>
            <p class="muted">新成员通过注册页填写邀请码加入当前空间。</p>
            <div class="composer">
              <input :value="invite" readonly aria-label="邀请码" /><button
                @click="copy(invite)"
              >
                复制邀请码
              </button>
            </div>
          </div>
          <div class="tablewrap">
            <table>
              <tr>
                <th>姓名</th>
                <th>邮箱</th>
                <th>身份</th>
              </tr>
              <tr v-for="m in members" :key="m.id">
                <td>{{ m.name }}</td>
                <td>{{ m.email }}</td>
                <td>{{ m.role === "owner" ? "空间管理员" : "成员" }}</td>
              </tr>
            </table>
          </div>
          <div v-if="me.role === 'owner'" class="panel spaced">
            <div class="sectionhead">
              <h2>空间 API Key</h2>
              <button @click="openModal('apikey', { name: 'API 客户端' })">
                <KeyRound :size="16" />创建 Key
              </button>
            </div>
            <p class="muted">
              Key 仅展示一次，有效期 90 天，可调用本空间发布的 Agent。
            </p>
            <div v-for="key in keys" :key="key.id" class="choice">
              <span>{{ key.name }}</span
              ><small class="muted"
                >{{
                  new Date(key.expires * 1000).toLocaleDateString()
                }}
                到期</small
              ><button
                @click="
                  action(async () => {
                    await api('/api-keys/' + key.id, { method: 'DELETE' });
                    keys = await api('/api-keys');
                    notify('Key 已撤销');
                  })
                "
              >
                撤销
              </button>
            </div>
          </div></template
        >
      </main>
    </div>
  </template>

  <div v-if="modal" class="overlay" @click.self="closeModal">
    <div
      role="dialog"
      aria-modal="true"
      class="modal"
      :aria-label="modal === 'child' ? '子 Agent 配置' : '资源配置'"
    >
      <button
        class="close iconbutton"
        aria-label="关闭"
        :disabled="saving"
        @click="closeModal"
      >
        <X :size="20" />
      </button>
      <template v-if="modal === 'publish'"
        ><h2>发布 Agent</h2>
        <p>当前配置将保存为新版本，发布后才能调试。已发布的版本保持不变。</p>
        <div class="notice">
          发布会检查模型与资源是否可用，并包含子 Agent 配置。
        </div>
        <p v-if="error" class="error">{{ error }}</p>
        <div class="actions modalactions">
          <button :disabled="saving" @click="closeModal">取消</button
          ><button class="primary" :disabled="saving" @click="publishAgent">
            {{ saving ? "发布中…" : "确认发布" }}
          </button>
        </div></template
      >
      <form
        v-else-if="
          [
            'model',
            'skill',
            'tool',
            'kb',
            'upload',
            'child',
            'tooltest',
            'apikey',
          ].includes(modal)
        "
        @submit.prevent="submitModal"
      >
        <h2>
          {{
            {
              model: "模型连接",
              skill: "导入 Skill",
              tool: "添加工具",
              kb: "创建知识库",
              upload: "上传知识文档",
              child: "子 Agent 配置",
              tooltest: "测试工具",
              apikey: "创建空间 API Key",
            }[modal]
          }}
        </h2>
        <template v-if="modal === 'model'"
          ><label for="rname">连接名称</label
          ><input id="rname" v-model="form.name" required />
          <div class="row">
            <div>
              <label>供应商</label
              ><select v-model="form.provider" @change="changeModelProvider">
                <option value="deepseek">DeepSeek</option>
                <option value="openai">OpenAI</option>
              </select>
            </div>
            <div>
              <label>用途</label
              ><select
                v-model="form.purpose"
                @change="invalidateModelSelection(form)"
              >
                <option value="chat">聊天模型</option>
                <option value="embedding">向量化模型</option>
              </select>
            </div>
          </div>
          <label for="modelkey">API Key</label
          ><input
            id="modelkey"
            v-model="form.api_key"
            type="password"
            :required="!form.id"
            autocomplete="new-password"
            :placeholder="form.id ? '留空保留当前 Key' : 'Key 仅存于服务端'"
            @input="resetModelDiscovery"
          />
          <div class="actions spaced">
            <button type="button" :disabled="saving" @click="discoverModels">
              {{ saving ? "获取中…" : "获取可用模型" }}
            </button>
          </div>
          <label for="discoveredmodel">选择模型</label>
          <select
            id="discoveredmodel"
            :value="form.model_id"
            @change="selectDiscoveredModel"
          >
            <option value="" disabled>填写 Key 后获取模型列表</option>
            <option
              v-if="
                form.model_id &&
                !modelChoices.some((m) => m.id === form.model_id)
              "
              :value="form.model_id"
            >
              {{ form.model_id }}（当前配置）
            </option>
            <option
              v-for="model in modelChoices"
              :key="model.id"
              :value="model.id"
              :disabled="
                form.purpose === 'chat' &&
                model.metadata.chat_compatible === false
              "
            >
              {{ model.name }} · {{ model.id
              }}{{
                model.metadata.chat_compatible === false &&
                form.purpose === "chat"
                  ? "（当前聊天接口不支持）"
                  : ""
              }}
            </option>
          </select>
          <p class="muted">
            模型列表由当前 Key
            实时获取；可见模型的调用额度及接口权限仍由供应商决定。
          </p>
          <div
            v-if="form.model_id && form.purpose === 'chat'"
            class="notice spaced"
          >
            上下文窗口 {{ form.context_window.toLocaleString() }} tokens ·
            输出预留 {{ form.max_output_tokens.toLocaleString() }} tokens。
            {{ metadataSourceLabels[form.metadata.source] }}
            <span v-if="form.metadata.verified_at"
              >（{{ form.metadata.verified_at }}）</span
            >
          </div>
          <details class="spaced">
            <summary>高级设置：服务地址、手动模型 ID 与上下文预算</summary>
            <label for="baseurl">API 基础地址</label>
            <input
              id="baseurl"
              v-model="form.base_url"
              type="url"
              required
              @input="resetModelDiscovery"
            />
            <label for="modelid">手动模型 ID</label>
            <input
              id="modelid"
              v-model="form.model_id"
              placeholder="接口不提供列表时手动填写"
              @input="manualModelMetadata"
            />
            <template v-if="form.purpose === 'chat'">
              <h3>上下文预算</h3>
              <p class="muted">
                选择模型时自动填入；也可按供应商或代理服务的实际限制覆盖。
                未识别模型的 32,768 tokens 默认值不代表真实窗口。
              </p>
              <label for="contextwindow">模型上下文窗口（tokens）</label>
              <input
                id="contextwindow"
                v-model.number="form.context_window"
                type="number"
                min="1"
                step="1"
                required
                @input="manualBudgetMetadata"
              />
              <div class="row">
                <div>
                  <label for="maxoutput">最大输出 / 输出预留（tokens）</label>
                  <input
                    id="maxoutput"
                    v-model.number="form.max_output_tokens"
                    type="number"
                    min="1"
                    step="1"
                    required
                  />
                </div>
                <div>
                  <label for="safetymargin">安全边距（tokens）</label>
                  <input
                    id="safetymargin"
                    v-model.number="form.safety_margin_tokens"
                    type="number"
                    min="0"
                    step="1"
                    required
                  />
                </div>
              </div>
              <div class="notice spaced">
                可用输入预算 = 上下文窗口 − 输出预留 − 安全边距。
                最大输出同时限制单次模型响应。修改后需重新发布 Agent 才会应用。
              </div>
            </template>
          </details></template
        >
        <template v-if="modal === 'skill'"
          ><label>导入方式</label
          ><select v-model="form.source">
            <option value="upload">文件夹上传</option>
            <option value="git">Git 地址</option></select
          ><template v-if="form.source === 'upload'"
            ><label for="skillfiles">技能目录</label
            ><input
              id="skillfiles"
              type="file"
              webkitdirectory
              multiple
              required
              @change="picked"
            />
            <p class="muted">
              目录中需要 SKILL.md，包含 name 与 description。
            </p></template
          ><template v-else
            ><label for="giturl">Git HTTPS 地址</label
            ><input id="giturl" v-model="form.url" type="url" required /><label
              >技能子目录</label
            ><input
              v-model="form.subdir"
              placeholder="如 skills/report-helper"
            /><label>分支或标签</label
            ><input v-model="form.ref" placeholder="留空使用默认分支" />
            <p class="muted">第一版支持公开 HTTPS 仓库。</p></template
          ></template
        >
        <template v-if="modal === 'tool'"
          ><label>服务名称</label><input v-model="form.name" required /><label
            >接入方式</label
          ><select v-model="form.source">
            <option value="external">外部 MCP 服务</option>
            <option value="upload">托管代码：文件夹上传</option>
            <option value="git">托管代码：Git 导入</option></select
          ><template v-if="form.source === 'external'"
            ><label>MCP 地址</label
            ><input v-model="form.endpoint" type="url" required /><label
              >传输方式</label
            ><select v-model="form.transport">
              <option value="streamable-http">Streamable HTTP</option>
              <option value="sse">SSE</option></select
            ><label>Bearer Token（可选）</label
            ><input
              v-model="form.api_key"
              type="password"
              autocomplete="new-password" /></template
          ><template v-else-if="form.source === 'upload'"
            ><label>Python 工具目录</label
            ><input
              type="file"
              webkitdirectory
              multiple
              required
              @change="picked"
            />
            <p class="muted">
              根目录包含 tool.py，可附 requirements.txt。使用 MCP stdio
              服务启动。
            </p></template
          ><template v-else
            ><label>Git HTTPS 地址</label
            ><input v-model="form.url" type="url" required /><label
              >工具子目录</label
            ><input v-model="form.subdir" /><label>分支或标签</label
            ><input v-model="form.ref"
          /></template>
          <div v-if="form.source !== 'external'" class="notice spaced">
            工具在 Docker 中构建与执行，不读取平台密钥；运行时默认无网络。
          </div></template
        >
        <template v-if="modal === 'kb'"
          ><label>知识库名称</label><input v-model="form.name" required /><label
            >平台向量化模型</label
          ><select v-model="form.embedding_id">
            <option value="">暂不配置，仅关键词检索</option>
            <option v-for="e in embeddings" :key="e.id" :value="e.id">
              {{ e.name }} · {{ e.model_id }}
            </option>
          </select>
          <p class="muted">
            配置向量化模型后，文档建立关键词与向量混合索引。Agent
            无需再选择该模型。
          </p></template
        >
        <template v-if="modal === 'upload'"
          ><p class="muted">{{ form.name }}</p>
          <label>文本文件</label
          ><input
            type="file"
            accept=".md,.markdown,.txt,.sql"
            multiple
            required
            @change="picked"
          />
          <p class="muted">支持 Markdown、TXT、SQL；单文件最多 5MB。</p>
          <ul>
            <li v-for="f in files" :key="f.name">{{ f.name }}</li>
          </ul></template
        >
        <template v-if="modal === 'child'"
          ><div class="notice">
            模型继承主 Agent：{{
              resourceName("models", draft!.model)
            }}。只执行主 Agent 委派的具体任务。
          </div>
          <label for="childname">名称</label
          ><input id="childname" v-model="form.name" required /><label
            for="childdescription"
            >职责与适用任务</label
          ><input
            id="childdescription"
            v-model="form.description"
            required
            placeholder="何时应委派给它"
          /><label for="childprompt">系统 Prompt</label
          ><textarea id="childprompt" v-model="form.prompt" rows="5"></textarea>
          <div v-for="kind in ['skills', 'tools', 'wiki']" :key="kind">
            <h3 class="spaced">{{ titles[kind] }}</h3>
            <label v-for="r in resources[kind]" :key="r.id" class="choice"
              ><input v-model="form[kind]" type="checkbox" :value="r.id" />{{
                r.name
              }}</label
            >
            <p v-if="!resources[kind].length" class="muted">未添加资源</p>
          </div></template
        >
        <template v-if="modal === 'tooltest'"
          ><label>工具</label
          ><select v-model="form.tool">
            <option v-for="s in form.schemas" :key="s.name">
              {{ s.name }}
            </option></select
          ><label>参数 schema</label>
          <pre>{{
            JSON.stringify(
              form.schemas.find((s: any) => s.name === form.tool)?.schema,
              null,
              2,
            )
          }}</pre>
          <label>输入参数（JSON）</label
          ><textarea v-model="form.arguments" required></textarea>
          <pre v-if="form.result">{{
            JSON.stringify(form.result, null, 2)
          }}</pre>
        </template>
        <template v-if="modal === 'apikey'"
          ><template v-if="!keyResult"
            ><label>名称</label><input v-model="form.name" required /></template
          ><template v-else
            ><div class="notice">仅显示一次，请保存到你的调用程序配置中。</div>
            <pre>{{ keyResult }}</pre>
            <button type="button" @click="copy(keyResult)">
              复制 Key
            </button></template
          ></template
        >
        <p v-if="error" class="error">{{ error }}</p>
        <div class="actions modalactions">
          <button type="button" :disabled="saving" @click="closeModal">
            {{ keyResult ? "关闭" : "取消" }}</button
          ><button v-if="!keyResult" class="primary" :disabled="saving">
            {{
              saving
                ? "处理中…"
                : modal === "child"
                  ? "保存子 Agent 配置"
                  : modal === "tooltest"
                    ? "调用工具"
                    : modal === "upload"
                      ? "上传并索引"
                      : "保存"
            }}
          </button>
        </div>
      </form>
      <template v-else-if="modal === 'detail' && resourceDetail"
        ><h2>{{ resourceDetail.name }}</h2>
        <div class="tags">
          <span class="tag">{{
            statusNames[resourceDetail.status] || resourceDetail.status
          }}</span>
        </div>
        <template v-if="page === 'skills'"
          ><p>{{ resourceDetail.description }}</p>
          <label>SKILL.md</label>
          <pre>{{ resourceDetail.content }}</pre>
          <label>目录文件</label>
          <div class="filetree">
            <a
              v-for="f in resourceDetail.files"
              :key="f"
              :href="`/api/skills/${resourceDetail.id}/file?path=${encodeURIComponent(f)}`"
              >{{ f }}</a
            >
          </div></template
        ><template v-if="page === 'tools'"
          ><p v-if="resourceDetail.error" class="error">
            {{ resourceDetail.error }}
          </p>
          <h3 class="spaced">工具定义</h3>
          <details
            v-for="t in resourceDetail.schemas"
            :key="t.name"
            class="event"
          >
            <summary>{{ t.name }}</summary>
            <p>{{ t.description }}</p>
            <pre>{{ JSON.stringify(t.schema, null, 2) }}</pre>
          </details>
          <pre v-if="resourceDetail.build_log">{{
            resourceDetail.build_log
          }}</pre></template
        ><template v-if="page === 'wiki'"
          ><h3 class="spaced">文档</h3>
          <div v-for="d in docs" :key="d.id" class="choice">
            <button class="textbutton" @click="showDocument(d.id)">
              {{ d.name }}</button
            ><span class="tag">v{{ d.revision }}</span
            ><button @click="deleteDoc(d.id)">移除</button>
          </div>
          <p v-if="!docs.length" class="muted">还没有文档</p>
          <h3 class="spaced">检索测试</h3>
          <form class="composer" @submit.prevent="searchKB">
            <input
              v-model="searchQuery"
              required
              placeholder="输入问题、表名或字段"
            /><button :disabled="saving">搜索</button>
          </form>
          <div v-for="hit in searchResults" :key="hit.id" class="steps">
            <button class="textbutton" @click="showDocument(hit.document_id)">
              {{ hit.file }}:{{ hit.start_line }}–{{ hit.end_line }}
            </button>
            <p class="answer">{{ hit.content }}</p>
          </div></template
        ><template v-if="page === 'models'"
          ><p>供应商：{{ resourceDetail.provider }}</p>
          <p>模型：{{ resourceDetail.model_id }}</p>
          <p>基础地址：{{ resourceDetail.base_url }}</p>
          <div class="notice">API Key 仅保存在服务端，不回显。</div></template
        >
        <div class="actions modalactions">
          <button
            :disabled="saving"
            @click="resourceAction('remove', resourceDetail)"
          >
            移除资源</button
          ><button @click="closeModal">关闭</button>
        </div></template
      >
      <template v-else-if="modal === 'document'"
        ><h2>{{ form.name }}</h2>
        <pre class="documenttext">{{ form.content }}</pre>
      </template>
    </div>
  </div>
  <div v-if="toast" class="toast" role="status">{{ toast }}</div>
</template>
