# 药学学习平台 —— 后端

- **阶段一：复习面板 MVP** —— FSRS-6 间隔重复调度闭环：录入卡片 → 到期查询 → 评分 → 更新调度。
- **阶段二：错题诊断** —— 错题录入、知识点识别（需用户确认）、薄弱点警报、薄弱点卡片优先复习。
- **阶段三：文献工作台** —— PubMed 检索、PICO-D 结构化提取、知识图谱、**人工校对界面**、知识点一键转卡片。
- **阶段四：实验档案** —— 药学专用 ELN（试剂批号 / 仪器校准 / 偏差与处理）+ **偏差回溯规则引擎**（无 AI）。
- **阶段五：考点匹配** —— 考点导入（文本 / txt / PPT 截图 OCR）、知识点匹配、**高危/巩固/确认三级分类**、自动插入复习队列。

技术栈：Python 3.14 / FastAPI / Uvicorn / SQLAlchemy / PyMySQL / MySQL（`pharmacy_db`）/ py-fsrs 6.x
阶段三新增：LangChain（`LLMGraphTransformer`）/ ChromaDB ≥ 1.5.9 / NetworkX
阶段四：**不引入任何新依赖**，纯规则引擎 + MySQL 查询，参考 AI4Green4Students（诺丁汉大学开源 ELN）。
阶段五新增：sentence-transformers（语义精筛）/ python-multipart（文件上传）/ pytesseract（OCR，需系统二进制）。

> **AI 结果不自动入库**：所有 LLM/规则抽取都先落到 ChromaDB 草稿区，
> 只有人工确认后才写入 MySQL。
> **无 Agent 降级模式**：未配置 LLM 时，PICO-D 与三元组由本地规则实现，
> 全部功能照常可用；PubMed 访问仅用标准库 `urllib`，不需要任何厂商 SDK。

## 快速开始

```bash
cd backend

# 1) 配置环境变量（.env 由你自己维护，不要提交到仓库）
cp .env.example .env
#    编辑 .env，至少填 DATABASE_URL 与 JWT_SECRET_KEY（也兼容 JWT_SECRET / SECRET_KEY）

# 2) 安装依赖
#    requirements.txt 锁定 torch==2.14.1+cpu（本地版本标识，只存在于 PyTorch 的 CPU 源），
#    因此必须带该索引，否则会去 PyPI 拉数 GB 的 CUDA 轮子：
./venv/bin/pip install -r requirements.txt \
  --extra-index-url https://download.pytorch.org/whl/cpu

# 3) 建表（迁移为唯一入口）
./venv/bin/alembic upgrade head

# 4) 启动（**单进程**，原因见下方「部署形态」）
./venv/bin/uvicorn app.main:app --reload

# 文档： http://127.0.0.1:8000/docs
# 健康检查： http://127.0.0.1:8000/health
```

启动时**不会**自动建表：表结构以 Alembic 迁移为唯一入口，请先执行
`./venv/bin/alembic upgrade head`（详见「数据库迁移」一节）。
本地想恢复旧行为可设 `AUTO_CREATE_SCHEMA=true`，但不建议用于生产。

### 部署形态：当前只支持单进程

请**不要**用 `--workers N`（N>1）启动。有三个组件是**进程内单例**：

| 组件 | 位置 | 多进程后果 |
| --- | --- | --- |
| 登录/注册限流计数器 | `app/ratelimit.py` | 实际阈值变成 `配置值 × worker 数`，限流被放大 |
| 嵌入后端（sentence-transformers） | `app/embeddings.py` | 每个 worker 各加载一份模型，内存 × N（单份约 400MB+） |
| ChromaDB 客户端 | `app/services/kg_service.py` | 同一持久化目录被多进程同时打开，可能导致锁冲突 |

要支持多 worker，需要先把限流外置到 Redis、把模型放到独立推理服务。
在此之前，单进程 + 反代（Nginx/Caddy）是受支持的部署方式。

## 目录结构

