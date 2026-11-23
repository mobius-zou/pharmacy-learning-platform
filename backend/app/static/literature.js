/* 文献工作台 · 人工校对（零构建静态资源）
 *
 * 由 literature_review.html 的内联 <script> 拆出：
 *   · 外部脚本才能配合严格 CSP（script-src 'self'，禁止内联）；
 *   · 便于单独缓存与审阅。
 *
 * 约束：不引入任何第三方库；图谱布局自己实现并在帧之间分批执行。
 */

"use strict";

const API = "/api/v1";
const TOKEN_KEY = "pharm_token";

let token = localStorage.getItem(TOKEN_KEY) || "";
let current = null;        // 当前草稿
let currentTriples = [];   // 当前三元组（可编辑）
let currentKps = [];       // 当前知识点建议

const $ = (id) => document.getElementById(id);

/* ============================ 通用工具 ============================ */

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
const escapeAttr = escapeHtml;

/** 截断长文本并显式加省略号（原实现直接 slice，看不出被截断）。 */
function truncate(s, max) {
  const str = String(s ?? "");
  return str.length > max ? str.slice(0, max - 1) + "…" : str;
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("toast").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

/** 空状态块：比一行灰字更明确「为什么空、下一步做什么」。 */
function emptyState(title, hint) {
  return `<div class="empty"><div class="empty-title">${escapeHtml(title)}</div>${
    hint ? escapeHtml(hint) : ""
  }</div>`;
}

/** 加载态骨架。 */
function skeletonState(rows = 3) {
  return `<div class="skeleton">${'<i></i>'.repeat(rows)}</div>`;
}

/**
 * 确认弹窗：用于**有副作用且不易撤销**的操作（确认入库 / 拒绝 / 转卡片）。
 * 解决误触 —— 原生 <dialog> 自带焦点捕获与 Esc 取消，无需第三方库。
 */
function confirmAction(message, { okText = "确认", danger = false } = {}) {
  const dialog = $("confirmDialog");
  const okBtn = $("confirmOk");
  $("confirmMessage").textContent = message;
  okBtn.textContent = okText;
  okBtn.className = danger ? "bad" : "ok";

  return new Promise((resolve) => {
    const onClose = () => {
      dialog.removeEventListener("close", onClose);
      resolve(dialog.returnValue === "ok");
    };
    dialog.addEventListener("close", onClose);
    dialog.returnValue = "";
    dialog.showModal();
  });
}

/**
 * 幂等键：同一个「逻辑请求」在重试时必须复用同一个键。
 *
 * 目的：网络超时后用户再点一次 → 服务端识别为同一次操作，不会重复推进 FSRS。
 * 实现：按「方法+路径+请求体」记住键，**成功后清除**；
 * 失败（含超时）时保留，于是下一次点击复用旧键。
 */
const pendingIdempotencyKeys = new Map();

function idempotencyKeyFor(method, path, body) {
  const fingerprint = `${method} ${path} ${JSON.stringify(body ?? null)}`;
  const existing = pendingIdempotencyKeys.get(fingerprint);
  if (existing) return { fingerprint, key: existing };

  const key =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  pendingIdempotencyKeys.set(fingerprint, key);
  return { fingerprint, key };
}

function forgetIdempotencyKey(fingerprint) {
  pendingIdempotencyKeys.delete(fingerprint);
}

/**
 * 请求中锁定按钮：防双击重复提交。
 * 用 dataset.busy 做同步判重（disabled 在回填前仍可能被第二次点击命中）。
 */
async function withBusy(btn, task) {
  if (!btn) return task();
  if (btn.dataset.busy === "1") return undefined;
  btn.dataset.busy = "1";
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  try {
    return await task();
  } finally {
    delete btn.dataset.busy;
    btn.disabled = false;
    btn.removeAttribute("aria-busy");
  }
}

/* ============================ 认证 ============================ */

/**
 * 只描述**本地**状态：有 token 不代表服务端校验通过（可能已过期或被吊销）。
 * 原实现直接断言令牌有效，属于未经校验的结论，这里改成中性描述。
 */
function setAuthState() {
  const el = $("authState");
  const authed = Boolean(token);
  el.dataset.state = authed ? "authed" : "anon";
  el.textContent = authed ? "已保存登录凭据（本地）" : "未登录";

  $("authForm").hidden = authed;
  $("logoutBtn").hidden = !authed;
}

/** 清空本地凭据并复位界面（401 或主动登出时调用）。 */
function clearAuth() {
  token = "";
  localStorage.removeItem(TOKEN_KEY);
  current = null;
  currentTriples = [];
  currentKps = [];
  setAuthState();
  resetPanels();
}

function resetPanels() {
  $("editor").hidden = true;
  $("editorEmpty").hidden = false;
  $("reviews").innerHTML = emptyState("暂无草稿", "登录后点「刷新」加载待校对草稿。");
  $("searchResults").innerHTML = emptyState("还没有检索结果", "输入关键词后点「检索」。");
  $("searchInfo").textContent = "";
  graphData = { nodes: [], edges: [] };
  $("graphStats").innerHTML = "";
  drawGraph();
}

async function api(path, { method = "GET", body = null, raw = false } = {}) {
  const headers = {};
  if (token) headers["Authorization"] = "Bearer " + token;
  if (body !== null) headers["Content-Type"] = "application/json";

  // 写请求带幂等键：重试复用旧键，避免「超时重发」产生第二次副作用
  const isWrite = method !== "GET";
  const idem = isWrite ? idempotencyKeyFor(method, path, body) : null;
  if (idem) headers["Idempotency-Key"] = idem.key;

  const res = await fetch(API + path, {
    method,
    headers,
    body: body === null ? undefined : JSON.stringify(body),
  });

  if (res.status === 401) {
    // 令牌失效：立刻清掉本地状态，避免界面停留在「看似已登录」的假象
    const wasAuthed = Boolean(token);
    clearAuth();
    if (!wasAuthed) toast("请先登录", "bad");
    else toast("登录已失效，请重新登录", "bad");
    throw new Error("unauthorized");
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.message || detail;
      if (data.detail && Array.isArray(data.detail.errors)) {
        detail = data.detail.errors.map((e) => e.msg || e.type).join("; ");
      } else if (typeof data.detail === "string") {
        detail = data.detail;
      }
    } catch (e) { /* 响应体不是 JSON，保留 statusText */ }
    toast("请求失败：" + detail, "bad");
    throw new Error(detail);
  }

  // 服务端已确认成功：清掉该操作的幂等键，下一次是新操作
  if (idem) forgetIdempotencyKey(idem.fingerprint);

  if (raw) return res;
  if (res.status === 204) return null;
  return res.json();
}

