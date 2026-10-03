<script setup lang="ts">
import { Bot, BookOpen, Play } from "lucide-vue-next";
import { titles, statusNames, eventNames } from "../domain/labels";
import type { Agent } from "../domain/types";
import type { useRunWorkspace } from "../composables/useRunWorkspace";
const props = defineProps<{ workspace: ReturnType<typeof useRunWorkspace> }>();
const emit = defineEmits<{
  back: [];
  edit: [agent: Agent];
  document: [id: string];
}>();
const {
  activeAgent,
  published,
  versions,
  chosenVersion,
  question,
  conversation,
  session,
  activeRun,
  running,
  events,
  runTab,
  savedRuns,
  selectedRun,
  currentPlan,
  loadVersion,
  run,
  resumeRun,
  restoreRun,
  cancelRun,
  refreshRun,
  apiExample,
  eventDetail,
  newSession,
  action,
  copy,
  saving,
  connectionNotice,
} = props.workspace;
</script>
<template>
  <div class="pagehead">
    <div>
      <h1>{{ published.config.name }}</h1>
      <p class="muted">
        已发布应用 · {{ published.config.mode === "plan" ? "Plan" : "ReAct" }}
      </p>
    </div>
    <div class="actions">
      <select
        v-model="chosenVersion"
        :disabled="running"
        aria-label="发布版本"
        @change="action(loadVersion)"
      >
        <option v-for="v in versions" :key="v.version" :value="v.version">
          版本 v{{ v.version }}
        </option></select
      ><button @click="emit('back')">返回列表</button
      ><button :disabled="running" @click="emit('edit', activeAgent!)">
        开发配置
      </button>
    </div>
  </div>
  <div class="split">
    <div class="panel chat">
      <div class="tabs">
        <button :class="{ active: runTab === 'chat' }" @click="runTab = 'chat'">
          调试对话</button
        ><button
          :class="{ active: runTab === 'trace' }"
          @click="runTab = 'trace'"
        >
          执行记录 <span v-if="events.length">{{ events.length }}</span>
        </button>
      </div>
      <div class="notice">
        使用已发布版本 v{{ chosenVersion }}；模型
        {{ published.model_obj.model_id }}。
      </div>
      <div v-if="runTab === 'chat'" class="chatarea">
        <div v-if="!conversation.length" class="chatempty">
          <div class="agenticon"><Bot /></div>
          <h3>向 Agent 发起任务</h3>
          <p>模型、技能与工具将按已发布配置运行。</p>
        </div>
        <div
          v-for="(message, index) in conversation"
          :key="index"
          class="message"
          :class="{ human: message.role === 'user' }"
        >
          <small>{{ message.role === "user" ? "你" : "Agent" }}</small>
          <div class="answer">
            {{ message.text || "正在执行，请在执行记录中查看进度…" }}
          </div>
          <div v-if="message.citations?.length" class="citations">
            <button
              v-for="c in message.citations"
              :key="c.id"
              class="textbutton"
              @click="emit('document', c.document_id)"
            >
              {{ c.file }}:{{ c.start_line }}–{{ c.end_line }}
            </button>
          </div>
          <div v-if="message.artifacts?.length" class="artifacts">
            <a
              v-for="file in message.artifacts"
              :key="file"
              :href="`/api/v1/runs/${message.run_id}/artifact?path=${encodeURIComponent(file)}`"
              ><BookOpen :size="15" />{{ file }}</a
            >
          </div>
        </div>
      </div>
      <div v-else class="chatarea">
        <p v-if="!events.length" class="muted">
          发起任务后，执行事件会显示在这里。
        </p>
        <details v-for="e in events" :key="e.seq" class="event">
          <summary>
            <span class="eventnum">{{ e.seq }}</span
            ><b>{{ eventNames[e.kind] || e.kind }}</b
            ><span v-if="e.instance" class="tag">{{ e.instance }}</span>
          </summary>
          <pre v-if="eventDetail(e)">{{ eventDetail(e) }}</pre>
        </details>
      </div>
      <div v-if="currentPlan.length" class="steps planlist">
        <h3>执行计划</h3>
        <div v-for="step in currentPlan" :key="step.id" class="choice">
          <span class="tag" :class="{ green: step.status === 'completed' }">{{
            statusNames[step.status] || step.status
          }}</span
          >{{ step.step }}
        </div>
      </div>
      <p v-if="connectionNotice" class="notice">{{ connectionNotice }}</p>
      <div class="runstate" v-if="activeRun">
        <span
          class="tag"
          :class="{ green: activeRun.status === 'succeeded' }"
          >{{ statusNames[activeRun.status] || activeRun.status }}</span
        ><button class="textbutton" @click="refreshRun">刷新状态</button
        ><button v-if="running" @click="cancelRun">停止任务</button
        ><button
          v-if="activeRun.resumable && !running"
          class="primary"
          :disabled="saving"
          @click="resumeRun"
        >
          继续任务
        </button>
      </div>
      <form class="composer" @submit.prevent="run">
        <input
          v-model="question"
          :placeholder="
            activeRun?.resumable
              ? '可补充信息，再点继续任务；或发起新任务'
              : '输入你的任务'
          "
          aria-label="任务"
          :disabled="running"
          required
        /><button class="primary" :disabled="running || saving">
          <Play :size="16" />{{ running ? "执行中" : "运行" }}
        </button>
      </form>
      <button
        v-if="session && !running"
        class="textbutton"
        @click="newSession()"
      >
        新建会话
      </button>
    </div>
    <div class="panel">
      <h2>最近任务</h2>
      <select
        v-model="selectedRun"
        :disabled="running"
        aria-label="最近任务"
        @change="restoreRun"
      >
        <option value="">选择任务查看或恢复</option>
        <option v-for="r in savedRuns" :key="r.id" :value="r.id">
          {{ statusNames[r.status] || r.status }} · {{ r.input.slice(0, 35) }}
        </option>
      </select>
      <p class="muted">中断、失败或等待补充的任务，可从保存的进度继续。</p>
      <h2>API 调用</h2>
      <p class="muted">网页和 API 共用同一运行引擎。</p>
      <pre>{{ apiExample }}</pre>
      <button @click="copy(apiExample)">复制示例</button>
      <h2 style="margin-top: 28px">绑定资源</h2>
      <div v-for="kind in ['skills', 'tools', 'wiki']" :key="kind">
        <label>{{ titles[kind] }}</label>
        <div class="tags">
          <span v-for="r in published[kind]" :key="r.id" class="tag">{{
            r.name
          }}</span
          ><span v-if="!published[kind].length" class="muted">未绑定</span>
        </div>
      </div>
      <h2 style="margin-top: 28px">子 Agent 配置</h2>
      <div v-for="child in published.config.subs" :key="child.id" class="steps">
        <h3>{{ child.name }}</h3>
        <p>{{ child.description }}</p>
        <span class="tag">继承模型 {{ published.model_obj.model_id }}</span>
      </div>
      <p v-if="!published.config.subs.length" class="muted">未配置</p>
    </div>
  </div>
</template>
