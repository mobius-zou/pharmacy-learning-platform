"""文献服务（阶段三）：PubMed 检索 + PICO-D 结构化提取。

依赖说明
--------
PubMed 通过 NCBI E-utilities 公开接口访问，仅使用标准库 ``urllib`` +
``xml.etree``（E-utilities 与网络访问不需要任何第三方包）。

PICO-D 提取有两条路径：
- **LLM 路径**：一次调用让模型输出 PICO-D 五个字段的 JSON；
- **规则路径（无 Agent 降级模式）**：未配置 LLM 或调用失败时，用研究设计关键词、
  句式模板与 MeSH 词做确定性抽取。

无论走哪条路径，结果都只是**待人工校对的草稿**，不会直接写入数据库。
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from ..config import settings
from ..llm import LLMClient, default_client
from ..logging_config import get_logger

logger = get_logger(__name__)


class PubMedError(Exception):
    """PubMed 访问或解析失败。"""


# --- 数据模型 ---------------------------------------------------------------


@dataclass(slots=True)
class Article:
    """一篇文献的元数据与摘要。"""

    pmid: str
    title: str = ""
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    year: str = ""
    doi: str = ""
    mesh_terms: list[str] = field(default_factory=list)
    publication_types: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["url"] = self.url
        return data


@dataclass(slots=True)
class PicoDField:
    """PICO-D 的单个字段（含来源与置信度）。"""

    value: str = ""
    confidence: float = 0.0
    evidence: str = ""


@dataclass(slots=True)
class PicoD:
    """PICO-D 结构化结果。"""

    population: PicoDField = field(default_factory=PicoDField)
    intervention: PicoDField = field(default_factory=PicoDField)
    comparator: PicoDField = field(default_factory=PicoDField)
    outcome: PicoDField = field(default_factory=PicoDField)
    design: PicoDField = field(default_factory=PicoDField)
    source: str = "heuristic"
    llm_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "population": asdict(self.population),
            "intervention": asdict(self.intervention),
            "comparator": asdict(self.comparator),
            "outcome": asdict(self.outcome),
            "design": asdict(self.design),
            "source": self.source,
            "llm_error": self.llm_error,
        }

    @property
    def filled_count(self) -> int:
        return sum(
            1
            for f in (self.population, self.intervention, self.comparator, self.outcome, self.design)
            if f.value.strip()
        )


# --- PubMed 客户端 ----------------------------------------------------------


class PubMedClient:
    """NCBI E-utilities 的最小客户端（标准库实现）。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        email: str | None = None,
        tool: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.pubmed_base_url).rstrip("/")
        self.api_key = settings.pubmed_api_key if api_key is None else api_key
        self.email = settings.pubmed_email if email is None else email
        self.tool = settings.pubmed_tool if tool is None else tool
        self.timeout = settings.pubmed_timeout_seconds if timeout is None else timeout

    # -- 传输层（测试可覆写） --

    def _request(self, endpoint: str, params: dict[str, Any]) -> str:
        """调用一个 E-utilities 端点并返回原始文本。"""
        query = {k: v for k, v in params.items() if v not in (None, "")}
        if self.api_key:
            query["api_key"] = self.api_key
        if self.email:
            query["email"] = self.email
        if self.tool:
            query["tool"] = self.tool

        url = f"{self.base_url}/{endpoint}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={"User-Agent": self.tool})
        limit = settings.pubmed_max_response_bytes
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                # 有 Content-Length 时先判后读，避免白读一遍超大响应
                declared = response.headers.get("Content-Length") if response.headers else None
                if declared and declared.isdigit() and int(declared) > limit:
                    raise PubMedError(
                        f"PubMed 响应过大（声明 {declared} 字节，上限 {limit}）"
                    )
                # 多读 1 字节用于判断是否超限（审计 M16：原先 read() 无上限）
                payload = response.read(limit + 1)
                if len(payload) > limit:
                    raise PubMedError(f"PubMed 响应超过上限 {limit} 字节")
                return payload.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise PubMedError(f"PubMed HTTP {exc.code}") from exc
        except Exception as exc:
            raise PubMedError(f"PubMed 请求失败：{type(exc).__name__}: {exc}") from exc

    # -- 业务方法 --

    def search(
        self, query: str, retmax: int = 20, retstart: int = 0, sort: str = "relevance"
    ) -> tuple[list[str], int]:
        """检索 PMID 列表，返回 (pmids, 命中总数)。"""
        if not query or not query.strip():
            raise PubMedError("检索词不能为空")

        raw = self._request(
            "esearch.fcgi",
            {
                "db": "pubmed",
                "term": query.strip(),
                "retmode": "json",
                "retmax": retmax,
                "retstart": retstart,
                "sort": sort,
            },
        )
        try:
            data = json.loads(raw)
            result = data["esearchresult"]
            pmids = [str(p) for p in result.get("idlist", [])]
            total = int(result.get("count", len(pmids)))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise PubMedError(f"PubMed 检索结果解析失败：{exc}") from exc

        # 检索词可能包含受试者信息或未公开课题名，日志里只留长度与短哈希，
        # 既能关联重复检索，又不落明文（审计 M16）。
        digest = hashlib.sha1(query.strip().encode("utf-8")).hexdigest()[:8]
        logger.info(
            "PubMed 检索（关键词 %d 字符，摘要 %s）命中 %s 条，返回 %s 条",
            len(query.strip()),
            digest,
            total,
            len(pmids),
        )
        return pmids, total

    def fetch_many(self, pmids: Sequence[str]) -> list[Article]:
        """按 PMID 批量抓取文献（efetch XML）。"""
        clean = [str(p).strip() for p in pmids if str(p).strip()]
        if not clean:
            return []

        raw = self._request(
            "efetch.fcgi",
            {"db": "pubmed", "id": ",".join(clean), "retmode": "xml"},
        )
        articles = parse_pubmed_xml(raw)
        logger.info("PubMed 抓取 %d 篇（请求 %d 个 PMID）", len(articles), len(clean))
        return articles

    def fetch(self, pmid: str) -> Article:
        """按 PMID 抓取单篇文献。"""
        articles = self.fetch_many([pmid])
        if not articles:
            raise PubMedError(f"未找到 PMID={pmid} 对应的文献")
        return articles[0]