async function login(register, btn) {
  const username = $("user").value.trim();
  const password = $("pass").value;
  if (!username || !password) {
    toast("请输入用户名和密码", "bad");
    return;
  }
  await withBusy(btn, async () => {
    try {
      if (register) {
        await api("/auth/register", { method: "POST", body: { username, password } });
        toast("注册成功，正在登录…", "ok");
      }
      const data = await api("/auth/login", { method: "POST", body: { username, password } });
      token = data.access_token;
      localStorage.setItem(TOKEN_KEY, token);
      // 登录成功：把用户输入从 DOM 里清掉，避免留存在页面上
      $("pass").value = "";
      setAuthState();
      toast("登录成功", "ok");
      await Promise.all([loadReviews(), loadGraph()]);
    } catch (e) { /* 已提示 */ }
  });
}

async function logout(btn) {
  await withBusy(btn, async () => {
    try {
      // 服务端会把 token_version +1，使已签发令牌全部失效
      await api("/auth/logout", { method: "POST" });
    } catch (e) { /* 令牌本就失效时也要继续本地登出 */ }
    clearAuth();
    toast("已登出", "ok");
  });
}

/* ============================ PubMed 检索 ============================ */

async function search(btn) {
  const q = $("query").value.trim();
  if (!q) {
    toast("请输入检索关键词", "bad");
    return;
  }
  $("searchInfo").textContent = "检索中…";
  $("searchResults").innerHTML = skeletonState(3);
  await withBusy(btn, async () => {
    try {
      const data = await api("/literature/search?q=" + encodeURIComponent(q) + "&retmax=10");
      $("searchInfo").textContent = `命中 ${data.total} 条，显示 ${data.items.length} 条`;
      $("searchResults").innerHTML = data.items.length
        ? data.items.map((a) => `
          <div class="item">
            <div><b>${escapeHtml(a.title || "(无标题)")}</b></div>
            <div class="muted">PMID ${escapeAttr(a.pmid)} · ${escapeHtml(a.journal || "")} ${escapeHtml(a.year || "")}</div>
            <button class="ghost mt-2" type="button" data-pmid="${escapeAttr(a.pmid)}">抽取这条</button>
          </div>`).join("")
        : emptyState("没有匹配的文献", "换个更宽泛的关键词试试。");

      $("searchResults").querySelectorAll("button[data-pmid]").forEach((b) => {
        b.onclick = () => extract(b.dataset.pmid, b);
      });
    } catch (e) {
      $("searchInfo").textContent = "检索失败";
      $("searchResults").innerHTML = emptyState("检索失败", "请检查网络或稍后重试。");
    }
  });
}

