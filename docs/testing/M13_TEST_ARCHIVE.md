# M13 Test Archive

M13 把一次性的测试输出变成可审计的质量证据。每次 CI 会保留三种文件：

- `junit-<run_id>.xml`：测试平台通用的逐用例原始结果；
- `test-archive-<run_id>.json`：程序可读取的完整归档；
- `test-archive-<run_id>.md`：人可以直接阅读的测试报告。

## 本地运行

```bash
poetry run pytest --junitxml=data/evaluation/test-archive/junit.xml
poetry run pyright
poetry run python eval/rag_layer_smoke.py
poetry run python -m evaluation.test_archive build \
  --junit data/evaluation/test-archive/junit.xml \
  --scenario-config config/quality_scenarios.toml \
  --output-dir data/evaluation/test-archive \
  --pytest-status passed \
  --pyright-status passed \
  --rag-status passed \
  --run-id local
poetry run python -m evaluation.test_archive verify \
  data/evaluation/test-archive/latest.json
```

归档属于运行产物，默认不提交 Git。GitHub Actions 会将它保存 30 天。

## 关键函数

### `parse_junit(path)`

输入是 JUnit XML 路径，输出是 `list[TestCaseResult]`。

```python
parse_junit(Path("junit.xml"))
# [
#   TestCaseResult(
#       node_id="tests/test_demo.py::test_ok",
#       file="tests/test_demo.py",
#       name="test_ok",
#       status="passed",
#       duration_seconds=0.01,
#   )
# ]
```

### `scenario_evidence(scenarios, cases)`

输入是业务场景目录和实际执行用例，输出是每个需求的测试证据。它解决“测试很多，但不知道是否覆盖目标业务”的问题。

```python
# 输入场景：100 Session 并发
# 匹配规则：tests/test_m13_quality_archive.py::*hundred_sessions*
# 输出：covered=True, passed=True, matched_count=1
```

### `build_archive(...)`

输入：JUnit、场景配置、Pyright/RAG/pytest 状态、commit 和 run id。

输出：包含运行环境、测试总数、逐用例结果、最慢用例、场景覆盖和最终门禁的字典。只要出现以下任意一种情况，`quality_gate_passed` 就是 `false`：

- pytest 有 failed/error；
- Pyright 或 RAG smoke 失败；
- 必选业务场景没有匹配到测试；
- 必选场景匹配到的测试失败。

### `write_archive(archive, output_dir, junit_path)`

输入归档对象、输出目录和原始 JUnit；输出本次 JSON 文件路径。同时生成 immutable 文件和便于程序读取的 `latest.json/latest.md`。

### `verify_archive(path)`

输入 `latest.json`，输出布尔值。CLI 根据它返回 0/1，让 GitHub CI 真正被质量门禁阻断。

### `score_proactive_cases(cases)`

输入是人工标注的主动决策样本，输出六项业务指标：

```python
{
  "precision_of_push": 0.8,
  "miss_rate": 0.1,
  "duplicate_rate": 0.0,
  "ack_accuracy": 1.0,
  "quiet_hour_violations": 0,
  "passive_interference": 0,
}
```

`ACK Accuracy` 比较的是 ACK 与真实投递结果是否一致：发送成功却没 ACK 是错；发送失败却 ACK 了同样是错。

## CI 为什么使用 `continue-on-error`

这里不是忽略失败。pytest、Pyright 和 RAG smoke 先各自跑完并记录 outcome；即使其中一项失败，后面仍然生成、上传归档。最后 `verify` 再统一使 CI 失败。这样失败现场不会因为流水线提前停止而消失。

真实 Telegram、LLM 限流和外部 MCP 故障具有网络依赖，场景目录中标记为 `required = false`，留给 Nightly；它们不会伪装成本地确定性测试。