def parse_pubmed_xml(raw: str) -> list[Article]:
    """解析 efetch 返回的 PubmedArticleSet XML。

    纯函数，便于用固定样本做单元测试（不依赖网络）。
    """
    if not raw or not raw.strip():
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise PubMedError(f"PubMed XML 解析失败：{exc}") from exc

    articles: list[Article] = []
    for node in root.findall(".//PubmedArticle"):
        articles.append(_parse_article(node))
    return articles


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def _parse_article(node: ET.Element) -> Article:
    citation = node.find("MedlineCitation")
    article_node = citation.find("Article") if citation is not None else None

    pmid = _text(citation.find("PMID")) if citation is not None else ""
    if not pmid:
        pmid = _text(node.find(".//ArticleId[@IdType='pubmed']"))

    article = Article(pmid=pmid)
    if article_node is None:
        return article

    # 标题：可能含 <i> 等内联标签，用 itertext 拼接
    article.title = _text(article_node.find("ArticleTitle"))

    # 摘要：多个 AbstractText（可能带 Label，如 BACKGROUND/METHODS）
    abstract_node = article_node.find("Abstract")
    if abstract_node is not None:
        parts: list[str] = []
        for chunk in abstract_node.findall("AbstractText"):
            label = chunk.get("Label") or chunk.get("NlmCategory") or ""
            body = _text(chunk)
            if not body:
                continue
            parts.append(f"{label.upper()}: {body}" if label else body)
        # 结构化摘要之外还可能有其他摘要文本
        if not parts:
            parts.append(_text(abstract_node))
        article.abstract = "\n".join(p for p in parts if p)

    # 作者
    for author in article_node.findall(".//AuthorList/Author"):
        last = _text(author.find("LastName"))
        fore = _text(author.find("ForeName"))
        collective = _text(author.find("CollectiveName"))
        if collective:
            article.authors.append(collective)
        elif last:
            article.authors.append(f"{fore} {last}".strip())

    # 期刊
    journal_node = article_node.find("Journal")
    if journal_node is not None:
        article.journal = _text(journal_node.find("Title")) or _text(journal_node.find("ISOAbbreviation"))

    # 年份：优先 JournalIssue/PubDate/Year，退回 MedlineDate 的前 4 位
    year = ""
    if journal_node is not None:
        pub_date = journal_node.find("JournalIssue/PubDate")
        if pub_date is not None:
            year = _text(pub_date.find("Year"))
            if not year:
                medline_date = _text(pub_date.find("MedlineDate"))
                match = re.search(r"\d{4}", medline_date)
                year = match.group(0) if match else ""
    article.year = year

    # 出版类型（用于研究设计判定）
    article.publication_types = [
        _text(pt) for pt in article_node.findall(".//PublicationTypeList/PublicationType") if _text(pt)
    ]

    # MeSH 主题词
    article.mesh_terms = [
        _text(mh) for mh in node.findall(".//MeshHeadingList/MeshHeading/DescriptorName") if _text(mh)
    ]

    # DOI
    for article_id in node.findall(".//ArticleIdList/ArticleId"):
        if article_id.get("IdType") == "doi":
            article.doi = _text(article_id)
            break

    return article


