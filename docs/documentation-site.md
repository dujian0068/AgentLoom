# 维护与部署文档站

文档使用 VitePress 1.6.4 生成网页。Node.js 需要 22.12+。`docs/*.md` 仍是正文来源，可继续在 GitHub 阅读；侧栏、搜索和主题由 `docs/.vitepress` 配置。不维护第二套 HTML 正文。

## 本地阅读

在项目根目录安装文档依赖，然后启动：

```sh
npm ci
npm run docs:dev
```

打开 http://127.0.0.1:8767 。修改 Markdown 后页面会自动更新。文档站独立于业务后端，不需要数据库、供应商 Key 或 `.env`。

## 构建与预览

```sh
npm run docs:build
npm run docs:preview
```

输出位于 `docs/.vitepress/dist`，可以部署到静态 Web 服务器。预览默认也使用端口 8767，先关闭同端口开发服务再启动预览。

构建产物、依赖和缓存被 Git 忽略；提交 Markdown、主题、配置和根目录的 package-lock.json 即可。

## 使用子路径部署

若站点地址使用 `/AgentLoom/` 等前缀，构建时设置：

```sh
AGENTLOOM_DOCS_BASE=/AgentLoom/ npm run docs:build
AGENTLOOM_DOCS_BASE=/AgentLoom/ npm run docs:preview
```

上传 dist 内容到服务器的对应路径。此配置没有自动开启 GitHub Pages，也没有公开发布文档；上线时按仓库可见性和部署环境决定访问范围。

站内使用 `.html` 地址，静态服务器无需配置单页应用回退。保留 `404.html` 作为未找到页面。首页为 `index.html`。

## 依赖与校验

根目录 package.json/lock 管理文档依赖，apps/web 的依赖继续独立。构建工具固定为 Vite 7.3.7、Vue 插件 6.0.8，Mermaid 的 KaTeX 固定为 0.19.0；这些 overrides 用于避开旧依赖链的已知漏洞，升级时须同时复验构建和浏览器导航。

`npm run docs:check` 在构建后检查产物里的站内页面、锚点、图片和脚本链接，需要 Python 3。子路径构建使用 `npm run docs:check -- --base /AgentLoom/`。

## 维护规则

- 新增文档后在 `docs/.vitepress/config.mts` 中添加侧栏，并更新文档索引。
- 正文继续写相对 Markdown 链接，例如 `runtime-design.md`；构建时自动转换成站内地址。
- `../apps`、`../packages` 等源码和目录链接由文档插件转到 GitHub `master` 的 blob/tree 地址，不会把项目文件复制进站点。
- 原有显式 `<a id="...">` 锚点保留；更名标题前检查入站章节链接。
- 搜索在浏览器本地运行，支持中文分词；不把搜索词发送到外部搜索服务。早期需求/实现/Demo 和第一阶段模块化记录不纳入搜索，但保留在历史目录中，避免旧答案抢占当前设计。
- Mermaid 代码块在浏览器渲染为 SVG；原有 SVG 架构图继续直接嵌入。
- VitePress 的生产构建会检查页面链接，不要用忽略所有死链的选项掩盖错误。

框架参考：[VitePress](https://vitepress.dev/guide/getting-started)、[本地搜索](https://vitepress.dev/reference/default-theme-search)、[Mermaid](https://mermaid.js.org/config/usage.html)。