async function extract(pmid, btn) {
  const id = String(pmid || "").trim();
  if (!id) {
    toast("请输入 PMID", "bad");
    return;
  }
  await withBusy(btn || $("extractBtn"), async () => {
    toast("正在抓取并抽取 " + id + " …");
    try {
      const draft = await api("/literature/extract", { method: "POST", body: { pmid: id } });
      toast(
        `抽取完成：来源 ${draft.extraction_source}，${draft.triples.length} 条关系。请人工校对后入库。`,
        "ok",
      );
      await loadReviews();
      await openDraft(draft.draft_id);
    } catch (e) { /* 已提示 */ }
  });
}

/* ============================ 草稿列表 ============================ */

async function loadReviews() {
  if (!token) return;
  $("reviews").innerHTML = skeletonState(2);
  try {
    const data = await api("/literature/reviews?status=" + $("statusFilter").value);
    $("reviews").innerHTML = data.items.length
      ? data.items.map((d) => `
        <div class="item" role="option" tabindex="0" aria-selected="false"
             data-id="${escapeAttr(d.draft_id)}">
          <div>
            <b>PMID ${escapeAttr(d.pmid)}</b>
            <span class="pill ${escapeAttr(d.status)}">${escapeHtml(d.status)}</span>
            <span class="pill">${escapeHtml(d.extraction_source)}</span>
          </div>
          <div class="muted">${escapeHtml(truncate((d.article && d.article.title) || "", 90))}</div>
          <div class="muted">${escapeAttr(d.triples.length)} 条关系 · ${escapeAttr(d.knowledge_points.length)} 个知识点</div>
        </div>`).join("")
      : emptyState("没有符合条件的草稿", "换个状态筛选，或从左侧检索并抽取一篇文献。");

    $("reviews").querySelectorAll(".item[data-id]").forEach((el) => {
      const open = () => openDraft(el.dataset.id);
      el.onclick = open;
      // 键盘可达：列表项是 role=option，需要 Enter/Space 可激活
      el.onkeydown = (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          open();
        }
      };
    });
  } catch (e) {
    $("reviews").innerHTML = emptyState("加载失败", "请检查登录状态后重试。");
  }
}

/* ============================ 校对编辑器 ============================ */

async function openDraft(id) {
  try {
    const draft = await api("/literature/reviews/" + encodeURIComponent(id));
    current = draft;
    currentTriples = draft.triples.map((t) => ({ ...t }));
    currentKps = draft.knowledge_points.map((k) => ({ ...k }));

    $("reviews").querySelectorAll(".item").forEach((el) => {
      const active = el.dataset.id === id;
      el.classList.toggle("active", active);
      el.setAttribute("aria-selected", active ? "true" : "false");
    });

    $("editorEmpty").hidden = true;
    $("editor").hidden = false;

    $("editorMeta").innerHTML =
      `PMID ${escapeAttr(draft.pmid)} · 状态 ` +
      `<span class="pill ${escapeAttr(draft.status)}">${escapeHtml(draft.status)}</span> · ` +
      `抽取来源 <b>${escapeHtml(draft.extraction_source)}</b>` +
      (draft.fallback_reason
        ? ` · <span class="muted">降级原因：${escapeHtml(draft.fallback_reason)}</span>`
        : "");

    const p = draft.pico_d || {};
    ["population", "intervention", "comparator", "outcome", "design"].forEach((k) => {
      $("f_" + k).value = (p[k] && p[k].value) || "";
    });
    $("note").value = draft.note || "";
    renderTriples();
    renderKps();
  } catch (e) { /* 已提示 */ }
}

