# 组 4 修复报告：输入校验 / 配置健壮性 / 规范性

> 来源：审计报告 **M10、M11、M12、M14** 与低风险 **L1、L3–L7、L14、L17–L19**，
> 外加修复过程中连带发现的 **#20（500 响应泄露异常类名）**。
> 结论：**14 项真实存在，已全部修复**。
> 回归：`624 passed, 0 skipped`（组 3 后 606 → 组 4 后 624）。

---

## 一、修复清单

| 编号 | 问题 | 修复 |
| --- | --- | --- |
| M10 | 多个入参无长度上限 | 补 `LoginRequest`、`knowledge_point`、`socratic_question`、`TripleIn`（对齐列宽 200）、`ConfirmRequest.source_id`（64）、`error_count`/`delta` 上限、标签数量与长度上限、`max_points` 与服务的 200 对齐 |
| M11 | 注册无限流 | 新增注册限流器（默认 20 次/5 分钟/IP），批量刷号会触发大量 bcrypt 哈希，属 CPU 放大 |
| M12 | 配置无自检 | 新增 `validate_settings()`：算法白名单、过期时间、CORS 混用 `*`、限流阈值、multipart 与 body 上限关系；启动时逐条 `error` 输出 |
| M14 | 文献向量库与考点匹配用了两套嵌入空间 | 集合元数据记录 `embedding_backend`/`embedding_dim`；签名不一致才重建，历史集合（无签名）**保留数据只补签名** |
| L1 | `assert` 做业务校验（`-O` 下被移除） | 改为显式判空返回；并加静态测试禁止 `app/` 出现裸 `assert` |
| L3 | `schemas/types.py` 中部 import | 移到文件顶部 |
| L4 | `format_interval(days: float)` 内有 `if days is None` | 注解改为 `float \| None` |
| L5 | `username` 同时 `unique=True` + `index=True` | 去掉冗余 `index`（唯一约束已建索引） |
| L6 | `alembic_version.version_num` 只有 32 字符 | 放宽到 255 |
| L7 | 请求 `max_points` 允许 500，服务只处理 200 | 对齐为 200 |
| L14 | README 称「启动自动建表」 | 改为「迁移为唯一入口，需先 `upgrade head`」 |
| L17 | PATCH 无法清空可空字段 | `if name in fields` 判断（配合路由的 `exclude_unset=True`），显式 `null` 可清空 |
| L18 | 混用 1.x `db.query()` | 改 `delete()`；加静态测试禁止 `db.query(` |
| L19 | 静默 `except: continue` | 补 `logger.warning` 留痕 |

### 连带修复
| 问题 | 说明 |
| --- | --- |
| **#20 500 响应带异常类名** | 原返回 `{"detail": {"exception": "NameError"}}`，等于把内部实现细节告诉调用方。已改为通用文案，堆栈只进日志 |
| **`TagValidationError` 变成 500** | 标签校验失败是**客户端错误**，全局映射为 422 |
| **标签长度上限过紧** | 我最初设为 100 字符，但文献抽取出的知识点是整句英文（80~150 字符），导致「文献转卡片」500。已按 DB 列宽调到 200，并在转卡片时截断 |

---

## 二、组 4 实际踩到的两个坑（值得记录）

### 1. 注册限流把测试与验收脚本自己刷爆
限流按客户端 IP 计数，测试全部来自同一地址：第 6 个用例起直接 429，**181 个用例报错**。
- 测试侧：conftest 增加 autouse fixture，每个用例前重置限流器（测试隔离问题，不是限流逻辑问题）。
- 脚本侧：默认阈值 5 太紧，连跑 5 个验收脚本就会撞墙。默认改为 20 并注明生产可调低。
- 已验证：**连续两轮 5 个脚本全部通过**。

### 2. Chroma 自定义嵌入函数未注册
`_collection()` 对已存在的集合**直接返回**，从未构造嵌入函数，因此注册副作用从未发生。
一旦 Chroma 需要按名字解析（如 `modify`、重开集合），就抛
`Embedding function pharmacy-offline-hashing not found` ——**持久化集合会读不出来**。
修复：无条件构造一次嵌入函数（注册副作用）+ 显式传入 `get_collection`。

---

## 三、修复后测试情况

新增回归 **18 项**（`tests/test_group4_validation.py`）：

| 覆盖点 | 用例 |
| --- | --- |
| 登录/错题/三元组/标签/考点数量上限 | 5 |
| 注册限流（3 次内 2 成功、第 3 次 429） | 2 |
| 配置自检（错配全部命中 / 默认无问题） | 2 |
| 集合记录嵌入签名 / 重开仍可读 | 2 |
| 签名决策：历史集合补签名保留数据 / 真不一致才重建 | 2 |
| 静态规范：无裸 `assert`、无 `db.query(` | 2 |
| `format_interval(None)` | 1 |
| PATCH 可清空可空字段 / 不清空未传字段 | 2 |

全量回归：

```
624 passed, 0 skipped in 205s      # 组 3 后：606 passed
```

五个验收脚本连跑**两轮**：每轮 `24 / 26 / 29 / 35 / 31`，全 0 失败。

---

## 四、遗留

- **M14 只做到「显式化 + 安全重建」**，没有把两套嵌入统一成一套。要统一需选定一种嵌入
  并为两处同时重建向量索引，属于架构决策，建议单独立项。
- M13（`hf-mirror` 默认下载源、`torch` 未锁定版本）、M15（多 worker 单例）、
  M16（PubMed 无响应大小限制）、M17（前端 token 存 localStorage）未处理。
- L2（`type: ignore`）保留：`@computed_field` 的 `prop-decorator` 忽略是 mypy 已知限制，
  删掉会产生误报。
