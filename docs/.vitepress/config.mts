import { defineConfig } from "vitepress";
import { existsSync, statSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const docsRoot = path.resolve(fileURLToPath(new URL("..", import.meta.url)));
const repoRoot = path.dirname(docsRoot);
const repository = "https://github.com/dujian0068/AgentLoom";
const base = process.env.AGENTLOOM_DOCS_BASE || "/";
if (!base.startsWith("/") || !base.endsWith("/")) {
  throw new Error("AGENTLOOM_DOCS_BASE 必须以 / 开始和结束，例如 /AgentLoom/");
}

export default defineConfig({
  lang: "zh-CN",
  title: "AgentLoom",
  description: "织点 · 团队 Agent 平台技术文档",
  base,
  lastUpdated: true,
  cleanUrls: false,
  ignoreDeadLinks: false,
  vite: {
    server: { strictPort: true },
    preview: { strictPort: true },
    plugins: [
      {
        name: "agentloom-original-diagrams",
        generateBundle() {
          for (const name of readdirSync(path.join(docsRoot, "diagrams"))) {
            if (name.endsWith(".svg"))
              this.emitFile({
                type: "asset",
                fileName: `diagrams/${name}`,
                source: readFileSync(path.join(docsRoot, "diagrams", name)),
              });
          }
        },
      },
    ],
  },
  themeConfig: {
    siteTitle: "AgentLoom 文档",
    nav: [
      { text: "阅读指南", link: "/reading-guide" },
      { text: "架构设计", link: "/technical-design" },
      { text: "API 与部署", link: "/platform-api-data" },
    ],
    socialLinks: [{ icon: "github", link: repository }],
    sidebar: [
      {
        text: "开始阅读",
        items: [
          { text: "文档首页", link: "/" },
          { text: "阅读指南", link: "/reading-guide" },
          { text: "产品需求与实现范围", link: "/requirements-v0.2" },
          { text: "总体技术设计", link: "/technical-design" },
          { text: "源码目录与依赖", link: "/directory-plan" },
        ],
      },
      {
        text: "Runtime 与扩展",
        items: [
          { text: "Runtime 详细设计", link: "/runtime-design" },
          { text: "事件总线与工具", link: "/runtime-event-bus" },
          { text: "Hooks 使用与恢复", link: "/hooks-runtime-v0.1" },
          { text: "上下文与压缩", link: "/runtime-context-v0.2" },
          {
            text: "系统工具与共享工作区",
            link: "/system-tools-shared-workspace-v0.1",
          },
          { text: "Embedding 与知识索引", link: "/embedding-hooks-v0.1" },
        ],
      },
      {
        text: "平台与部署",
        items: [
          { text: "平台、API 与数据", link: "/platform-api-data" },
          { text: "部署与运维", link: "/deployment-operations" },
          { text: "PostgreSQL 专题", link: "/database-postgresql" },
        ],
      },
      {
        text: "目标设计 · 部分待实现",
        collapsed: true,
        items: [
          { text: "总体目标架构", link: "/architecture-v0.3" },
          { text: "Hooks 扩展目标", link: "/hooks-design-v0.1" },
          { text: "上下文与记忆目标", link: "/context-management-design-v0.1" },
        ],
      },
      {
        text: "历史与索引",
        collapsed: true,
        items: [
          { text: "完整文档索引", link: "/README" },
          { text: "第一阶段模块化", link: "/runtime-modularity-v0.1" },
          { text: "需求 v0.1", link: "/requirements-v0.1" },
          { text: "实现记录 v0.1", link: "/implementation-v0.1" },
          { text: "实现记录 v0.2", link: "/implementation-v0.2" },
          { text: "静态 Demo 验证", link: "/demo-verification" },
          { text: "维护文档站", link: "/documentation-site" },
        ],
      },
    ],
    outline: { level: [2, 3], label: "本页目录" },
    docFooter: { prev: "上一篇", next: "下一篇" },
    returnToTopLabel: "返回顶部",
    sidebarMenuLabel: "文档目录",
    darkModeSwitchLabel: "主题",
    lightModeSwitchTitle: "切换为浅色",
    darkModeSwitchTitle: "切换为深色",
    skipToContentLabel: "跳到正文",
    editLink: {
      pattern: `${repository}/edit/master/docs/:path`,
      text: "在 GitHub 编辑此页",
    },
    lastUpdated: { text: "最近更新", formatOptions: { dateStyle: "medium" } },
    search: {
      provider: "local",
      options: {
        _render(src, env, md) {
          const history = [
            "requirements-v0.1.md",
            "implementation-v0.1.md",
            "implementation-v0.2.md",
            "demo-verification.md",
            "runtime-modularity-v0.1.md",
          ];
          if (history.includes(env.relativePath)) return "";
          const html = md.render(src, env);
          return env.frontmatter?.search === false ? "" : html;
        },
        miniSearch: {
          options: {
            // The same self-contained function runs during indexing and in the browser.
            tokenize: (text: string) => {
              const normalized = text.toLocaleLowerCase("zh-CN");
              const words = Array.from(
                new Intl.Segmenter("zh-CN", { granularity: "word" }).segment(
                  normalized,
                ),
              )
                .filter((part) => part.isWordLike)
                .map((part) => part.segment);
              const han = normalized.match(/[\p{Script=Han}]+/gu) || [];
              for (const run of han) {
                for (let i = 0; i < run.length - 1; i++)
                  words.push(run.slice(i, i + 2));
              }
              return Array.from(new Set(words));
            },
          },
          searchOptions: {
            prefix: true,
            fuzzy: 0.1,
            boost: { title: 5, titles: 2, text: 1 },
            boostDocument: (id: string) => {
              if (
                /context-management-design|hooks-design|architecture-v0.3|README/.test(
                  id,
                )
              )
                return 0.25;
              return /technical-design|runtime-design|platform-api-data|deployment-operations|runtime-context/.test(
                id,
              )
                ? 2
                : 1;
            },
          },
        },
        translations: {
          button: { buttonText: "搜索文档", buttonAriaLabel: "搜索文档" },
          modal: {
            displayDetails: "显示详细内容",
            resetButtonTitle: "清空搜索",
            backButtonTitle: "关闭搜索",
            noResultsText: "没有找到相关内容",
            footer: {
              selectText: "选择",
              selectKeyAriaLabel: "回车",
              navigateText: "切换",
              navigateUpKeyAriaLabel: "上",
              navigateDownKeyAriaLabel: "下",
              closeText: "关闭",
              closeKeyAriaLabel: "Esc",
            },
          },
        },
      },
    },
  },
  markdown: {
    anchor: {
      // Keep GitHub-style heading links, including existing numeric Chinese anchors.
      slugify: (text: string) =>
        text
          .toLowerCase()
          .replace(/[^\p{L}\p{N}\p{M}_\-\s]/gu, "")
          .trim()
          .replace(/\s/g, "-"),
    },
    config(md) {
      // Retain repository-relative Markdown for GitHub while giving the site valid source links.
      md.core.ruler.after("inline", "repository-links", (state) => {
        const current = state.env.path || path.join(docsRoot, "index.md");
        for (const block of state.tokens) {
          for (const token of block.children || []) {
            if (token.type !== "link_open") continue;
            const href = token.attrGet("href");
            if (!href) continue;
            if (/^https?:\/\/localhost(?::|\/|$)/i.test(href)) {
              // These documented local application URLs are external to the documentation site.
              token.attrSet("target", "_blank");
              token.attrSet("rel", "noopener noreferrer");
              continue;
            }
            if (/^(?:[a-z]+:|\/|#)/i.test(href)) continue;
            const [pathname, suffix = ""] = href.split(/(?=[?#])/s, 2);
            const target = path.resolve(
              path.dirname(current),
              decodeURIComponent(pathname),
            );
            if (!existsSync(target)) continue;
            const relative = path
              .relative(repoRoot, target)
              .split(path.sep)
              .join("/");
            const insideDocs = target.startsWith(docsRoot + path.sep);
            if (insideDocs && target.endsWith(".svg")) {
              token.attrSet("target", "_blank");
              token.attrSet("rel", "noopener noreferrer");
              continue;
            }
            if (insideDocs && target.endsWith(".md")) continue;
            if (relative.startsWith("../")) continue;
            const kind = statSync(target).isDirectory() ? "tree" : "blob";
            token.attrSet(
              "href",
              `${repository}/${kind}/master/${relative.split("/").map(encodeURIComponent).join("/")}${suffix}`,
            );
            token.attrSet("target", "_blank");
            token.attrSet("rel", "noopener noreferrer");
          }
        }
      });
      const fence = md.renderer.rules.fence!;
      md.renderer.rules.fence = (tokens, index, options, env, self) => {
        if (tokens[index].info.trim() === "mermaid") {
          const source = md.utils.escapeHtml(
            JSON.stringify(tokens[index].content),
          );
          return `<MermaidDiagram :source="${source}" />`;
        }
        return fence(tokens, index, options, env, self);
      };
    },
  },
});