```
backend/
├── app/
│   ├── main.py              # FastAPI 入口：JWT、日志、统一错误处理、CORS
│   ├── config.py            # 全部配置来自环境变量（无硬编码密钥）
│   ├── database.py          # 引擎/会话（惰性连接）、Base、create_all
│   ├── security.py          # bcrypt 哈希 + JWT 签发/校验
│   ├── deps.py              # 认证依赖、api_guard 异常兜底
│   ├── errors.py            # 统一错误体 {"error","message","detail"}
│   ├── logging_config.py    # 日志 -> backend/logs/app.log
│   ├── tags.py              # 三维度标签体系与校验
│   ├── timeutils.py         # 时间纪律：库里存 naive UTC，出库为 aware UTC
│   ├── llm.py               # LLM 客户端（标准库 urllib，失败即降级）
│   ├── langchain_adapter.py # 标准库 ChatModel -> LangChain（驱动 LLMGraphTransformer）
│   ├── embeddings.py        # 嵌入后端：sentence-transformers + 离线哈希降级
│   ├── ocr.py               # OCR 后端（Tesseract 可插拔，不可用时报 503+指引）
│   ├── models/              # users / cards / review_logs / wrong_questions / knowledge_graph / experiments / exam_points
│   ├── schemas/             # Pydantic 请求/响应模型
│   ├── static/
│   │   └── literature_review.html   # ★ 人工校对界面（自包含单页，无需构建）
│   ├── services/
│   │   ├── review_service.py          # ★ FSRS-6 核心（纯逻辑，可脱离数据库单测）
│   │   ├── card_service.py            # 卡片 CRUD / 到期查询 / 评分落库
│   │   ├── wrong_question_service.py  # ★ 错题诊断 / 薄弱点判定（纯逻辑 + DB 层）
│   │   ├── literature_service.py      # ★ PubMed 检索 + PICO-D 提取
│   │   ├── kg_service.py              # ★ 嵌入 / ChromaDB 草稿区 / 三元组 / NetworkX
│   │   ├── experiment_service.py      # ★ 偏差回溯规则引擎（纯逻辑 + 模板 + DB 层）
│   │   ├── exam_match_service.py      # ★ 考点匹配（分词/同义词/编辑距离/优先级）
│   │   └── protocols.py               # 服务层标准接口（Protocol）
│   └── api/v1/              # 路由：auth / cards / review / wrong-questions / literature / experiments
├── scripts/
│   ├── verify_phase1_loop.py      # 阶段一真机验收（自清理）
│   ├── verify_phase2_loop.py      # 阶段二真机验收（自清理）
│   ├── verify_phase3_loop.py      # 阶段三真机验收（自清理）
│   ├── verify_phase4_loop.py      # 阶段四真机验收（自清理）
│   └── verify_phase5_loop.py      # 阶段五真机验收（自清理）
├── alembic/                 # ★ 数据库迁移（格式版本化的落点）
│   ├── env.py               #   连接串取自 os.getenv("DATABASE_URL")，不硬编码
│   └── versions/            #   迁移脚本
├── alembic.ini
├── tests/                   # pytest
├── requirements.txt
└── .env.example
```

## 主要接口（前缀 `/api/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/auth/register` | 注册 |
| POST | `/auth/login` | 登录，返回 JWT |
| GET | `/auth/me` | 当前用户 |
| POST | `/cards` | 手动录入一张卡片 |
| POST | `/cards/bulk` | 粘贴文本批量录入（`\|`、`::`、`｜`、Tab 分隔） |
| GET | `/cards` | 分页查询（`offset`/`limit`/`keyword`/`subjects`/`chapters`/`point_types`） |
| GET · PATCH · DELETE | `/cards/{id}` | 详情 / 修改 / 删除 |
| POST | `/cards/{id}/reset` | 清空学习进度 |
| GET | `/review/due` | 到期卡片（**薄弱点卡片优先** → 新卡 → 到期升序） |
| GET | `/review/{id}/preview` | 预览四档评分对应间隔 |
| POST | `/review/{id}/grade` | 评分（1=忘了 2=困难 3=一般 4=简单） |
| GET | `/review/{id}/logs` | 复习历史 |

