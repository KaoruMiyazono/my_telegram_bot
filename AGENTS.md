# AGENTS.md

## 工具链
- Python 3.11+, Poetry 管理依赖
- DeepSeek API (openai 兼容接口), 阿里云 DashScope Embedding
- sqlite-vec 向量检索
- 代理: http://127.0.0.1:7897

## 代码规范
- 所有设计决策先参考 ~/akashic-agent 源码，再执行
- 变量/方法名对齐 akashic-agent 命名
- Pipeline 5 阶段不能跳，eval 不能挂载在 pipeline hook 里
- source_ref 格式: "session:{user_id}:{chat_id}"
- 每次 eval 必须 --fresh 清理 DB
- 基准 eval 必须跑真实被动回复链: haystack replay → consolidation → invalidation → PassiveTurnPipeline QA
- 不使用 `--mock`；`eval/runner.py` 只作为 legacy seeded regression，不代表真实链路

## 关键命令
```bash
# 启动 Bot
python main.py

# Eval（真实被动回复链）
HTTP_PROXY=http://127.0.0.1:7897 HTTPS_PROXY=http://127.0.0.1:7897 python eval/replay_runner.py --set both --fresh --trace --output data/evaluation/results/replay_live_current.json
HTTP_PROXY=http://127.0.0.1:7897 HTTPS_PROXY=http://127.0.0.1:7897 python eval/replay_runner.py --set green --fresh --trace --limit 3 --output data/evaluation/results/replay_live_smoke.json

# Legacy seeded eval（只作历史回归参考）
python eval/runner.py --fresh --compare data/evaluation/baseline.json

# 测试
python tests/test_pipeline_integration.py
python tests/test_metrics.py
```

## 参考项目
~/akashic-agent (uv .venv, 无 pip)
参考重点: eval/longmemeval/ (CLI + metrics + dataset + ingest + qa_runner)