# --- PICO-D：规则抽取（无 Agent 降级模式） ----------------------------------

# 研究设计：出版类型优先，其次摘要正文关键词
_DESIGN_BY_PUBLICATION_TYPE: tuple[tuple[str, str, float], ...] = (
    ("meta-analysis", "Meta 分析", 0.95),
    ("systematic review", "系统评价", 0.9),
    ("randomized controlled trial", "随机对照试验（RCT）", 0.95),
    ("clinical trial", "临床试验", 0.85),
    ("controlled clinical trial", "对照临床试验", 0.8),
    ("observational study", "观察性研究", 0.75),
    ("comparative effectiveness", "比较效果研究", 0.7),
    ("validation study", "验证性研究", 0.7),
    ("review", "综述", 0.6),
)

_DESIGN_BY_TEXT: tuple[tuple[str, str, float], ...] = (
    ("systematic review", "系统评价", 0.8),
    ("meta-analysis", "Meta 分析", 0.8),
    ("randomized controlled trial", "随机对照试验（RCT）", 0.85),
    ("randomised controlled trial", "随机对照试验（RCT）", 0.85),
    ("double-blind", "双盲随机对照试验", 0.75),
    ("randomly assigned", "随机对照试验（RCT）", 0.7),
    ("randomized", "随机对照试验（RCT）", 0.65),
    ("randomised", "随机对照试验（RCT）", 0.65),
    ("retrospective cohort", "回顾性队列研究", 0.75),
    ("prospective cohort", "前瞻性队列研究", 0.75),
    ("cohort study", "队列研究", 0.7),
    ("case-control", "病例对照研究", 0.75),
    ("cross-sectional", "横断面研究", 0.7),
    ("crossover", "交叉试验", 0.65),
    ("phase 3", "III 期临床试验", 0.7),
    ("phase iii", "III 期临床试验", 0.7),
)

# 名词短语的右边界：遇到谓语动词/介词/标点就停下。
# 若只用贪婪的 [^.;,]+，会把「COVID-19 received BNT162b2」整段吞进人群字段。
_BOUNDARY = (
    r"(?=\s+(?:were|was|are|is|had|have|has|underwent|received|receiving|"
    r"treated|taking|using|participated|completed|consented|withdrew|discontinued|"
    r"showed|demonstrated|reported|experienced|provided|took|"
    r"versus|vs|compared|who|that|which|in|from|to|and|but|with|for|of)\b"
    r"|[,.;:，。；：]|$)"
)