### 错题诊断（阶段二）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/wrong-questions/analyze` | 识别知识点 + 生成引导问题（**不落库**，等用户确认） |
| POST | `/wrong-questions` | 录入错题（可带确认后的知识点） |
| POST | `/wrong-questions/bulk` | 粘贴文本批量录入（`题干 \| 知识点` 可指定） |
| GET | `/wrong-questions` | 分页查询（`knowledge_point`/`keyword`/`weak_only`） |
| GET | `/wrong-questions/weak-points` | **薄弱点警报列表**（`threshold`/`weak_only`） |
| GET · PATCH · DELETE | `/wrong-questions/{id}` | 详情 / 修正知识点 / 删除 |
| GET | `/wrong-questions/{id}/related-cards` | 该知识点自动关联的卡片 |
| POST | `/wrong-questions/{id}/errors` | 再次做错，累加错误次数 |
| POST | `/wrong-questions/{id}/answer` | 回答引导问题并获取解析 |

### 文献工作台（阶段三）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/literature/search` | PubMed 检索（E-utilities） |
| GET | `/literature/pubmed/{pmid}` | 文献详情（并写入向量库） |
| POST | `/literature/extract` | **按 PMID 抽取 PICO-D + 三元组 → 生成待校对草稿（不落库）** |
| GET | `/literature/reviews` | 待校对草稿列表（`status=pending/confirmed/rejected/all`） |
| GET · PATCH | `/literature/reviews/{draft_id}` | 草稿详情 / **人工校对** |
| POST | `/literature/reviews/{draft_id}/confirm` | **确认入库（写入 knowledge_graph）** |
| POST | `/literature/reviews/{draft_id}/reject` | 拒绝草稿（不写库） |
| POST | `/literature/reviews/{draft_id}/to-cards` | **知识点一键转复习卡片** |
| GET | `/literature/graph` | 知识图谱（NetworkX 计算 nodes/edges/stats） |
| GET | `/literature/graph/export` | 导出 `json` / `graphml` / `gexf` |
| GET | `/literature/graph/triples` | 已确认的三元组列表 |
| DELETE | `/literature/graph/triples/{id}` | 删除一条已确认的三元组 |
| GET | `/literature/vector-search` | ChromaDB 向量检索 |

人工校对界面：**<http://127.0.0.1:8000/ui/literature>**（自包含单页，无需 npm 构建，可离线使用）

### 实验档案（阶段四）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/experiments/options` | 实验类型 / 偏差类型枚举 |
| GET | `/experiments/templates` | 全部实验模板（步骤、关键参数、风险点） |
| GET | `/experiments/template/{experiment_type}` | **进入模板时主动提示历史偏差** |
| GET | `/experiments/warnings` | 按实验类型 + 偏差类型 + 药物/试剂相似度召回历史偏差 |
| GET | `/experiments/summary` | 偏差统计（类型交叉汇总、高偏差药物） |
| POST | `/experiments` | **记录实验（自动回溯历史偏差）** |
| GET · PATCH · DELETE | `/experiments/{id}` | 详情 / 修改 / 删除 |
| GET | `/experiments/{id}/traceback` | 对该记录重新执行偏差回溯 |
| GET | `/experiments` | 分页查询（`experiment_type`/`drug_name`/`deviation_type`/`only_deviations`/`keyword`） |

### 考点匹配（阶段五）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/exam-points/capabilities` | 嵌入后端与 OCR 可用性（不加载模型，不阻塞） |
| POST | `/exam-points/match` | **试匹配（不落库）**，先看匹配质量 |
| POST | `/exam-points/import` | **导入考点文本 → 匹配 → 三级分类 → 落库** |
| POST | `/exam-points/import/file` | 从 txt 文件导入（支持 utf-8 / GBK） |
| POST | `/exam-points/import/image` | 从 PPT 截图 / 试卷照片导入（OCR） |
| GET | `/exam-points/review-list` | **针对性复习清单**（按三级分组） |
| GET | `/exam-points/summary` | 优先级分布统计 |
| POST | `/exam-points/to-review-queue` | **高危考点插入 FSRS 复习队列**（幂等） |
| GET · DELETE | `/exam-points/{id}` | 详情 / 删除 |
| GET | `/exam-points` | 分页查询（可按 `priority` 过滤） |

