# 知流 NoteFlow 官网

官网主线是用自然语言生成笔记。示例贯穿草稿大纲与正文、阅读目录、整篇修改、选区改写和长期记忆。

## 截图来源

所有截图均来自知流前端 `34396e7` 在本机隔离的 FastAPI / PostgreSQL 环境中运行的界面。使用测试账号、Docker 示例笔记和测试写作偏好。截图只做裁切、缩放和 WebP 压缩。

- `draft-prompt.webp`、`draft.webp` 展示自然语言请求、实际 AI 草稿卡、生成的大纲和章节正文。实测完成后可确认保存，并在笔记列表读取保存的正文。
- `outline.webp` 是阅读笔记时展开的章节目录。
- `edit.webp` 是 AI 生成整篇修改后的原文对照。`selection.webp` 是编辑器内「AI 改一下」的实际输出。
- `memory.webp`、`recall.webp` 展示通过实际 API 保存的测试偏好，以及在新对话中读回这条偏好。使用项目的本机记忆引擎进行隔离验证。
- `zhiliu.svg` 取自项目的 `public/favicon.svg`。

## 本地预览和托管

从仓库根目录运行：

```sh
python3 -m http.server 4178 --bind 127.0.0.1 --directory website
```

打开 <http://127.0.0.1:4178/>。发布时将 `index.html`、`styles.css` 与 `assets/` 放到同一静态目录。资源使用相对路径，可部署到独立域名或子路径。回退可以恢复上一版静态目录，或在托管平台切回相应版本。项目仓库提供工作台的运行说明。
