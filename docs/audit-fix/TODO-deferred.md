# 待办（本轮未做，已确认延后）

> 记录「为什么延后」与「重新启动时需要什么」，避免遗忘或重复讨论。
> 每条都对应 `docs/audit-fix/fix-log.txt` 中的一条记录。

---

## 1. OCR 独立进程 / 队列（审计建议，**暂缓**）

**现状（已保留的能力）**

- OCR 不可用 → `POST /exam-points/import/image` 返回 **503** + `install_hint`
  （`apt-get install tesseract-ocr tesseract-ocr-chi-sim`）+ `alternatives`
  （改用文本粘贴 / txt 文件导入）；
- 可用性通过 `GET /exam-points/capabilities` 上报（`ocr.available` / `reason` /
  `languages` / `install_hint` / `concurrency`）；
- 本轮新增**并发闸门**：`OCR_MAX_CONCURRENCY`（默认 2）+ 有界排队
  （`OCR_QUEUE_TIMEOUT_SECONDS`，默认 20s），排队超时返回 **503 `ocr_busy`**
  并带 `retry_after`，与「识别失败（422）」区分开；
- 语言包枚举结果已缓存，不再每次 OCR 都起子进程。

**为什么延后**

- 当前形态是「线程池 + 信号量」，单机小并发够用，且不引入新依赖；
- 真要上队列，需要引入 broker（Redis/RabbitMQ）或独立 worker 进程，
  属于部署形态变更（当前项目明确只支持单进程，见 `backend/README.md`
  「部署形态」一节），需要连同多 worker 一起规划。

**重新启动时需要做的事**

1. 选定 broker（优先 Redis，可与限流外置共用）；
2. 新增 `POST /exam-points/import/image` 的异步模式：入队返回 `job_id`，
   再提供 `GET /exam-points/import/jobs/{id}` 查询状态；
3. OCR worker 独立进程，消费队列并回写结果；
4. 保留现有同步路径作为 `prefer_sync=true` 的兼容分支；
5. 补 3 类测试：入队/查询状态机、worker 崩溃重试、并发上限。

---

## 2. 其他延后项（来自审计报告）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 跨秒双击评分的 Idempotency-Key | **已完成** | 见 `tests/test_idempotency.py`；客户端 UI 另加确认弹窗 |
| exam_points 同文本重复导入 | **已完成** | 合并 + `duplicates_skipped`，见 `tests/test_exam_dedupe.py` |
| Chroma/MySQL 一致性 | **已完成** | 改为状态机 `pending → indexing → indexed`，启动扫描 `indexing` 重放 |
| C2 统一嵌入空间 | **保持两套** | 边界已显式化，见 `tests/test_embedding_boundary.py` |
| H6 索引优化 | **已完成** | 生成列 + 多值索引，实测 5.7ms vs 32.7ms |
| lint 转阻塞 | **进行中** | 已记录存量基线，见 `docs/audit-fix/lint-baseline.md` |
