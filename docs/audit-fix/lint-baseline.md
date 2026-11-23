# 静态分析基线（lint baseline）

> 目的：把「CI 里的 lint 到底拦不拦」这件事变成可度量的决定，而不是长期挂着
> `continue-on-error: true` 假装有门禁。
>
> 采集命令（在 `backend/` 下，`python -m` 形式避免 PATH 问题）：
> ```bash
> python -m ruff check app --statistics
> python -m mypy app --ignore-missing-imports
> python -m bandit -r app -q -f json
> XDG_CACHE_HOME=../.piptmp/pip-audit-cache python -m pip_audit -r requirements.txt
> ```
> 采集时间：2026-11-23（本机 CPython 3.14.4 / MySQL 8.4.13）

---

## 1. 基线数字

| 工具 | 存量 | 本轮处理后 | CI 地位 |
| --- | --- | --- | --- |
| ruff（默认规则集） | **212** 条 | 212（未清理，见下） | **棘轮守卫（阻塞）** |
| ruff（棘轮规则集 `E9,F63,F7,F82`） | 1 条（`F821 Undefined name Any`） | **0** | **阻塞门禁** |
| mypy | **13** errors / 9 files | 13 | **棘轮守卫（阻塞）** |
| bandit | 9（**3 HIGH**） | **6（0 HIGH）** | advisory |
| pip-audit | 无法采集 | 无法采集（本机网络到 pypi.org 不通） | advisory |

### ruff 存量明细（前 11 类）

```
133  B008   函数调用作为默认参数（FastAPI 的 Depends/Query 惯用写法，误报为主）
 16  UP037  quoted-annotation
 15  BLE001 blind-except
 12  F401   unused-import
 11  I001   unsorted-imports
  8  RUF100 unused-noqa
  4  UP035  deprecated-import
  4  RUF022 unsorted-dunder-all
  2  SIM102 collapsible-if
  2  UP017  datetime-timezone-utc
  1  ISC004 implicit-string-concatenation
  1  FURB188 slice-to-remove-prefix-or-suffix
```

其中 **B008 占 62%**，且几乎全是 FastAPI 依赖注入的标准写法
（`db: Session = Depends(get_db)`），属于需要 per-file-ignore 或换规则的噪音，
不宜按条修改。

### mypy 13 条（按文件）

分布在 9 个文件，主要是 SQLAlchemy `Result` 行解包的类型推断、以及
`list[dict]` 传给 `list[BaseModel]` 的位置。修复需要逐个补 `cast`/`TypedDict`，
属于纯类型层面工作。

### bandit：本轮已把 3 个 HIGH 清零

| 修复前 | 位置 | 处置 |
| --- | --- | --- |
| B324 弱 SHA1 | `models/knowledge_graph.py:66` | 三元组指纹，非安全用途 → 显式 `usedforsecurity=False` |
| B324 弱 MD5 | `services/kg_service.py:90` | 字符串→桶索引，非安全用途 → 显式 `usedforsecurity=False` |
| B324 弱 SHA1 | `services/literature_service.py:190` | 检索词脱敏哈希，非安全用途 → 显式 `usedforsecurity=False` |

剩余 6 条（0 HIGH）：

| 严重度 | 规则 | 位置 | 判断 |
| --- | --- | --- | --- |
| LOW | B106 | `api/v1/auth.py:117` | `token_type="bearer"` 字面量，误报 |
| MEDIUM | B310 | `llm.py:159`、`literature_service.py:143` | `urlopen` 的目标 URL 由配置拼装、scheme 固定为 http(s) |
| LOW | B110 | `llm.py:166` | 有意的降级分支（LLM 不可用时不打断主流程） |
| LOW | B405 | `literature_service.py:24` | 解析 PubMed 返回的 XML |
| MEDIUM | B314 | `literature_service.py:231` | `ElementTree.fromstring`：默认不做实体扩展，无 XXE |

### pip-audit 为什么采集不到

`pip-audit` 需要访问 `pypi.org` 解析依赖与漏洞库；本机到 pypi.org 超时，
只能走镜像，而镜像没有 `torch==2.14.1+cpu` 这个本地版本标识：

```
ERROR: Could not find a version that satisfies the requirement torch==2.14.1+cpu
       (from versions: ..., 2.14.1, 2.15.0)
```

在能直连 PyPI 的环境（含 GitHub Actions）里执行：

```bash
pip install pip-audit
pip-audit -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
```

---

## 2. 决定：ruff 立即阻塞，其余 advisory

**已经落地的阻塞门禁**（`ruff.toml` 的棘轮配置）：

```toml
[lint]
select = ["E9", "F63", "F7", "F82"]   # 语法错误 / 无效比较 / 未定义名称
exclude = ["alembic/versions", "scripts"]
```

这套规则集**当前 100% 通过**，且能拦住最严重的一类问题——未定义名称
（真正会在运行时抛 `NameError`）。本轮它就抓到一个真实缺陷：
`wrong_question_service.py` 里用了 `Any` 却没导入（在 H6 改造中引入）。

**存量棘轮守卫（阻塞）**：`scripts/check_lint_baseline.py` 会实际运行

```bash
ruff check app --isolated --statistics    # 默认规则集，当前 212
mypy app --ignore-missing-imports         # 当前 13
```

并与 `backend/lint-baseline.json` 比较：**当前数 > 基线即失败**（列出超出的
规则/文件），**当前数 < 基线则提示 `--update` 收紧**。这样存量即使一时清不完，
也保证不会继续变多。

> 注意：ruff 必须带 `--isolated`。项目 `ruff.toml` 只启用棘轮规则集，
> 数出来是 0；若基线误用项目配置，守卫会永远通过。已有测试锁定这一点。

**仍为 advisory**：bandit 剩余 6 条、pip-audit（本机无法采集）。
原因：它们的存量需要逐条改代码而不是改配置，直接阻塞会让 CI 立刻红灯。

**升级路径（只增不减）**：

1. `F401`（12 条 unused-import）→ `ruff check --fix` 后加进 `select`；
2. `I001`（11 条 import 排序）→ 同上；
3. 为 `B008` 加 per-file-ignore（FastAPI 路由文件）后启用剩余 bugbear 规则；
4. mypy 清零 9 个文件的 14 条后，把 `lint-advisory` 里的 mypy 拆成阻塞 job；
5. bandit：保持 advisory，但**新增 HIGH 必须修**（已在 CI 注释里写明）。

---

## 3. CI 现状

`.github/workflows/backend-ci.yml` 现有三个 job：

| job | 阻塞？ | 内容 |
| --- | --- | --- |
| `test` | **是** | 空库 `alembic upgrade head` → `alembic check` → `compileall` → 全量 pytest |
| `lint-blocking` | **是** | `ruff check app`（棘轮规则集，当前 0 条） |
| `lint-advisory` | 否 | `mypy` / `bandit` / `pip-audit`，仅报告 |

`lint-advisory` 的 `continue-on-error: true` 只有在上面「升级路径」全部完成后才会删除。