function renderTriples() {
  $("tripleBody").innerHTML = currentTriples.length
    ? currentTriples.map((t, i) => `
      <tr>
        <td><input data-i="${i}" data-k="entity" aria-label="第 ${i + 1} 条关系的实体"
                   value="${escapeAttr(t.entity)}"></td>
        <td><input data-i="${i}" data-k="relation" aria-label="第 ${i + 1} 条关系的关系"
                   value="${escapeAttr(t.relation)}"></td>
        <td><input data-i="${i}" data-k="target" aria-label="第 ${i + 1} 条关系的目标"
                   value="${escapeAttr(t.target)}"></td>
        <td><input data-i="${i}" data-k="confidence" aria-label="第 ${i + 1} 条关系的置信度"
                   inputmode="decimal" value="${escapeAttr(t.confidence ?? 0)}"></td>
        <td>
          <button class="ghost icon" type="button" data-del="${i}"
                  aria-label="删除第 ${i + 1} 条关系">×</button>
        </td>
      </tr>`).join("")
    : `<tr><td colspan="5" class="muted">暂无关系，点下方「添加一条关系」手动补充。</td></tr>`;

  $("tripleBody").querySelectorAll("input").forEach((inp) => {
    inp.onchange = () => {
      const i = Number(inp.dataset.i);
      const k = inp.dataset.k;
      currentTriples[i][k] = k === "confidence" ? parseFloat(inp.value) || 0 : inp.value;
    };
  });
  $("tripleBody").querySelectorAll("button[data-del]").forEach((btn) => {
    btn.onclick = () => {
      currentTriples.splice(Number(btn.dataset.del), 1);
      renderTriples();
    };
  });
}

function renderKps() {
  $("kpList").innerHTML = currentKps.length
    ? currentKps.map((k, i) => `
      <div class="kp">
        <input type="checkbox" data-kp="${i}" checked
               aria-label="选择知识点 ${escapeAttr(truncate(k.knowledge_point, 40))}">
        <div>
          <div><b>${escapeHtml(k.knowledge_point)}</b>
            <span class="pill">${escapeAttr((k.confidence ?? 0).toFixed(2))}</span></div>
          <div class="muted">正面：${escapeHtml(k.front)}</div>
          <div class="muted">背面：${escapeHtml(truncate(k.back, 120))}</div>
        </div>
      </div>`).join("")
    : emptyState("暂无知识点建议", "该草稿没有抽取到知识点，可先在 PICO-D 中补充内容。");
}

function collectPico() {
  const p = JSON.parse(JSON.stringify(current.pico_d || {}));
  ["population", "intervention", "comparator", "outcome", "design"].forEach((k) => {
    p[k] = p[k] || {};
    p[k].value = $("f_" + k).value.trim();
  });
  return p;
}

function collectTriples() {
  return currentTriples
    .filter((t) => (t.entity || "").trim() && (t.relation || "").trim() && (t.target || "").trim())
    .map((t) => ({
      entity: t.entity.trim(),
      relation: t.relation.trim(),
      target: t.target.trim(),
      confidence: Number(t.confidence) || 0,
      evidence: t.evidence || "",
    }));
}

/* ============================ 校对动作 ============================ */

async function saveDraft(btn) {
  if (!current) return;
  await withBusy(btn, async () => {
    try {
      await api("/literature/reviews/" + encodeURIComponent(current.draft_id), {
        method: "PATCH",
        body: { pico_d: collectPico(), triples: collectTriples(), note: $("note").value },
      });
      toast("校对结果已保存（尚未入库）", "ok");
      await loadReviews();
    } catch (e) { /* 已提示 */ }
  });
}