标签过滤语义：**同一维度多值取并集（或），不同维度之间取交集（与）**。

## 错题诊断设计（阶段二）

**知识点识别需用户确认**：`POST /wrong-questions/analyze` 只返回建议（含 `source`、
`confidence`、`alternatives`、`requires_confirmation`），不写库；用户确认或改选后
再 `POST /wrong-questions` 落库。

**无 Agent 降级模式**：未配置 `LLM_API_BASE`/`LLM_MODEL`，或调用超时、报错、
返回非法 JSON 时，知识点识别与引导问题自动退化为本地启发式规则
（`source` 字段标明 `llm` 还是 `heuristic`）。核心业务逻辑不依赖 LLM。

**薄弱点判定**：同一知识点的 `error_count` **累计和** 达到 `WEAK_POINT_THRESHOLD`
（默认 2）即触发警报，`severity` 在达到阈值两倍时升级为 `critical`。
判定逻辑是纯函数 `summarize_weak_points()`，可脱离数据库单测。

**知识点与 cards 自动关联**：不新增关联表，按「三维度标签精确匹配 / 标签 JSON 文本
模糊匹配 / 卡片正反面正文模糊匹配」实时计算，因此标签或正文一改，关联立即生效、不会失效。

**薄弱点优先复习**：只调整到期队列的**顺序**，不改变 FSRS 的到期判定 ——
未到期的薄弱卡片不会被强行提前，以免破坏间隔重复的有效性。

**苏格拉底式提问**：识别问题的三种类型，`preference`（偏好性问题）无标准答案，
作答后不展示解析；`procedural`/`judgment` 有答案。`socratic_question` 与
`user_answer` 存储问答内容；解析材料来自**关联卡片**的正确答案。

## 文献工作台设计（阶段三）

**人工校对是硬性关卡**（AI 抽取准确率约 77%，不可信）：
抽取结果写入 ChromaDB 的 `literature_drafts` 集合，**MySQL 中不会有任何记录**；
只有 `confirm` 接口才会把三元组写入 `knowledge_graph`。因此该表内数据天然全部可信，
无需额外的 `status` 字段。草稿存档需要一个新的持久化位置，选择放在 ChromaDB
而不是新增数据表，既满足「不自动入库」又利用了必需的向量存储。

**PICO-D 提取**：LLM 单次调用优先；未配置 LLM、超时或返回非法 JSON 时退化为
确定性规则实现（研究设计词表 → 句式模板 → MeSH 兜底）。`source` 字段如实标注来源，
LLM 只贡献部分字段时其余字段由规则补齐，若一个字段都没贡献则整体标注为 `heuristic`。

**三元组抽取**：`LLMGraphTransformer`（经 `langchain_adapter.StdlibChatModel` 驱动，
**不需要任何厂商 SDK**）优先；失败时退化为基于 PICO-D 的规则三元组。

**离线确定性嵌入**：ChromaDB 使用自实现的字符/词 n-gram 哈希嵌入，
基于 `hashlib`（**不是** Python 内置 `hash()`，后者每进程随机化，会让持久化向量在重启后失去可比性）。
不下载模型、不联网，中英文混排也可检索。

**知识图谱**：`knowledge_graph` 逐行存三元组，NetworkX 在读取时构建有向图
（并行边合并、累加权重），提供度中心性排序、连通分量、密度等统计与三种导出格式。

**知识点一键转卡片**：知识点建议来自 PICO-D 组合与三元组实体，转卡片时调用
`card_service.create_card`，因此生成的卡片直接进入 FSRS 复习队列；
知识点同时写入章节标签，便于后续按知识点统计薄弱点。

## 实验档案设计（阶段四）