# 被捕获片段若以这些词开头，说明抓到的是句子骨架而非名词短语，应丢弃
_REJECT_PREFIXES: tuple[str, ...] = (
    "in this",
    "in the",
    "in a",
    "in our",
    "of this",
    "of the",
    "to this",
    "to the",
    "this ",
    "these ",
    "those ",
    "who ",
    "which ",
    "that ",
    "at ",
    "for ",
)

# 人群：句式模板（「a total of N ...」最精确，放在最前）
_POPULATION_PATTERNS: tuple[str, ...] = (
    rf"a total of ([\d,]+\s[^.;,]{{3,50}}?){_BOUNDARY}",
    rf"patients with ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"adults with ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"children with ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"participants with ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"individuals with ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"women with ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"men with ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"in patients ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"we enrolled ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"were enrolled ([^.;,]{{3,60}}?){_BOUNDARY}",
)

# 干预：句式模板
_INTERVENTION_PATTERNS: tuple[str, ...] = (
    rf"(?:treated with|treatment with|received|receiving|administered|assigned to|"
    rf"randomized to|randomised to|given)\s+([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"efficacy of ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"effect of ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"administration of ([^.;,]{{3,60}}?){_BOUNDARY}",
    # 被动语态：「X was compared with / was administered to ...」
    r"([A-Za-z0-9][^.;,]{2,60}?)\s+was (?:compared|randomized|randomised|assigned|administered|given|used)",
)

# 对照：句式模板
_COMPARATOR_PATTERNS: tuple[str, ...] = (
    rf"compared with ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"compared to ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"versus ([^.;,]{{3,60}}?){_BOUNDARY}",
    rf"vs\.? ([^.;,]{{3,60}}?){_BOUNDARY}",
    r"(placebo)",
    r"(standard care)",
    r"(usual care)",
    r"(control group)",
)

# 结局：句式模板（结局本身是较长的描述，边界放宽）
_OUTCOME_PATTERNS: tuple[str, ...] = (
    r"primary (?:outcome|end ?point)s? (?:was|were|included|are)?\s*([^.;]{3,120})",
    r"secondary (?:outcome|end ?point)s? (?:was|were|included|are)?\s*([^.;]{3,120})",
    r"outcomes? (?:was|were|included|are|measured)\s*([^.;]{3,120})",
    r"we (?:measured|assessed|evaluated)\s+([^.;]{3,120})",
)

_OUTCOME_KEYWORDS: tuple[str, ...] = (
    "mortality",
    "morbidity",
    "efficacy",
    "safety",
    "response rate",
    "survival",
    "incidence",
    "adverse event",
    "adverse effect",
    "血药浓度",
    "生物利用度",
    "有效率",
    "不良反应",
)

_STRUCTURED_LABELS = ("BACKGROUND", "OBJECTIVE", "METHODS", "RESULTS", "CONCLUSIONS", "CONCLUSION")


def split_sentences(text: str) -> list[str]:
    """把摘要切成句子，保留结构化摘要的段落标签。"""
    if not text:
        return []
    normalized = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    parts = re.split(r"(?<=[.!?。！？])\s+", normalized)
    return [p.strip() for p in parts if p.strip()]


def _first_match(patterns: Sequence[str], text: str) -> tuple[str, str]:
    """返回 (匹配到的片段, 命中的完整句子)。"""
    for sentence in split_sentences(text):
        for pattern in patterns:
            for match in re.finditer(pattern, sentence, flags=re.IGNORECASE):
                captured = (match.group(1) or "").strip(" ,;:")
                if not captured:
                    continue
                if captured.lower().startswith(_REJECT_PREFIXES):
                    continue
                return captured[:200], sentence
    return "", ""


def _strip_label(sentence: str) -> str:
    """去掉结构化摘要的段落标签前缀。"""
    for label in _STRUCTURED_LABELS:
        if sentence.upper().startswith(label):
            return sentence[len(label) :].lstrip(":： ").strip()
    return sentence


