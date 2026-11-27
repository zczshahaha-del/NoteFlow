# 知流 NoteFlow 官网

官网主线是用自然语言生成笔记。首屏 4K 实录展示从输入写作要求、生成大纲和正文，到确认保存并打开正式笔记。后续展示阅读目录、整篇修改、选区改写和长期记忆。

## 截图来源

录屏和截图均来自知流前端 `34396e7` 在本机隔离的 FastAPI / PostgreSQL 环境中运行的界面。使用测试账号、Docker 示例笔记和测试写作偏好。截图只做裁切、缩放和 WebP 压缩。

- `create-note-4k.mp4` 来自完整的一次产品操作。Chrome 源画面为 4320×2430，成片为 3840×2160、26.6 秒、H.264。保留真实界面和生成过程，剪短等待，添加分段标题与操作指示。`film-poster.webp` 取自该次录制的正文画面。
- `outline.webp` 是阅读笔记时展开的章节目录，`outline-detail.webp` 裁自同一张实录。
- `edit.webp` 是 AI 生成整篇修改后的原文对照。`selection.webp` 是编辑器内「AI 改一下」的实际输出，`selection-detail.webp` 是放大的原始面板。
- `memory.webp`、`recall.webp` 展示通过实际 API 保存的测试偏好，以及在新对话中读回这条偏好。使用项目的本机记忆引擎进行隔离验证。
- `zhiliu.svg` 取自项目的 `public/favicon.svg`。

## 本地预览和托管

从仓库根目录运行：

```sh
python3 -m http.server 4178 --bind 127.0.0.1 --directory website
```

打开 <http://127.0.0.1:4178/>。发布时将 `index.html`、`styles.css`、`film.js` 与 `assets/` 放到同一静态目录。资源使用相对路径，可部署到独立域名或子路径。回退可以恢复上一版静态目录，或在托管平台切回相应版本。项目仓库提供工作台的运行说明。