**纯规则引擎，不用 AI**：`experiment_service.py` 不 import 任何 LLM / LangChain /
向量库代码（验收脚本会直接读源码断言这一点）。偏差回溯由确定性规则 + MySQL 查询完成。

**偏差回溯的两个必要条件**（规范要求「偏差类型相同 + 实验类型相同」）：

1. `experiment_type` 相同；
2. `deviation_type` 相同 —— 当查询**未指定**偏差类型时（刚进入模板、尚未发生偏差）
   该条不适用，此时只按实验类型召回，用于「主动提示」。

**相关度排序**（必要条件之外）：药物名称完全相同 +1.0 / 互为子串 +0.4；
每种共用试剂 +0.35（批号也相同再 +0.3）；每台共用仪器 +0.25；
历史记录写明了处理方式 +0.2。等级：`high` ≥ 2.0、`medium` ≥ 1.3、其余 `low`。
排序为 相关度 ↓ → 时间 ↓ → id ↓，与输入顺序无关。

**主动提示的入口**：`GET /experiments/template/{experiment_type}` 返回
「模板骨架 + 该类型历史偏差 + 偏差类型分布 + 提示文案」，
前端可直接把 `field_hints` 渲染成表单、把 `history.matches` 渲染成警告条。

**偏差类型必填校验**：填了 `deviation_description` 却没给 `deviation_type` 会被拒绝
（422），因为回溯引擎按偏差类型匹配，缺类型会让这条记录永远无法被检索到。

**无偏差实验**：`deviation_type` 为空表示「本次无偏差」，这类记录不参与回溯
（避免把正常实验当作历史偏差提示）。

## 考点匹配设计（阶段五）

**匹配算法**：编辑距离**初筛**（保留 top-N 候选）→ 语义**精筛**（sentence-transformers
批量编码 + 余弦相似度）→ 加权合成 `语义*0.6 + 编辑*0.4`。互相包含时编辑距离给一个
较高的下限（短考点常是长知识点的一部分）。

**语义精筛的实际价值**（真实验收数据）：`aspirin 是怎么起效的` 与
`阿司匹林的作用机制` 几乎没有字面重叠 —— 离线哈希嵌入判为**未匹配**，
sentence-transformers 以 0.94 的语义相似度**正确匹配**；`抗生素是怎么杀菌的`
也能泛化匹配到 `青霉素的抗菌机制`。

**无 Agent 降级模式**：未安装 sentence-transformers、模型不可达或 `ST_ENABLED=false`
时，自动降级为「编辑距离 + 离线确定性哈希嵌入」，并通过 `capabilities.embedding`
如实上报（`degraded=true` + `fallback_reason`）。**权重会整体让给编辑距离** ——
否则降级后最高分只有 0.4，永远低于阈值，降级路径会彻底失效。

**优先级三级**（规范要求「考点 + 学生薄弱点 / 掌握一般 / 已掌握」）：
掌握度由既有数据推导 —— 阶段二的错题（薄弱点阈值）与阶段一 FSRS 卡片的
`fsrs_stability` / 是否复习过：

| 掌握度 | 判定 | 优先级 |
| --- | --- | --- |
| weak | 命中薄弱点、或没有任何卡片、或卡片全部从未复习 | **高危** |
| moderate | 有复习记录但平均稳定性 < `EXAM_MASTERED_STABILITY`（默认 21 天） | **巩固** |
| mastered | 平均稳定性 ≥ 阈值且无历史错误 | **确认** |

未匹配到知识点的考点按**高危**处理：考纲里出现但知识图谱中没有对应内容，
意味着缺少复习材料，属于需要优先补上的风险项。

**插入复习队列**是幂等的：按「卡片正面已存在」判定重复，因此重复导入/重复点击
不会产生重复卡片。高危考点的卡片正面为 `【考点】...`，背面优先引用关联卡片的
真实内容（保证是有内容的复习材料，而不是空壳）。

**OCR 可插拔**：`app/ocr.py` 把 OCR 引擎抽象为后端，不可用时返回 **503 + 安装指引**
（而不是 500），并始终保留「文本粘贴 / txt 文件」这条可用路径。