def _detect_design(article: Article) -> PicoDField:
    """研究设计：出版类型优先，其次正文关键词。

    注意：按**词表的特异性顺序**匹配，而不是按 XML 中出版类型的先后顺序 ——
    否则「Clinical Trial」排在「Randomized Controlled Trial」之前时，
    会把 RCT 误判为普通临床试验。
    """
    lowered_types = [pub_type.lower() for pub_type in article.publication_types]
    for needle, label, confidence in _DESIGN_BY_PUBLICATION_TYPE:
        for index, lowered in enumerate(lowered_types):
            if needle in lowered:
                return PicoDField(
                    value=label,
                    confidence=confidence,
                    evidence=f"出版类型：{article.publication_types[index]}",
                )

    haystack = f"{article.title}\n{article.abstract}".lower()
    for needle, label, confidence in _DESIGN_BY_TEXT:
        if needle in haystack:
            sentence = next(
                (s for s in split_sentences(article.abstract) if needle in s.lower()), ""
            )
            return PicoDField(
                value=label,
                confidence=confidence,
                evidence=_strip_label(sentence)[:200] or f"关键词：{needle}",
            )

    return PicoDField(value="", confidence=0.0, evidence="未能判定研究设计")


def _detect_population(article: Article) -> PicoDField:
    value, sentence = _first_match(_POPULATION_PATTERNS, article.abstract or article.title)
    if value:
        return PicoDField(value=value, confidence=0.55, evidence=_strip_label(sentence)[:200])

    # 退回 MeSH：疾病类主题词常描述人群
    disease_terms = [
        term
        for term in article.mesh_terms
        if term.lower() not in {"humans", "male", "female", "adult", "aged", "animals"}
    ]
    if disease_terms:
        return PicoDField(
            value="、".join(disease_terms[:3]),
            confidence=0.35,
            evidence="来源：MeSH 主题词",
        )
    return PicoDField(value="", confidence=0.0, evidence="未能识别人群")


def _detect_intervention(article: Article) -> PicoDField:
    value, sentence = _first_match(_INTERVENTION_PATTERNS, article.abstract)
    if value:
        return PicoDField(value=value, confidence=0.5, evidence=_strip_label(sentence)[:200])
    return PicoDField(value="", confidence=0.0, evidence="未能识别干预措施")


def _detect_comparator(article: Article) -> PicoDField:
    value, sentence = _first_match(_COMPARATOR_PATTERNS, article.abstract)
    if not value:
        return PicoDField(value="", confidence=0.0, evidence="未提及对照（可能是单臂研究）")

    lowered = value.lower()
    label = "安慰剂" if "placebo" in lowered else value
    confidence = 0.6 if "placebo" in lowered else 0.55
    return PicoDField(
        value=label, confidence=confidence, evidence=_strip_label(sentence)[:200]
    )


def _detect_outcome(article: Article) -> PicoDField:
    value, sentence = _first_match(_OUTCOME_PATTERNS, article.abstract)
    if value:
        return PicoDField(value=value, confidence=0.5, evidence=_strip_label(sentence)[:200])

    haystack = (article.abstract or "").lower()
    hits = [kw for kw in _OUTCOME_KEYWORDS if kw.lower() in haystack]
    if hits:
        sentence = next(
            (s for s in split_sentences(article.abstract) if any(k.lower() in s.lower() for k in hits)),
            "",
        )
        return PicoDField(
            value="、".join(hits[:4]),
            confidence=0.35,
            evidence=_strip_label(sentence)[:200] or "来源：结局关键词",
        )
    return PicoDField(value="", confidence=0.0, evidence="未能识别结局指标")


def extract_pico_d_heuristic(article: Article) -> PicoD:
    """规则抽取 PICO-D（确定性、可单测、离线可用）。"""
    return PicoD(
        population=_detect_population(article),
        intervention=_detect_intervention(article),
        comparator=_detect_comparator(article),
        outcome=_detect_outcome(article),
        design=_detect_design(article),
        source="heuristic",
    )


