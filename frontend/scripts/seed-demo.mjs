/**
 * 演示数据播种（仅调用公开 API，不直接写数据库）。
 * 用于本地截图 / E2E：造一个学生账号 + 若干药学卡片 + 少量复习历史。
 *
 * 用法：node scripts/seed-demo.mjs [--base http://127.0.0.1:8000/api/v1]
 */
const baseArg = process.argv.indexOf("--base");
const BASE = baseArg > -1 ? process.argv[baseArg + 1] : "http://127.0.0.1:8000/api/v1";

const USER = { username: "demo_student", password: "pharmacy2026" };

const CARDS = [
  ["阿司匹林不可逆抑制哪种酶？", "环氧化酶（COX-1 与 COX-2），使血小板内 TXA2 合成减少。", ["药理学"], ["解热镇痛药"]],
  ["为什么小剂量阿司匹林可用于抗血小板？", "血小板无细胞核、不能再合成 COX，抑制可持续整个血小板生命周期（7–10 天）。", ["药理学"], ["解热镇痛药"]],
  ["对乙酰氨基酚过量最危险的毒性是什么？", "肝毒性：NAPQI 蓄积耗竭谷胱甘肽，可用 N-乙酰半胱氨酸解毒。", ["药理学"], ["解热镇痛药"]],
  ["青霉素类的主要抗菌机制？", "抑制细菌细胞壁肽聚糖交联（转肽酶/青霉素结合蛋白）。", ["药理学"], ["抗菌药物"]],
  ["为什么青霉素与氨基糖苷类联用有协同作用？", "青霉素破坏细胞壁利于氨基糖苷进入胞内，作用于不同靶点。", ["药理学"], ["抗菌药物"]],
  ["首过效应是什么？", "口服药经门静脉入肝，部分被代谢灭活，使进入体循环的药量减少。", ["药剂学"], ["药动学"]],
  ["生物利用度 F 的定义？", "进入体循环的药量与给药剂量之比，静脉注射绝对生物利用度为 1。", ["药剂学"], ["药动学"]],
  ["表观分布容积 Vd 偏大说明什么？", "药物广泛分布于组织、血浆浓度低；Vd 大者透析难以清除。", ["药剂学"], ["药动学"]],
  ["一级动力学消除的特点？", "单位时间消除恒定比例，半衰期与剂量无关；多数药物属此类。", ["药剂学"], ["药动学"]],
  ["零级动力学消除的特点？", "单位时间消除恒定量，半衰期随剂量增大而延长，易蓄积中毒。", ["药剂学"], ["药动学"]],
  ["华法林与阿司匹林联用的主要风险？", "出血风险叠加：抗凝 + 抗血小板，需监测 INR 与出血征象。", ["药理学"], ["药物相互作用"]],
  ["他汀类药物的作用靶点？", "抑制 HMG-CoA 还原酶，减少胆固醇合成，上调 LDL 受体。", ["药理学"], ["调脂药"]],
  ["肝药酶诱导剂对合用药物有何影响？", "加速代谢、降低血药浓度与疗效（如利福平、卡马西平）。", ["药理学"], ["药物相互作用"]],
  ["药物半衰期与给药间隔的关系？", "通常按半衰期设定间隔，经 4–5 个半衰期达稳态血药浓度。", ["药剂学"], ["药动学"]],
];

async function call(path, { method = "GET", body, token } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  if (method !== "GET") headers["Idempotency-Key"] = crypto.randomUUID();
  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok && response.status !== 409) {
    throw new Error(`${method} ${path} -> ${response.status}: ${text}`);
  }
  return { status: response.status, data };
}

async function main() {
  await call("/auth/register", { method: "POST", body: USER });
  const login = await call("/auth/login", { method: "POST", body: USER });
  const token = login.data.access_token;

  const list = await call("/cards?limit=1", { token });
  const existing = list.data.total ?? 0;
  let created = 0;
  if (existing < CARDS.length) {
    for (const [front, back, subjects, chapters] of CARDS) {
      const result = await call("/cards", {
        method: "POST",
        token,
        body: { front, back, tags: { subjects, chapters, point_types: ["机制"] } },
      });
      if (result.status === 201) created += 1;
    }
  }

  const due = await call("/review/due?limit=20", { token });
  // 造一点复习历史：给前两张卡各评一次，让「复习历史」有内容
  let graded = 0;
  for (const card of (due.data.items ?? []).slice(0, 2)) {
    const preview = await call(`/review/${card.id}/preview`, { token });
    const option = preview.data.options.find((item) => item.rating === 3) ?? preview.data.options[0];
    await call(`/review/${card.id}/grade`, {
      method: "POST",
      token,
      body: { rating: option.rating },
    });
    graded += 1;
  }

  const after = await call("/review/due?limit=20", { token });
  console.log(
    JSON.stringify(
      {
        user: USER.username,
        password: USER.password,
        cardsCreated: created,
        cardsTotal: after.data.total_due + graded,
        graded,
        dueNow: after.data.total_due,
        weakPoints: after.data.weak_point_count,
      },
      null,
      2,
    ),
  );
}

main().catch((error) => {
  console.error("seed failed:", error.message);
  process.exit(1);
});