async function confirmDraft(btn) {
  if (!current) return;
  const triples = collectTriples();
  if (!triples.length) {
    toast("至少需要一条完整的关系才能入库", "bad");
    return;
  }
  const ok = await confirmAction(
    `确认把这份草稿（${triples.length} 条关系）写入知识图谱？入库后需另行撤销。`,
    { okText: "确认入库" },
  );
  if (!ok) return;
  await withBusy(btn, async () => {
    try {
      const data = await api(
        "/literature/reviews/" + encodeURIComponent(current.draft_id) + "/confirm",
        { method: "POST", body: { triples } },
      );
      toast(`已确认入库：新增 ${data.added} 条，跳过重复 ${data.skipped} 条`, "ok");
      await openDraft(current.draft_id);
      await Promise.all([loadReviews(), loadGraph()]);
    } catch (e) { /* 已提示 */ }
  });
}

async function rejectDraft(btn) {
  if (!current) return;
  const ok = await confirmAction("确认拒绝这份草稿？拒绝后不会有任何数据入库。", {
    okText: "确认拒绝",
    danger: true,
  });
  if (!ok) return;
  await withBusy(btn, async () => {
    try {
      await api("/literature/reviews/" + encodeURIComponent(current.draft_id) + "/reject", {
        method: "POST",
        body: { reason: $("note").value || "人工判定不可用" },
      });
      toast("已拒绝该草稿，未写入数据库", "ok");
      await openDraft(current.draft_id);
      await loadReviews();
    } catch (e) { /* 已提示 */ }
  });
}

async function toCards(btn) {
  if (!current) return;
  const picked = [...document.querySelectorAll("#kpList input[type=checkbox]:checked")]
    .map((cb) => currentKps[Number(cb.dataset.kp)].knowledge_point);
  if (!picked.length) {
    toast("请至少勾选一个知识点", "bad");
    return;
  }
  const ok = await confirmAction(
    `把选中的 ${picked.length} 个知识点转成复习卡片？它们会立即进入复习队列。`,
    { okText: "转成卡片" },
  );
  if (!ok) return;
  await withBusy(btn, async () => {
    try {
      const data = await api(
        "/literature/reviews/" + encodeURIComponent(current.draft_id) + "/to-cards",
        { method: "POST", body: { knowledge_points: picked } },
      );
      toast(`已生成 ${data.created} 张复习卡片（卡片 id：${data.card_ids.join(", ")}）`, "ok");
    } catch (e) { /* 已提示 */ }
  });
}

/* ============================ 知识图谱 ============================ */

let graphData = { nodes: [], edges: [] };

/* 单帧内可安全渲染的节点上限；超出部分按连接度裁剪并明确告知用户 */
const GRAPH_MAX_NODES = 160;
const GRAPH_ITERATIONS = 240;
const GRAPH_ITERS_PER_FRAME = 10;

let layoutRun = 0; // 递增令牌：新的绘制请求会让旧的 rAF 循环自行退出

function setGraphStatus(text, busy) {
  const el = $("graphStatus");
  el.textContent = text || "";
  el.dataset.busy = busy ? "true" : "false";
}

async function loadGraph(btn) {
  if (!token) return;
  const task = async () => {
    setGraphStatus("加载图谱…", true);
    try {
      graphData = await api("/literature/graph");
      const s = graphData.stats || {};
      $("graphStats").innerHTML = `
        <span>节点 <b>${escapeAttr(s.node_count ?? 0)}</b></span>
        <span>关系 <b>${escapeAttr(s.edge_count ?? 0)}</b></span>
        <span>密度 <b>${escapeAttr(s.density ?? 0)}</b></span>
        <span>连通分量 <b>${escapeAttr(s.component_count ?? 0)}</b></span>
        <span>关系类型 <b>${escapeAttr(Object.keys(s.relation_counts || {}).length)}</b></span>`;
      drawGraph();
    } catch (e) {
      setGraphStatus("图谱加载失败", false);
    }
  };
  if (btn) await withBusy(btn, task);
  else await task();
}