## 数据库迁移（格式版本化）

表结构以 **Alembic 迁移**为准，不再依赖 `create_all()`：

```bash
cd backend
./venv/bin/alembic upgrade head      # 建表 / 升级到最新
./venv/bin/alembic current           # 查看当前版本
./venv/bin/alembic check             # 检查模型与迁移是否一致（无 drift）
./venv/bin/alembic revision --autogenerate -m "说明"   # 改完模型后生成迁移
./venv/bin/alembic downgrade -1      # 回滚一步
```

连接串不写在 `alembic.ini`，而由 `alembic/env.py` 通过 `os.getenv("DATABASE_URL")`
读取，与运行时代码用同一份配置；也可用 `alembic -x db_url=...` 临时覆盖。

**已有库的接管方式**：如果表是早期由 `create_all()` 建的（库里有表但没有
`alembic_version`），直接 `upgrade head` 会因表已存在而失败。先打一次标记：

```bash
./venv/bin/alembic stamp head
```

启动日志会报告迁移状态；处于未接管状态时会打印上面这条指引。

> 注意：`alembic/env.py` 中 `fileConfig(..., disable_existing_loggers=False)`
> 不可省略 —— 默认的 `True` 会在执行迁移时禁用应用已存在的 logger，导致日志静默丢失。

## 测试

```bash
cd backend

# 纯逻辑测试（无需数据库）：FSRS 调度、标签、密码/JWT、错题诊断、LLM 降级
./venv/bin/python -m pytest tests/ -q

# 真机验收：在真实 MySQL 上跑通完整闭环（自清理，不留业务数据）
./venv/bin/python scripts/verify_phase1_loop.py   # 阶段一：复习闭环
./venv/bin/python scripts/verify_phase2_loop.py   # 阶段二：错题诊断
./venv/bin/python scripts/verify_phase3_loop.py   # 阶段三：文献工作台（默认打真实 PubMed）
./venv/bin/python scripts/verify_phase4_loop.py   # 阶段四：实验档案 + 偏差回溯
./venv/bin/python scripts/verify_phase5_loop.py   # 阶段五：考点匹配
```

### 关于数据库集成测试（隔离优先）

**默认全量运行**：测试会自动派生并创建独立测试库
``<dbname>_test``（如 ``pharmacy_db_test``），全程不碰业务库。
默认应为 0 跳过；**唯一**会跳过的是「仓库提交状态」那条断言——
仓库尚无提交时它跳过并提示先提交（见「Git 基线」），提交后即通过。
需要数据库账号有建库权限：

```sql
CREATE DATABASE IF NOT EXISTS pharmacy_db_test
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON pharmacy_db_test.* TO 'pharmacy_user'@'localhost';
FLUSH PRIVILEGES;
```

```bash
./venv/bin/python -m pytest tests/          # 直接跑，全部用例都会执行
```

选择顺序（**隔离优先**）：

1. ``TEST_DATABASE_URL``（显式指定测试库）；
2. 派生 ``<dbname>_test`` 并自动创建；
3. 仅当独立库确实不可用、**且**显式设置 ``PHARMACY_ALLOW_PROD_DB_TESTS=1`` 时，
   才退回复用业务库（仍带空表护栏：检测到应用表有数据就立即中止）；
4. 以上都不行才跳过，并给出可执行的修复指引。

测试库与业务库**同名时直接拒绝运行**。测试结束会清空测试库中的应用表，
``alembic_version`` 等基础设施表不受影响。

## 安全与限流

