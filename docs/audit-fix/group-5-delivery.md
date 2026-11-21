# 组 5 修复报告：工程流程 / 交付完整性 / 死抽象

> 来源：审计报告 **H8（流程）、H9（交付）、L10（忽略项）、L15/C11（死抽象）**，
> 以及耦合项 **C1–C3** 的处置规划。
> 结论：**4 项已修复，1 项（H8 的初始提交）需人工执行，耦合重构列为规划**。
> 回归：`640 passed, 1 skipped`（组 4 后 624 → 组 5 后 640）。

---

## 一、修复内容

### H8：工程流程（已修复可自动化部分）

| 项 | 处理 |
| --- | --- |
| 无 CI | 新增 `.github/workflows/backend-ci.yml`：MySQL 8 服务 + 安装依赖 + **空库执行 `alembic upgrade head`** + **`alembic check`** + 全量 `pytest` |
| 依赖 CVE 未扫描 | CI 增加 `pip-audit`（`lint` job，暂 `continue-on-error`） |
| ruff/mypy/bandit 未安装 | 同上，先在 CI 中引入，存量清理完再改为阻塞 |
| 测试通过依赖本机 | CI 显式设置 `DATABASE_URL`/`JWT_SECRET_KEY`/`ST_ENABLED=false`，不依赖任何本机状态 |
| 门禁形同虚设的风险 | 加测试断言 `test` job **不含** `continue-on-error`、CI 真的跑 `pytest` 与迁移 |
| **master 零提交** | ⚠️ **需你手动执行一次**（见下） |

`test` job 的关键设计：CI 里 `ST_ENABLED=false`，语义测试自动降级为编辑距离，
**不会**因为下载 458MB 模型而挂；真正的语义能力已由本机全量测试覆盖。

### H9：交付完整性（已明确范围）

审计的核心质疑是「`frontend/src` 为空，若验收含前端则阻塞交付」。
在无法凭空补出四个主流程 UI 的前提下，正确做法是**把范围写清楚**：

- 新增根 `README.md`，首段即声明：交付范围是**后端 API + 一个单页文献校对界面**，
  `frontend/` 为空，复习/错题/实验/考点四个流程**没有 UI**；
- 加测试锁定该声明（`交付范围` 与「没有前端 UI」必须存在），防止文案漂移；
- 同时补 `LICENSE` 缺失说明。

> 若验收要求完整 UI，这是一项独立排期的工作量，不是「修个 bug」能覆盖的。

### L10：忽略项

`.gitignore` 补充 `.cachebro/`（本机实际存在一个 9MB 的 `cache.db`）、`.omo/`、
`__pycache__` 之外的工具缓存目录、覆盖率产物等；加测试逐项断言。

### L15 / C11：死抽象

`app/services/protocols.py` 定义了 `SchedulingEngine` 与 `CardRepository`，**无人实现、无人引用**。

- 保留 `SchedulingEngine`，并用 `isinstance()` 结构校验**真正绑定**到 `ReviewScheduler`；
- 删除 `CardRepository`：它假设了面向对象的仓储风格，而本项目服务层是**函数式**的，
  这个接口永远不可能被满足；
- 新增测试：`protocols` 里残留的每个接口，其方法都必须在真实实现上存在。

---

## 二、需要你手动完成的一步（H8）

仓库目前仍有 **零提交**，这会导致：无法回滚、无法审计、无 PR 流程、CI 也不会被触发。
提交属于仓库写操作，我没有替你执行。建议：

```bash
cd /home/asus/projects/pharmacy-learning-platform
git add -A
git commit -m "chore: initial import (backend API + audit fixes)"
git remote add origin <你的远端>
git push -u origin master
```

推送后请到托管平台开启**主分支保护**（要求 PR + CI 通过）。

测试里有一条会检测提交数：有提交则断言 >0，无提交则 `skip` 并指向本节
（因此它不会假装问题已解决）。

---

## 三、耦合项 C1–C3：为什么本组没有重构

| 编号 | 审计判断 | 我的核实 | 处置 |
| --- | --- | --- | --- |
| C1 | schemas 反向依赖 service | **不成立**：`schemas/` 只依赖 `limits`/`timeutils`，无 service 依赖 | 无需处理 |
| C2 | 嵌入逻辑散落三处 | **部分成立**：`kg_service`（离线哈希）与 `embeddings`（ST）确实是两套 | 已在组 4 用「嵌入签名」显式化并安全重建；**统一为一套**需要重算全部向量，属架构决策 |
| C3 | exam ↔ wrong_question 双向依赖 | **成立**：`exam_match_service` 反向调用 `wrong_question_service` | 组 2/3 已把交互收敛为 `related_card_ids_bulk` + `MasteryContext` 两个明确入口；抽公共层属重构，收益低于风险 |

结论：**C1 不成立**，C2/C3 已把「最痛的部分」收敛，剩余重构建议单独排期。

---

## 四、修复后测试情况

新增回归 **17 项**（`tests/test_group5_delivery.py`），1 项 skip（无提交，符合预期）：

| 覆盖点 | 用例 |
| --- | --- |
| `SchedulingEngine` 真的被实现 | 1 |
| `CardRepository` 已删除 | 1 |
| protocols 无未实现残留 | 1 |
| CI 存在 / 跑测试 / 跑空库迁移 / 用 MySQL | 4 |
| CI test job 不能 `continue-on-error` | 1 |
| CI 必须设置真实 `JWT_SECRET_KEY` | 1 |
| 根 README 声明交付范围 | 1 |
| 后端 README 不再说「启动自动建表」 | 1 |
| `.gitignore` 覆盖 7 类目录 / 密钥与向量库 | 8 |
| 提交状态可检测 | 1（skip） |

全量回归：

```
640 passed, 1 skipped in 247s      # 组 4 后：624 passed
```

五个验收脚本：`24 / 26 / 29 / 35 / 31`，全 0 失败。

---

## 五、未处理项（如实列出）

| 编号 | 问题 | 原因 |
| --- | --- | --- |
| H8 | 初始提交 / 远端 / 分支保护 | 需你执行（见第二节） |
| H9 | 四个主流程的前端 UI | 独立排期的工作量，非缺陷修复 |
| M13 | `hf-mirror` 默认下载源、`torch` 未锁版本 | 影响依赖可复现性，建议锁版本 |
| M15 | 多 worker 下的单例（限流、嵌入、Chroma） | 单进程部署无影响；多 worker 需 Redis/共享缓存 |
| M16 | PubMed 无响应体积上限、日志含检索词 | 低风险，建议后续补 |
| M17 | 前端 token 存 `localStorage`、无 CSP | 单页校对界面，风险可控 |
| C2 | 两套嵌入统一 | 需重算全部向量，建议单独立项 |
