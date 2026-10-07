<script setup lang="ts">
import { computed } from "vue";
import { ArrowLeft, Save, Plus, MessageSquare } from "lucide-vue-next";
import { titles } from "../domain/labels";
import type { Agent, AgentDraft, Child, Resource } from "../domain/types";
const props = defineProps<{
  draft: AgentDraft;
  resources: Record<string, Resource[]>;
  chats: Resource[];
  saving: boolean;
}>();
const triggerPercent = computed({
  get: () =>
    Number((props.draft.context_policy.context_ratio * 100).toFixed(4)),
  set: (value: number) =>
    (props.draft.context_policy.context_ratio = value / 100),
});
const targetPercent = computed({
  get: () => Number((props.draft.context_policy.target_ratio * 100).toFixed(4)),
  set: (value: number) =>
    (props.draft.context_policy.target_ratio = value / 100),
});
function setCount(key: "user_turns" | "model_steps", event: Event) {
  const value = (event.target as HTMLInputElement).value;
  props.draft.context_policy[key] = value === "" ? null : Number(value);
}
const emit = defineEmits<{
  back: [];
  save: [];
  publish: [];
  child: [child?: Child];
  removeChild: [id: string];
  use: [agent: Agent];
}>();
</script>
<template>
  <div class="pagehead">
    <div>
      <h1>{{ draft.name || "新建 Agent" }}</h1>
      <p class="muted">
        开发配置 ·
        {{ draft.published ? "已有发布版本 v" + draft.published : "尚未发布" }}
      </p>
    </div>
    <div class="actions">
      <button @click="emit('back')"><ArrowLeft :size="16" />返回列表</button
      ><button :disabled="saving" @click="emit('save')">
        <Save :size="16" />保存草稿</button
      ><button class="primary" :disabled="saving" @click="emit('publish')">
        发布版本
      </button>
    </div>
  </div>
  <div class="split editorlayout">
    <div class="panel">
      <section>
        <h2>基础配置</h2>
        <div class="row">
          <div>
            <label for="agentname">Agent 名称</label
            ><input
              id="agentname"
              v-model="draft.name"
              placeholder="Agent 名称"
            />
          </div>
          <div>
            <label for="agentmodel">模型</label
            ><select id="agentmodel" v-model="draft.model">
              <option value="" disabled>请选择聊天模型</option>
              <option v-for="r in chats" :key="r.id" :value="r.id">
                {{ r.name }} · {{ r.model_id }}
              </option>
            </select>
          </div>
        </div>
        <div v-if="!chats.length" class="notice">
          先在模型连接页配置 DeepSeek 或 OpenAI。
        </div>
        <label for="prompt">系统 Prompt</label
        ><textarea
          id="prompt"
          v-model="draft.prompt"
          rows="6"
          placeholder="描述角色、任务方法与输出要求"
        ></textarea>
      </section>
      <section>
        <h2>执行策略</h2>
        <div class="modes">
          <button
            :class="{ selected: draft.mode === 'react' }"
            @click="draft.mode = 'react'"
          >
            <b>ReAct</b><small>逐步决策、调用工具并处理结果</small></button
          ><button
            :class="{ selected: draft.mode === 'plan' }"
            @click="draft.mode = 'plan'"
          >
            <b>Plan</b><small>自动规划、执行，并按结果调整计划</small>
          </button>
        </div>
      </section>
      <section>
        <h2>上下文压缩</h2>
        <p class="muted">
          在模型调用前检查。默认达到可用输入预算的 80% 时主动压缩；
          可用预算已扣除模型输出预留与安全边距。
        </p>
        <div class="row">
          <div>
            <label for="contexttrigger">预算触发比例（%）</label>
            <input
              id="contexttrigger"
              v-model.number="triggerPercent"
              type="number"
              min="0.01"
              max="100"
              step="0.1"
            />
          </div>
          <div>
            <label for="contexttarget">压缩目标比例（%）</label>
            <input
              id="contexttarget"
              v-model.number="targetPercent"
              type="number"
              min="0.01"
              max="99.99"
              step="0.1"
            />
          </div>
        </div>
        <div class="row">
          <div>
            <label for="contextturns">用户输入轮数（可选）</label>
            <input
              id="contextturns"
              :value="draft.context_policy.user_turns ?? ''"
              type="number"
              min="1"
              step="1"
              placeholder="留空不启用"
              @input="setCount('user_turns', $event)"
            />
          </div>
          <div>
            <label for="contextsteps">模型执行步骤数（可选）</label>
            <input
              id="contextsteps"
              :value="draft.context_policy.model_steps ?? ''"
              type="number"
              min="1"
              step="1"
              placeholder="留空不启用"
              @input="setCount('model_steps', $event)"
            />
          </div>
        </div>
        <label for="compactioncalls">每次压缩最多调用模型次数</label>
        <input
          id="compactioncalls"
          v-model.number="draft.context_policy.max_compaction_calls"
          type="number"
          min="1"
          max="16"
          step="1"
        />
        <p class="muted">
          预算、轮数、步骤数满足任一条件即触发；轮数和步骤数从上次压缩后累计。
          子 Agent 继承此策略，按自身上下文计数。修改需发布后生效。
        </p>
      </section>
      <section v-for="kind in ['skills', 'tools', 'wiki']" :key="kind">
        <h2>{{ titles[kind] }}</h2>
        <label v-for="r in resources[kind]" :key="r.id" class="choice"
          ><input
            v-model="draft[kind as 'skills' | 'tools' | 'wiki']"
            type="checkbox"
            :value="r.id"
          />{{ r.name
          }}<span class="tag">{{
            kind === "tools"
              ? r.source === "hosted"
                ? "平台托管"
                : "外部 MCP"
              : kind === "wiki"
                ? r.retrieval === "hybrid"
                  ? "混合检索"
                  : "关键词检索"
                : r.version
          }}</span></label
        >
        <p v-if="!resources[kind].length" class="muted">
          还没有资源，可在{{ titles[kind] }}页面添加。
        </p>
      </section>
      <section>
        <div class="sectionhead">
          <h2>子 Agent</h2>
          <button @click="emit('child')"><Plus :size="16" />添加配置</button>
        </div>
        <p class="muted">
          按需创建以执行子任务；继承主 Agent 模型，不单独配置执行模式。
        </p>
        <div v-for="child in draft.subs" :key="child.id" class="steps">
          <div class="sectionhead">
            <h3>{{ child.name }}</h3>
            <div class="actions">
              <button @click="emit('child', child)">编辑</button
              ><button @click="emit('removeChild', child.id)">移除</button>
            </div>
          </div>
          <p>{{ child.description }}</p>
          <div class="tags">
            <span class="tag">模型继承主 Agent</span
            ><span class="tag"
              >{{ child.skills.length }} 技能 · {{ child.tools.length }} 工具 ·
              {{ child.wiki.length }} 知识库</span
            >
          </div>
        </div>
      </section>
    </div>
    <div class="panel publishpanel">
      <div class="agenticon"><MessageSquare /></div>
      <h2>发布后调试</h2>
      <p class="muted">草稿只用于配置。发布版本后，才能运行调试对话。</p>
      <div class="notice">
        配置修改需要重新发布才会生效，调试始终使用选定的发布版本。
      </div>
      <button
        v-if="draft.published"
        class="primary"
        @click="emit('use', draft)"
      >
        调试已发布版本 v{{ draft.published }}</button
      ><button v-else disabled>尚未发布</button>
    </div>
  </div>
</template>