| 能力 | 说明 | 配置 |
| --- | --- | --- |
| 登录失败限流 | 滑动窗口 + 锁定，超限返回 429 带 ``retry_after``；只计失败，成功即清零 | ``LOGIN_MAX_ATTEMPTS`` / ``LOGIN_WINDOW_SECONDS`` / ``LOGIN_LOCKOUT_SECONDS`` |
| 令牌吊销 | ``POST /auth/logout`` 把用户 ``token_version`` +1，使**已签发的全部令牌立即失效**（无状态 JWT 的吊销锚点） | — |
| 请求体上限 | 超过 ``MAX_REQUEST_BODY_BYTES`` 的 JSON 请求在进业务逻辑前返回 413；multipart 上传走各自端点上限 | ``MAX_REQUEST_BODY_BYTES`` |
| 文本长度上限 | 所有文本入参都有 ``max_length``；批量导入按条截断，避免撞 MySQL ``TEXT`` 的 64KB **字节**上限 | ``app/limits.py`` |
| 登录错误不泄露账号是否存在 | 用户名不存在与密码错误返回相同响应 | — |

> 限流是**进程内**实现（不引入 Redis）。多 worker 部署时实际阈值约为
> ``LOGIN_MAX_ATTEMPTS × worker 数``；单进程部署则精确。

## 设计说明

- **时间纪律**：MySQL `DATETIME` 默认只有秒级精度且会四舍五入，会让 `due` 与调度结果
  不一致，因此所有时间列使用 `DATETIME(6)`。库内一律存 naive UTC，进出服务层统一转换；
  py-fsrs 强制要求 aware UTC，转换缺失会直接报错。
- **`cards` 表新增两列**：`fsrs_state`、`fsrs_step`。FSRS-6 的调度分支依赖这两个字段，
  只保留 `stability/difficulty/due/last_review` 会让 Review 态卡片被误当新卡重建，
  导致 `due` 完全错误（表数量仍为三张）。详见 `app/models/card.py` 顶部注释。
- **`wrong_questions` 表新增一列**：`socratic_question_type`。问题类型决定交互行为
  （偏好性问题无答案，作答后不显示解析），前端读取错题时必须能拿到该类型。
  详见 `app/models/wrong_question.py` 顶部注释。
- **`knowledge_graph` 严格按既定字段实现**，未加 `status` 列 —— 因为只有确认后的数据
  才会写入该表，表内不需要区分状态。
- **草稿与向量数据放在 `backend/chroma_db/`**（已加入 `.gitignore`）。
- **LLMGraphTransformer 需要 `langchain-experimental`**：该包不在阶段三声明的
  `langchain / chromadb / networkx` 清单中，但 `LLMGraphTransformer` 实际由它提供，
  因此 `requirements.txt` 中显式列出。
- **`exam_points` 严格按既定字段实现，本阶段未新增任何列。**
- **sentence-transformers / torch 安装注意**：必须装 **CPU 版** torch，
  否则 PyPI 默认会拉取数 GB 的 CUDA/NVIDIA 依赖：
  ```bash
  pip install --index-url https://download.pytorch.org/whl/cpu torch
  pip install sentence-transformers
  ```
- **HuggingFace 直连不通**：本机 `huggingface.co` DNS 不可达，默认走 `hf-mirror.com`
  镜像（`HF_ENDPOINT`），并设置 `HF_HUB_DISABLE_XET=1`（镜像的部分文件会重定向到
  本机不可达的 `cas-bridge.xethub.hf.co`）。模型缓存在 `.hf_cache/`（已 gitignore）。
  不需要语义精筛时设 `ST_ENABLED=false` 即可跳过模型加载。
- **`experiments` 表新增两列**：`drug_structure`（药物结构）与 `deviation_handling`（处理方式）。
  二者都是「实验模板字段」明确要求、但既定列清单中缺失的单一标量，不适合塞进 JSON。
  「试剂与批号」「仪器与校准信息」则是既定列 `reagents` / `instruments`（JSON）承载，
  写入时保留调用方给出的全部字段（规格、供应商等），仅把中文键名归一化。
- **新卡约定**：py-fsrs 的 `Card()` 会把 `due` 默认成当前时刻；本平台显式归一化为
  `NULL`，以区分「从未学过」与「已到期」，并让新卡在到期队列中排在最前。
- **降级模式**：调度完全由本地 py-fsrs 完成，不依赖任何 DSH 插件或外部 AI 服务。
