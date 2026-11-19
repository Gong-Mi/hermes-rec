# 独立运行时行为测试

这组用例独立于官方 `tests/` 和 `scripts/run_tests.sh`，不替代官方测试。基线为 NousResearch/hermes-agent 的 `31ce88f86e6cf4a66ef6aa315e4657a31bcf9915`。

在仓库根目录，用已装好该版本 Hermes 核心依赖的 Python 运行（无需 pytest）：

```sh
python3 -IB behavior_tests/run.py
python3 -IB behavior_tests/run.py test_receipt
```

每组在单独子进程中运行，有自己的临时工作目录、HOME、Hermes home 和数据库，不继承模型凭据。测试进程不连现有 Gateway，不启动平台服务、不调用模型。只有回执组用本机回环 WebSocket。临时目录结束后自动清理；失败、缺组、跳过和超时不算通过。

| 组 | 断言 | 替身边界 |
| --- | --- | --- |
| `test_admission` | 观察者排队时离开、认领后离开，并不会撤销同一条持久受理；授权取消队列项则不认领、不执行 | 真实 SessionAuthority、API 等待与 SQLite；挂载测试消息会话，无平台或模型调用 |
| `test_receipt` | 独立读库已见提交时，worker 仍可能没有回执；断线后重取同一笔不重复写，同序异载荷被拒绝 | 真实 WorkerRPC、回环 WebSocket、SQLite 事务；发现、票据及服务端入口是夹具 |
| `test_replay` | 发布接受不等于观看端接收；带游标重连补缺帧，无游标新连只收后续帧 | 真实会话连接逻辑、回放环和扇出；观看端为测试接收器 |
| `test_cron_handoff` | 同样的“没收到 ACK、子进程退出 0”，可能是已完成，也可能从未接管；读同一执行的账本区分 | 真实派发、等待和账本；子进程只写测试标记，不启动 systemd scope 或真实任务 |

本分支额外的 Actions 工作流只接收 `research/behavior-contracts-20261119` 的 push，不改官方工作流或默认分支。这里验证的是组件间的状态边界，不是生产端到端、真实故障概率或平台全覆盖。