# --- PICO-D：LLM 路径 -------------------------------------------------------

_PICO_SYSTEM_PROMPT = (
    "你是循证药学专家，负责把文献摘要结构化为 PICO-D 框架。\n"
    "P=Population 人群，I=Intervention 干预措施，C=Comparator 对照，"
    "O=Outcome 结局指标，D=Design 研究设计。\n"
    "要求：\n"
    "1. 只依据给定摘要，**不要编造**摘要中没有的信息；无法确定的字段填空字符串；\n"
    "2. 每个字段给出 0-1 的 confidence 以及摘要中的原文依据 evidence；\n"
    "3. 只输出 JSON，不要输出任何解释。\n"
    '输出格式：{"population":{"value":"","confidence":0.0,"evidence":""},'
    '"intervention":{...},"comparator":{...},"outcome":{...},"design":{...}}'
)


def _field_from_payload(data: dict[str, Any], key: str) -> PicoDField | None:
    raw = data.get(key)
    if isinstance(raw, str):
        value = raw.strip()
        return PicoDField(value=value, confidence=0.6 if value else 0.0) if value else None
    if not isinstance(raw, dict):
        return None

    value = str(raw.get("value", "") or "").strip()
    if not value:
        return None
    try:
        confidence = float(raw.get("confidence", 0.7))
    except (TypeError, ValueError):
        confidence = 0.7
    return PicoDField(
        value=value[:200],
        confidence=max(0.0, min(1.0, confidence)),
        evidence=str(raw.get("evidence", "") or "")[:200],
    )


def extract_pico_d(article: Article, client: LLMClient | None = None) -> PicoD:
    """PICO-D 提取：LLM 优先，失败/未配置则退回规则抽取。

    始终返回可用结果；``source`` 字段标明实际来源（``llm`` 或 ``heuristic``）。
    """
    llm = client if client is not None else default_client
    fallback = extract_pico_d_heuristic(article)

    if not llm.available:
        return fallback

    prompt = (
        f"标题：{article.title}\n"
        f"期刊：{article.journal} ({article.year})\n"
        f"出版类型：{'、'.join(article.publication_types) or '未知'}\n"
        f"MeSH：{'、'.join(article.mesh_terms[:10]) or '无'}\n\n"
        f"摘要：\n{article.abstract}"
    )
    response = llm.complete_json(_PICO_SYSTEM_PROMPT, prompt, temperature=0.0, max_tokens=1200)

    if not response.ok or not response.data:
        logger.info("PICO-D 提取回退到规则抽取：%s", response.error)
        fallback.llm_error = response.error
        return fallback

    data = response.data
    result = PicoD(source="llm")
    llm_contributed = 0
    # 逐个字段：LLM 没给出的，用规则抽取补齐（而不是留空）
    for key, attr in (
        ("population", "population"),
        ("intervention", "intervention"),
        ("comparator", "comparator"),
        ("outcome", "outcome"),
        ("design", "design"),
    ):
        parsed = _field_from_payload(data, key)
        if parsed is not None:
            setattr(result, attr, parsed)
            llm_contributed += 1
        else:
            setattr(result, attr, getattr(fallback, attr))

    # 一个字段都没贡献时，结果实际上全部来自规则抽取，来源必须如实标注 heuristic
    if llm_contributed == 0:
        logger.info("LLM 未返回任何有效字段，整体回退到规则抽取")
        fallback.llm_error = "LLM 返回的字段全为空"
        return fallback

    logger.info(
        "PICO-D 提取完成（LLM 贡献 %d/5 个字段，其余由规则补齐）", llm_contributed
    )
    return result


def build_draft_payload(article: Article, pico: PicoD) -> dict[str, Any]:
    """把文献 + PICO-D 组装成待校对草稿的载荷。"""
    return {
        "article": article.to_dict(),
        "pico_d": pico.to_dict(),
    }
