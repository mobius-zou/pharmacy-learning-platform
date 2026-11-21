# 组 1 修复报告：认证配置 / 请求体 DoS / 建表双轨

> 来源：外部审计报告 `后端全量测试通过.docx` 的 **P1 高风险** H1、H2、H3、H7。
> 结论：**4 项全部真实存在，已全部修复**；修复过程中另外发现并修掉了 2 个由本次改动引入的缺陷。
> 回归：`575 passed, 0 skipped`（修复前 557）。

---

## 一、问题定位（先验证，不盲信）

| 编号 | 报告结论 | 验证方法 | 验证结果 |
| --- | --- | --- | --- |
| H1 | `.env` 写 `JWT_SECRET_KEY`，代码只读 `JWT_SECRET`，静默回退进程内随机密钥 | 不读 `.env` 内容，直接读程序解析结果 `settings.jwt_secret_is_ephemeral` | **属实**：修复前为 `True`（即没拿到密钥） |
| H2 | `backend/.env` 权限 644 | 仅 `stat` 元数据 | **属实**：`-rw-r--r--` |
| H3 | 请求体限制只信 `Content-Length`、跳过 multipart；上传先 `await file.read()` 再校验 | 读 [middleware.py](../../backend/app/middleware.py) 与 `exam_points.py:244,296` | **属实**：chunked 可绕过；上传先全量入内存 |
| H7 | 启动 `create_all()` 与 Alembic 双轨 | `grep create_all app/main.py` | **属实**：`main.py:52` 每次启动建表 |

### H1 的实际危害
`jwt_secret_is_ephemeral=True` 意味着每次进程启动都会 `secrets.token_urlsafe(48)` 生成新密钥：

- 重启后**所有已签发 token 立即失效**（用户被动掉线）；
- 多 worker 部署时各 worker 密钥不同，**跨 worker 随机 401**；
- 单进程跑测试完全发现不了这个问题（测试不重启进程）。

---

## 二、解决方案

### H1：键名兼容 + 生产 fail-fast
- `config.py` 依次尝试 `JWT_SECRET` → `JWT_SECRET_KEY` → `SECRET_KEY`，任一生效即视为已配置；
- 新增 `settings.environment`（`APP_ENV`/`ENVIRONMENT`，默认 `development`）；
- **生产环境若仍是临时密钥，启动直接 `RuntimeError`**，不静默降级；
- 开发环境改为明确告警，提示后果。

**效果**：`settings.jwt_secret_is_ephemeral` 已从 `True` → **`False`**，token 现在跨重启稳定。

### H2：权限检查（不触碰 `.env`）
项目约束要求「不得读取、修改或生成 `.env`」，因此**没有 chmod、也没有读内容**：

- 启动时仅对 `.env` 做 `os.stat`，检测 `group/other` 可读位；
- 命中则告警并给出可直接执行的修复命令。

> 需要你手动执行一次：`chmod 600 backend/.env`
> 建议同时把该密码视为已暴露（同机其他用户此前可读）并按需轮换。

### H3：纯 ASGI 流式限制 + 上传前置校验
把 `BaseHTTPMiddleware` 换成**纯 ASGI 中间件**：

1. 有 `Content-Length` 时**先判后读**，不白读大包；
2. 无 `Content-Length`（chunked）时**边收边计数**，超限立即 413 —— 堵住未认证 `/auth/login` 的分块大包绕过；
3. 分层上限：JSON 2MiB、multipart 12MiB（覆盖 OCR 的 8MiB），端点内仍有更细限制；
4. 上传端点改用 `read_upload_limited()`：先看 `UploadFile.size`，超限直接拒绝；未知则 64KiB 分块读并硬计数。

### H7：建表唯一入口
- 新增 `AUTO_CREATE_SCHEMA`（**默认 false**）：启动不再 `create_all`；
- 启动只报告 Alembic 状态；库未接管时打印 `alembic upgrade head` / `stamp head` 指引；
- 5 个验收脚本也从 `create_all()` 改为执行 `alembic upgrade head`；
- 测试链路本来就由迁移建表，保持一致。

---

## 三、修复过程中新发现并修掉的 2 个缺陷

| 缺陷 | 成因 | 现象 | 修法 |
| --- | --- | --- | --- |
| 流异常被 Starlette 吞成 400 | 在 `receive()` 里抛自定义异常，被 Starlette body 解析捕获 | chunked 超限返回笼统 `400 "There was an error parsing the body"`，拿不到 413 | 改为「先计数缓冲、再重放」；缓冲上限即 limit，不会无界增长 |
| 流式响应被截断 | 重放后对后续 `receive()` 硬返回 `http.disconnect` | `/ui/literature`（FileResponse）返回空 body，测试 `test_review_ui_page_is_served` 失败 | 后续 `receive()` **透传真实事件**，保留断连语义 |

---

## 四、修复后测试情况

新增回归 **18 项**（`tests/test_group1_hardening.py`）：

| 覆盖点 | 用例数 | 关键断言 |
| --- | --- | --- |
| JWT 键名兼容（3 种）+ 缺失回退 | 4 | 三种键名都识别；缺失才回退 |
| 生产 fail-fast / 开发告警 | 2 | 生产抛 `RuntimeError`；开发仍可服务 |
| **当前环境密钥稳定性** | 1 | `jwt_secret_is_ephemeral is False`（防止键名再次漂移） |
| `.env` 权限检查（只 stat） | 2 | 644 时告警含 `chmod 600`；文件缺失不抛 |
| **chunked 无 Content-Length 超限 → 413** | 1 | 3 倍上限的分块包被拒 |
| 声明长度超限先判后读 | 1 | 413 |
| 上传按声明大小拒绝且不读内容 | 1 | `read_called is False` |
| 上传流式计数超限中断 / 正常读完 | 2 | 413 / 完整内容 |
| multipart 上限 > JSON 上限且 ≥8MiB | 1 | 防误伤课件截图 |
| `AUTO_CREATE_SCHEMA` 默认关闭 | 1 | 默认 `False` |
| 关闭时不调用 create_all / 开启时才调用 | 2 | 用替身断言调用次数 |

全量回归：

```
575 passed, 0 skipped in 107s      # 修复前：557 passed
```

五个验收脚本：`24 / 26 / 29 / 35 / 31`，全 0 失败（脚本已改为用 Alembic 建表）。

---

## 五、遗留与注意事项

- **`.env` 权限需你手动处理**（本项目约束不允许我改动 `.env`）；该文件内的数据库密码建议一并视为已暴露。
- 中间件现在会缓冲整个请求体（上限即 limit）。这是为了拿到确定性 413 的取舍；JSON 场景 2MiB 可接受，multipart 12MiB 在并发高时需留意内存。
- 多 worker 部署仍未验证（审计 M15），本组未涉及。
