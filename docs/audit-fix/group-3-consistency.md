# 组 3 修复报告：数据一致性与逻辑错误

> 来源：审计报告 P2 中 **M1、M2、M3、M4、M5、M6、M7、M8、M18、M19**。
> 结论：**10 项全部真实存在，已全部修复**（其中 M2 是我在阶段二重构时自己引入的缺陷）。
> 回归：`606 passed, 0 skipped`（组 2 后 586 → 组 3 后 606）。

---

## 一、逐项定位与验证

| 编号 | 问题 | 验证证据 | 结论 |
| --- | --- | --- | --- |
| M1 | 评分无幂等、`review_logs` 无唯一约束 | 表无唯一键；`grade_card` 先算后写，无任何去重 | 属实 |
| M2 | `llm.py` 重复定义 `complete_json` | `grep` 命中 94 行与 112 行两个 `def complete_json`，前者被覆盖成死代码 | **属实（我引入的）** |
| M3 | 用 `max(created_at)` 当最后错误时间 | `wrong_question_service.py:645` `func.max(WrongQuestion.created_at)` | 属实 |
| M4 | 知识点未归一化就聚合 | `error_aggregates` 按数据库原值 `group_by`，`" 阿司匹林。"` 与 `"阿司匹林"` 会拆成两个弱项 | 属实 |
| M5 | LIKE 通配符未转义 | 4 处 `f"%{...}%"` 直接拼进 `ilike` | 属实 |
| M6 | `reviewed_at`/`elapsed_days` 任意可控 | 服务端完全信任客户端传值；可倒退、可伪造未来 | 属实 |
| M7 | `to_cards` 无去重 + 死分支 | `back = suggestion.back` 两个分支取值相同；重复点击重复建卡 | 属实 |
| M8 | 知识图谱无唯一约束 | `confirm_triples` 仅靠应用层去重，并发会重复 | 属实 |
| M18 | 转卡片绕过 Pydantic 校验 | `literature.py` 直接调 `card_service.create_card`，LLM 生成的超长内容可入库 | 属实 |
| M19 | 删考点不清卡片 | `delete_exam_point` 只删 ExamPoint 行 | 属实 |

### M2 的影响（值得单独说明）
```python
def complete_json(...):   # 第 94 行，docstring 写的是「返回原始文本」
    ... parse_json=False
def complete_json(...):   # 第 112 行，覆盖上面的定义
    ... parse_json=True
```
Python 后定义者生效，所以 `complete_text` 这个公共方法**根本不存在**，
而第一个函数成了永远不可达的死代码。维护者改上面那个函数会「改了不生效」。

---

## 二、解决方案

| 编号 | 修复 |
| --- | --- |
| M1 | `review_logs (card_id, reviewed_at)` 唯一约束；`grade_card` 用 SAVEPOINT 捕获 `IntegrityError` → 409 `conflict`，重复提交不会二次推进 FSRS |
| M2 | 第一个函数改名为 `complete_text`；补测试锁定「只能有一个 complete_json」 |
| M3 | 新增 `wrong_questions.last_error_at`；创建与 `increment_error` 都刷新；聚合改用 `COALESCE(last_error_at, created_at)`；迁移回填历史 |
| M4 | 聚合前先 `normalize_knowledge_point`；`summarize_weak_points` 内部也按归一化 key 合并计数/卡片/时间 |
| M5 | 新增 `limits.escape_like()` / `like_contains()`，全部 `ilike` 加 `escape`；共修正 5 处查询 |
| M6 | `elapsed_days` **一律服务端计算**（忽略客户端）；`reviewed_at` 不得早于上次复习、不得超 `REVIEW_MAX_FUTURE_DAYS`（默认 365）；路由把 `ValueError` 映射为 422 |
| M7 | 删除死分支；转卡片按 `front` 去重，重复点击 `created=0` |
| M8 | `knowledge_graph.fingerprint`（sha1）+ `(user_id, fingerprint)` 唯一约束；`confirm_triples` 逐条 SAVEPOINT，撞约束计为 skipped；`before_insert` 事件自动补指纹，避免所有调用方都要记得算 |
| M18 | `card_service.create_card` 内部统一 clamp（覆盖所有绕过 Pydantic 的路径） |
| M19 | `delete_exam_point` 按 `【考点】<point_text>` 精确匹配并清理生成的卡片 |

### 迁移安全（重要）
自动生成的迁移是**危险的**：直接 `ADD COLUMN ... NOT NULL`（非空表会失败）、
直接加唯一约束（已有重复数据会失败）。已改写为：

```
加可空列 → 回填（SHA1 / created_at）→ 按 key 去重 → 收紧为 NOT NULL → 加唯一索引
```

并在**含重复数据的真实库**上验证过：

| 验证项 | 结果 |
| --- | --- |
| 重复三元组 2 条 | 去重为 1，fingerprint 回填成功 |
| 重复评分日志 2 条 | 去重为 1，唯一约束建立成功 |
| `last_error_at` | 由 `created_at` 回填 |
| 迁移后原有行 | 全部保留（无数据丢失） |

---

## 三、修复后测试情况

新增回归 **20 项**（`tests/test_group3_consistency.py`）：

| 覆盖点 | 用例 |
| --- | --- |
| `complete_json` 只定义一次 / `complete_text` 存在且不解析 JSON | 3 |
| `last_error_at` 创建即写、`increment_error` 推进、聚合取最新 | 3 |
| 归一化变体合并为同一弱项 | 1 |
| `%` / `_` 通配符不再匹配全部卡片 | 2 |
| 复习时间不可倒退 / 不可过远未来 / 客户端 `elapsed_days` 被忽略 | 3 |
| 同一时刻重复评分 → 409；唯一约束存在 | 2 |
| 指纹自动填充、唯一约束存在、重复三元组计为 skipped | 3 |
| `create_card` 服务层 clamp | 1 |
| 删考点级联删卡片 | 1 |
| 转卡片幂等 | 1 |

全量回归：

```
606 passed, 0 skipped in 112s      # 组 2 后：586 passed
```

五个验收脚本：`24 / 26 / 29 / 35 / 31`，全 0 失败。

---

## 四、修复过程中新发现并修掉的 3 个问题

1. **`KnowledgeGraph` 直接构造会违反 NOT NULL**：加指纹列后，测试/脚本里直接 `KnowledgeGraph(...)`
   插入会 `IntegrityError`。改为 `before_insert` 事件自动补指纹，而不是要求每个调用方记得算。
2. **`summarize_weak_points` 不合并变体**：M4 的合并原先只在数据库聚合路径生效，
   纯函数入口传原始 key 仍会拆开。已把合并下沉到纯函数，保证单一聚合口径。
3. **业务校验被兜成 500**：`grade_card` 抛 `ValueError` 后经 `api_guard` 变成 500。
   已在路由层映射为 422。

---

## 五、遗留

- **M8 的「导入前查重」只做了三元组**：`exam_points` 重复导入仍会产生多条记录
  （语义上允许同一考点多次导入，但会让统计膨胀）。是否要加「同文本去重」需产品决策。
- **M7 的 Chroma/MySQL 非原子**：确认流程已改为「先写 DB 再改草稿状态」，
  崩溃时最多留下「已入库但草稿仍 pending」，重跑确认会被唯一约束/去重挡住，不会重复。
  完整的补偿/对账任务未做。
- M1 只覆盖了「同一时刻」的重复；跨秒的双击仍会推进两次（幂等键 Idempotency-Key 未做）。
