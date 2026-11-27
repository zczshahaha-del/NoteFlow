# 知流 NoteFlow 官网

官网用同一篇 Docker 示例笔记介绍知流的工作台、笔记搜索和 AI 助手。页面中的产品界面均来自真实知流前端（`34396e7`）和本机隔离的 FastAPI / PostgreSQL 实例。笔记和账号为此次截图新建的测试资料。AI 使用项目已有的模型配置实际回答，并从当前笔记给出两处原文引用。网页展示截图，点击可查看大图。

## 截图来源

- `assets/workspace.webp`、`assets/mobile.webp` 来自同一篇笔记的桌面和手机布局。
- `assets/search.webp` 来自「搜索全部笔记」中查询「容器」的实际结果，裁切至搜索窗口周围。
- `assets/assistant.webp` 是在同一篇笔记旁发问的真实界面。`assets/assistant-detail.webp` 从同一图裁出供小屏阅读的回答和引用区域。
- `assets/zhiliu.svg` 取自项目的 `public/favicon.svg`。

WebP 仅裁切、缩放和压缩。截图未使用实际用户的笔记、账号或会话。

## 本地预览和托管

从仓库根目录运行：

```sh
python3 -m http.server 4178 --bind 127.0.0.1 --directory website
```

打开 <http://127.0.0.1:4178/>。发布时将 `index.html`、`styles.css` 与 `assets/` 放到同一静态目录。资源使用相对路径，可部署到独立域名或子路径。回退可以恢复上一版静态目录，或在托管平台切回相应版本。项目仓库提供工作台的运行说明。
