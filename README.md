# 药学学习平台（Pharmacy Learning Platform）

面向药学专业学生的**学习闭环**后端：错题 → 薄弱点 → 复习调度 → 文献知识 → 实验复盘 → 考点对齐。

> **交付范围（重要）**
> 本仓库当前交付的是**后端 API + 一个单页文献校对界面**
> （`backend/app/static/literature_review.html`）。
> `frontend/` 目录**尚为空**：复习、错题、实验档案、考点匹配四个主流程
> **没有前端 UI**，只能通过 REST API / OpenAPI 文档访问。
> 若验收要求完整的图形界面，这部分需要单独排期（见 `docs/audit-fix/`）。

## 五个阶段

| 阶段 | 主题 | 关键能力 |
| --- | --- | --- |
| 1 | 复习面板 | FSRS-6 调度、四档评分、到期查询 |
| 2 | 错题诊断 | 错题录入、知识点识别（需人工确认）、薄弱点预警 |
| 3 | 文献工作台 | PubMed 检索、PICO-D 抽取、知识图谱、人工校对 |
| 4 | 实验档案 | 药剂 ELN、偏差回溯规则引擎（纯规则，不用 AI） |
| 5 | 考点匹配 | 考点导入、编辑距离 + 语义匹配、优先级分类、入队复习 |

## 快速开始

```bash
cd backend
cp .env.example .env        # 填写 DATABASE_URL 与 JWT_SECRET_KEY
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
alembic upgrade head        # 建表（迁移是唯一入口）
uvicorn app.main:app --reload   # 单进程；不要用 --workers>1，原因见 backend/README
```

- API 文档：<http://127.0.0.1:8000/docs>
- 文献校对界面：<http://127.0.0.1:8000/ui/literature>
- 健康检查：<http://127.0.0.1:8000/health>

## 测试

```bash
cd backend
pytest tests/ -q            # 默认使用独立 <dbname>_test，全量运行
                            # （唯一跳过项是「仓库提交状态」检查，提交后即通过）
```

## 已知取舍

- **卡片↔知识点关联默认 `fallback` 模式，不是全量并集**：标签无命中时才回落正文扫描，
  详见 [`backend/README.md`](backend/README.md#卡片知识点关联的匹配口径重要)。
- **只支持单进程部署**（限流/嵌入/Chroma 均为进程内单例）。
- **两套向量空间并存**（文献用离线哈希、考点匹配优先 sentence-transformers），
  边界见 `backend/app/embeddings.py` 的 `SPACE_*` 常量。

## 文档

- [`backend/README.md`](backend/README.md) —— 接口、配置、迁移与运维细节
- [`docs/audit-fix/`](docs/audit-fix/) —— 外部审计的逐项核实与修复报告（含 `fix-log.txt`）

## 许可证

未声明许可证（内部项目）。如需开源请补充 `LICENSE`。
