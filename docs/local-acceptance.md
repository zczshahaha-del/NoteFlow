# 真实邮件与本机持续运行验收

真实邮件须经过系统发码、用户从实际收件箱取码、登录成功三个环节；开发环境返回 developmentCode 不算收信。2026-11-11 实际 QQ 收信由用户确认，独立 Edge 的真实表单完成登录，`/api/auth/me` 校验账号、刷新保留会话及退出后 401 均通过。证据见 [真实邮件记录](verification/real-mail-login-2026-11-11.json)，不保存验证码、会话值或密钥。

持续运行命令（项目已配置并启动本机 API、PostgreSQL、Redis 和 Hindsight）：

```bash
node scripts/run-python.mjs scripts/run_local_soak.py --duration-seconds 3600 --concurrency 3 --report /private/tmp/noteflow-soak-hour.json
```

脚本默认 3 个合成账号、每账号每秒约一项操作，读取笔记 / 版本 / 附件、执行文字检索和跨账号拒绝检查，每 5 分钟左右写入并等待索引。首尾各进行一次真实混合检索和短 Agent 回答，外部模型调用有界；`--skip-external` 可关闭这两类探测。合成会话不发送邮件，不代替上面的真实登录验证。

每分钟采样 API 进程 RSS / CPU、三个本机容器的内存和 CPU、数据库连接状态及这些账号的任务状态。要求请求、正文 / 附件一致性、账号隔离和索引都通过，健康 P95 ≤250ms、读取笔记 / 列表 P95 ≤500ms、文字检索 P95 ≤1500ms。预期的跨账号 404 不计为错误。内存变化需结合采样结果判断，不能仅凭一次快照断言无泄漏。

脚本只接受回环 API 和本机非 production 数据库配置；需要 httpx / SQLAlchemy 等项目依赖及 `docker`、`lsof`、`ps`。报告写临时目录且权限为 0600，不包含令牌、用户正文或响应错误体。结束或中断会清理其合成账号、附件、记忆 bank 和检查点；清理失败标为 incomplete。测试账号 SQL 清理仅对 PostgreSQL 死锁 / 序列化冲突有界重试。

这是当前开发服务的一段明确时长、轻量并发稳定性检查，不是生产部署、容量上限、全天运行、实体手机或灾难恢复验收。报告必须按实际持续时间和完成状态说明结果；运行中不能写成通过。