/**
 * 力导向布局：分批在 rAF 里算，避免 O(n²) 迭代把主线程锁死。
 * 原实现 320 次全量迭代在节点多时会造成数秒白屏（无任何反馈）。
 */
function drawGraph() {
  const svg = $("graph");
  const run = ++layoutRun;

  const W = svg.clientWidth || 600;
  const H = svg.clientHeight || 430;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const all = graphData.nodes || [];
  if (!all.length) {
    svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" fill="#93a4b5" font-size="13"
      text-anchor="middle">暂无已确认的知识图谱数据</text>`;
    setGraphStatus("", false);
    return;
  }

  // 按连接度裁剪，保证单帧渲染与布局成本可控
  const sorted = [...all].sort((a, b) => (b.degree || 0) - (a.degree || 0));
  const kept = sorted.slice(0, GRAPH_MAX_NODES);
  const keptIds = new Set(kept.map((n) => n.id));
  const dropped = all.length - kept.length;

  const nodes = kept.map((n, i) => ({
    ...n,
    x: W / 2 + Math.cos(i) * (60 + (i % 5) * 20),
    y: H / 2 + Math.sin(i) * (60 + (i % 5) * 20),
    vx: 0,
    vy: 0,
  }));
  const index = new Map(nodes.map((n) => [n.id, n]));
  const edges = (graphData.edges || [])
    .map((e) => ({ ...e, s: index.get(e.source), t: index.get(e.target) }))
    .filter((e) => e.s && e.t);
  void keptIds;

  setGraphStatus(`布局计算中… 0/${GRAPH_ITERATIONS}`, true);

  let iter = 0;
  const tkBase = Math.sqrt((W * H) / Math.max(1, nodes.length)) * 0.42;

  function iterate(it) {
    nodes.forEach((n) => { n.vx = 0; n.vy = 0; });
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i];
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d = Math.hypot(dx, dy) || 0.01;
        const f = (tkBase * tkBase) / d;
        dx /= d;
        dy /= d;
        a.vx += dx * f;
        a.vy += dy * f;
        b.vx -= dx * f;
        b.vy -= dy * f;
      }
    }
    edges.forEach((e) => {
      let dx = e.s.x - e.t.x;
      let dy = e.s.y - e.t.y;
      const d = Math.hypot(dx, dy) || 0.01;
      const f = (d * d) / tkBase;
      dx /= d;
      dy /= d;
      e.s.vx -= dx * f;
      e.s.vy -= dy * f;
      e.t.vx += dx * f;
      e.t.vy += dy * f;
    });
    const cool = 1 - it / (GRAPH_ITERATIONS + 20);
    nodes.forEach((n) => {
      n.vx += (W / 2 - n.x) * 0.008;
      n.vy += (H / 2 - n.y) * 0.008;
      const len = Math.hypot(n.vx, n.vy) || 1;
      const max = 12 * cool + 0.6;
      n.x += (n.vx / len) * Math.min(len, max);
      n.y += (n.vy / len) * Math.min(len, max);
      n.x = Math.max(28, Math.min(W - 28, n.x));
      n.y = Math.max(20, Math.min(H - 20, n.y));
    });
  }

  function step() {
    if (run !== layoutRun) return; // 已被新的绘制请求取代
    const until = Math.min(GRAPH_ITERATIONS, iter + GRAPH_ITERS_PER_FRAME);
    for (; iter < until; iter++) iterate(iter);

    if (iter < GRAPH_ITERATIONS) {
      setGraphStatus(`布局计算中… ${iter}/${GRAPH_ITERATIONS}`, true);
      requestAnimationFrame(step);
      return;
    }

    renderSvg(nodes, edges, W, H);
    setGraphStatus(
      dropped > 0
        ? `节点过多，仅显示连接度最高的 ${GRAPH_MAX_NODES} 个（已隐藏 ${dropped} 个）`
        : "",
      false,
    );
  }

  requestAnimationFrame(step);
}

function renderSvg(nodes, edges, W, H) {
  const svg = $("graph");
  const maxDeg = Math.max(1, ...nodes.map((n) => n.degree || 0));
  const parts = [];

  edges.forEach((e) => {
    parts.push(
      `<line x1="${e.s.x.toFixed(1)}" y1="${e.s.y.toFixed(1)}"
        x2="${e.t.x.toFixed(1)}" y2="${e.t.y.toFixed(1)}"
        stroke="#3d5266" stroke-width="${Math.min(3, 1 + (e.weight || 1) * 0.4)}"/>`,
    );
  });

  // 边标签：过长截断并加省略号，完整文本放 <title>
  edges.forEach((e) => {
    const label = String(e.label ?? "");
    parts.push(
      `<text x="${((e.s.x + e.t.x) / 2).toFixed(1)}" y="${((e.s.y + e.t.y) / 2).toFixed(1)}"
        fill="#7d8fa1" font-size="9" text-anchor="middle" pointer-events="none"
      ><title>${escapeHtml(label)}</title>${escapeHtml(truncate(label, 12))}</text>`,
    );
  });

  nodes.forEach((n) => {
    const r = 6 + 16 * ((n.degree || 0) / maxDeg);
    const label = String(n.label ?? "");
    parts.push(
      `<circle cx="${n.x.toFixed(1)}" cy="${n.y.toFixed(1)}" r="${r.toFixed(1)}"
        fill="#2f6fa8" stroke="#6fb3f2" stroke-width="1.2"
      ><title>${escapeHtml(label)}（连接度 ${escapeAttr(n.degree ?? 0)}）</title></circle>`,
    );
    parts.push(
      `<text x="${n.x.toFixed(1)}" y="${(n.y + 3).toFixed(1)}"
        fill="#e6edf3" font-size="10" text-anchor="middle" pointer-events="none"
      >${escapeHtml(truncate(label, 10))}</text>`,
    );
  });

  svg.innerHTML = parts.join("");
}

/* ============================ 事件绑定 ============================ */

$("authForm").onsubmit = (ev) => { ev.preventDefault(); login(false, $("loginBtn")); };
$("registerBtn").onclick = () => login(true, $("registerBtn"));
$("logoutBtn").onclick = () => logout($("logoutBtn"));

$("searchForm").onsubmit = (ev) => { ev.preventDefault(); search($("searchBtn")); };
$("pmidForm").onsubmit = (ev) => { ev.preventDefault(); extract($("pmid").value, $("extractBtn")); };

$("reloadReviews").onclick = () => withBusy($("reloadReviews"), loadReviews);
$("statusFilter").onchange = () => loadReviews();

$("addTriple").onclick = () => {
  currentTriples.push({ entity: "", relation: "干预", target: "", confidence: 0.5, evidence: "" });
  renderTriples();
};

$("saveBtn").onclick = () => saveDraft($("saveBtn"));
$("confirmBtn").onclick = () => confirmDraft($("confirmBtn"));
$("rejectBtn").onclick = () => rejectDraft($("rejectBtn"));
$("cardsBtn").onclick = () => toCards($("cardsBtn"));

$("graphBtn").onclick = () => loadGraph($("graphBtn"));

document.querySelectorAll("button[data-export]").forEach((btn) => {
  btn.onclick = () => withBusy(btn, async () => {
    if (!token) {
      toast("请先登录", "bad");
      return;
    }
    const fmt = btn.dataset.export;
    try {
      const res = await fetch(`${API}/literature/graph/export?fmt=${encodeURIComponent(fmt)}`, {
        headers: { Authorization: "Bearer " + token },
      });
      if (res.status === 401) {
        clearAuth();
        toast("登录已失效，请重新登录", "bad");
        return;
      }
      if (!res.ok) {
        toast("导出失败", "bad");
        return;
      }
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "knowledge_graph." + fmt;
      a.click();
      URL.revokeObjectURL(a.href);
      toast("已开始下载 " + String(fmt).toUpperCase(), "ok");
    } catch (e) {
      toast("导出失败", "bad");
    }
  });
});

// 窗口尺寸变化时重绘（防抖，避免拖拽窗口时反复触发 O(n²) 布局）
let resizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => drawGraph(), 220);
});

/* ============================ 初始化 ============================ */

setAuthState();
if (token) {
  loadReviews();
  loadGraph();
} else {
  resetPanels();
}
