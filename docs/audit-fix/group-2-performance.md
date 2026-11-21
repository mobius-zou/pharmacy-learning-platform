# 组 2 修复报告：事件循环阻塞 / 批量 LLM 放大 / 考点匹配 N+1

> 来源：外部审计报告 `后端全量测试通过.docx` 的 **P1 高风险** H4、H5、H6。
> 结论：**3 项全部真实存在，已全部修复**。
> 回归：`586 passed, 0 skipped`（组 1 后 575 → 组 2 后 586）。

---

## 一、问题定位（验证结果）

| 编号 | 报告结论 | 验证 | 结论 |
| --- | --- | --- | --- |
| H4 | `async def` 端点内同步调用 OCR（subprocess）与 DB/嵌入，阻塞事件循环 | `exam_points.py` 的 `import_from_image` / `import_from_file` 均为 `async def`，内部直接调 `extract_text_from_image`（同步 subprocess）与 `_import_text`（同步 DB + 嵌入） | **属实** |
| H4 附带 | OCR 每次枚举语言包 | `TesseractOCR.extract_text` 每次调用 `installed_languages()` → 每次起一次 `tesseract --list-langs` 子进程 | **属实** |
| H5 | 批量录入每行最多 2 次 LLM + 每行一次 `known_knowledge_points` 全表扫描 | `create_wrong_question` 在知识点为空时调 `identify_knowledge_point` + `generate_socratic_question`；`create_wrong_questions_bulk` 逐行调用且每行内部重算 `known` | **属实** |
| H6 | `mastery_evidence` 对每个考点各查关联卡片 + 错题聚合 + 全量薄弱点 | 每个考点执行 3~4 条查询，其中 `related_card_ids` 是 `JSON_CONTAINS + ILIKE` 全表扫描，`list_weak_points` 又做一次全量聚合 | **属实** |

### 影响量化（H6）
导入 N 条考点时，读查询数 ≈ 3N；其中 `related_card_ids` 每次都是全表扫描。
真实数据量下（几百条考点 × 上千卡片）导入会显著变慢并抬高数据库 CPU。

---

## 二、解决方案

### H4：把同步重活移出事件循环
- `import_from_image` / `import_from_file` 保持 `async def`（需要 `await file.read()`），
  但把 **OCR 识别** 与 **导入落库** 都用 `starlette.concurrency.run_in_threadpool` 包起来；
- 原先这两处直接跑在事件循环上，单次 OCR 数秒会让同 worker 的**所有**请求超时；
- OCR 语言包列表加缓存（`_languages`），避免每次识别都起子进程。

### H5：批量导入不再放大 LLM
- `create_wrong_question` 新增两个参数：
  - `known_points`：由批量入口算一次传进来，避免每行全表扫描；
  - `use_llm`：批量默认 **False**，强制走本地启发式（知识点识别 + 引导问题都用模板）；
- 新增 `_NoLLMClient` 替身实现「强制降级」，保证 `use_llm=False` 时不会误调模型；
- 批量入口限制行数（`WRONG_QUESTION_BULK_MAX_LINES`，默认 500），超限截断并回报；
- 单条录入行为不变（仍可走 LLM）。

> 取舍：批量导入默认不调 LLM，识别质量略降，但结果本来就需要**用户确认**（阶段二设计如此），
> 而 100 行 → 200 次 LLM 调用的代价不可接受。需要更高精度时可显式 `use_llm=True`。

### H6：掌握度证据一次预取
新增 `MasteryContext` + `build_mastery_context()`：

- 固定 4 条查询：薄弱点集合、关联卡片（复用组外已有的 `related_card_ids_bulk`）、
  卡片统计、错题聚合；
- `evidence(knowledge_point)` 变成**纯内存查表**；
- `import_exam_points` / `preview_matches` / `review_list` 三条路径各建一次上下文；
- 保留 `mastery_evidence()` 单点接口（内部复用批量上下文），保证调用方兼容。

---

## 三、修复后测试情况

新增回归 **11 项**（`tests/test_group2_performance.py`）：

| 覆盖点 | 用例 | 关键断言 |
| --- | --- | --- |
| OCR 语言包缓存 | 1 | 连续两次调用只枚举一次 |
| 图片导入的 OCR 走线程池 | 1 | 记录 `run_in_threadpool` 的调用函数名 |
| 文件导入走线程池 | 1 | 同上 |
| 批量错题默认走启发式 | 1 | 10 行全部生成、知识点非空 |
| `known_knowledge_points` 只算一次 | 1 | 调用次数 == 1（原先 20 行 → 20 次） |
| 批量行数上限 | 1 | 20 行输入 → 5 条 + 明确提示 |
| 显式知识点不触发识别 | 1 | 直接采用 |
| **掌握度上下文查询数恒定** | 1 | 2 → 6 个知识点，查询数不变且 ≤5 |
| 批量上下文与单点查询一致 | 1 | 5 个字段逐项相等 |
| 未知知识点为空 | 1 | 全 0 / 无薄弱点 |
| **导入考点读查询恒定** | 1 | 2 → 6 条考点，SELECT 数不变（写入按条数必要增长） |

全量回归：

```
586 passed, 0 skipped in 114s      # 组 1 后：575 passed
```

五个验收脚本：`24 / 26 / 29 / 35 / 31`，全 0 失败。

---

## 四、遗留

- **H6 的「生成列 + 索引」建议未做**：`JSON_CONTAINS`/`ILIKE` 无法走普通索引，
  要真正提速需要把章节/知识点抽成生成列并建索引。这属于 schema 演进，
  建议单独立项（当前已通过批量化把 N+1 消除，收益最大的一步已完成）。
- H4 只保证「不阻塞事件循环」，未做 OCR 独立进程/队列（审计建议的进阶方案），
  当前线程池方案在单机并发下够用。
- 限流/LLM 配额仍是进程内实现（M11、M12 未在本组）。
