<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from "vue";
import { useData } from "vitepress";

const props = defineProps<{ source: string }>();
const { isDark } = useData();
const markup = ref("");
const error = ref(false);
let revision = 0;
let alive = true;

async function renderDiagram() {
  const current = ++revision;
  try {
    const { default: mermaid } = await import("mermaid");
    if (!alive || current !== revision) return;
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: isDark.value ? "dark" : "neutral",
      fontFamily: "ui-sans-serif, system-ui, sans-serif",
    });
    const id = `loom-diagram-${crypto.randomUUID()}`;
    const { svg } = await mermaid.render(id, props.source);
    if (!alive || current !== revision) return;
    markup.value = svg;
    error.value = false;
  } catch {
    if (alive && current === revision) error.value = true;
  }
}
onMounted(renderDiagram);
watch([() => props.source, isDark], renderDiagram);
onBeforeUnmount(() => {
  alive = false;
  revision++;
});
</script>

<template>
  <figure class="loom-diagram" aria-label="架构关系图">
    <div v-if="markup && !error" class="loom-diagram-content" v-html="markup" />
    <pre v-else class="loom-diagram-source">{{ source }}</pre>
    <figcaption v-if="error">图表暂时未能显示，以上为原始定义。</figcaption>
  </figure>
</template>
