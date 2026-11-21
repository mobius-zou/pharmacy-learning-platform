"""文献服务单元测试（PubMed 解析 + PICO-D 抽取，不需要数据库、不需要网络）。

PubMed 的 HTTP 传输被替换为假客户端，因此可离线、确定性地验证解析与抽取逻辑。
另有一个标记为 ``network`` 的真实 PubMed 连通性测试，网络不可用时自动跳过。
"""

from __future__ import annotations

import json

import pytest

from app.llm import LLMResponse
from app.services import literature_service as lit

# --- 固定样本：一篇结构化的 RCT 记录 ----------------------------------------

SAMPLE_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">33301246</PMID>
      <Article>
        <Journal>
          <JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue>
          <Title>The New England journal of medicine</Title>
        </Journal>
        <ArticleTitle>Safety and Efficacy of the <i>BNT162b2</i> mRNA Vaccine.</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">BNT162b2 is a lipid nanoparticle vaccine.</AbstractText>
          <AbstractText Label="METHODS">A total of 43,548 participants were enrolled in this randomized controlled trial.
          Participants received BNT162b2 or placebo. The primary outcome was incidence of Covid-19.</AbstractText>
          <AbstractText Label="RESULTS">BNT162b2 was 95% effective.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Polack</LastName><ForeName>Fernando P</ForeName></Author>
          <Author><LastName>Thomas</LastName><ForeName>Stephen J</ForeName></Author>
        </AuthorList>
        <PublicationTypeList>
          <PublicationType>Randomized Controlled Trial</PublicationType>
          <PublicationType>Journal Article</PublicationType>
        </PublicationTypeList>
      </Article>
      <MeshHeadingList>
        <MeshHeading><DescriptorName>Covid-19</DescriptorName></MeshHeading>
        <MeshHeading><DescriptorName>Humans</DescriptorName></MeshHeading>
      </MeshHeadingList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="doi">10.1056/NEJMoa2034577</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

SAMPLE_SEARCH = json.dumps(
    {"header": {"type": "esearch"}, "esearchresult": {"count": "2", "idlist": ["33301246", "11111111"]}}
)


class FakePubMed(lit.PubMedClient):
    """把 HTTP 传输替换为固定响应。"""

    def __init__(self, search_body: str = SAMPLE_SEARCH, fetch_body: str = SAMPLE_XML, fail: str = ""):
        super().__init__(base_url="http://fake.local", timeout=1)
        self._search_body = search_body
        self._fetch_body = fetch_body
        self._fail = fail
        self.calls: list[tuple[str, dict]] = []

    def _request(self, endpoint: str, params: dict) -> str:
        self.calls.append((endpoint, params))
        if self._fail:
            raise lit.PubMedError(self._fail)
        if endpoint == "esearch.fcgi":
            return self._search_body
        if endpoint == "efetch.fcgi":
            return self._fetch_body
        raise AssertionError(f"未预期的端点：{endpoint}")


# --- XML 解析 ---------------------------------------------------------------


def test_parse_pubmed_xml_extracts_core_fields() -> None:
    articles = lit.parse_pubmed_xml(SAMPLE_XML)

    assert len(articles) == 1
    article = articles[0]
    assert article.pmid == "33301246"
    assert "BNT162b2" in article.title, "内联 <i> 标签不应丢失文本"
    assert article.journal == "The New England journal of medicine"
    assert article.year == "2020"
    assert article.doi == "10.1056/NEJMoa2034577"
    assert article.authors == ["Fernando P Polack", "Stephen J Thomas"]
    assert "Randomized Controlled Trial" in article.publication_types
    assert "Covid-19" in article.mesh_terms


def test_parse_pubmed_xml_keeps_structured_abstract_labels() -> None:
    article = lit.parse_pubmed_xml(SAMPLE_XML)[0]

    assert "BACKGROUND:" in article.abstract
    assert "METHODS:" in article.abstract
    assert "RESULTS:" in article.abstract


def test_parse_pubmed_xml_handles_multiple_articles() -> None:
    xml = SAMPLE_XML.replace("</PubmedArticleSet>", "") + SAMPLE_XML.split("<PubmedArticleSet>")[1]

    assert len(lit.parse_pubmed_xml(xml)) == 2


def test_parse_pubmed_xml_empty_and_invalid() -> None:
    assert lit.parse_pubmed_xml("") == []
    with pytest.raises(lit.PubMedError):
        lit.parse_pubmed_xml("<not-xml")


def test_article_url_points_to_pubmed() -> None:
    article = lit.parse_pubmed_xml(SAMPLE_XML)[0]
    assert article.url == "https://pubmed.ncbi.nlm.nih.gov/33301246/"


# --- 客户端行为 -------------------------------------------------------------


def test_search_returns_pmids_and_total() -> None:
    client = FakePubMed()

    pmids, total = client.search("covid vaccine", retmax=5)

    assert pmids == ["33301246", "11111111"]
    assert total == 2
    endpoint, params = client.calls[0]
    assert endpoint == "esearch.fcgi"
    assert params["term"] == "covid vaccine"
    assert params["db"] == "pubmed"
    assert params["retmax"] == 5


def test_search_rejects_empty_query() -> None:
    with pytest.raises(lit.PubMedError):
        FakePubMed().search("   ")


def test_fetch_returns_article() -> None:
    article = FakePubMed().fetch("33301246")
    assert article.pmid == "33301246"


def test_fetch_raises_when_not_found() -> None:
    with pytest.raises(lit.PubMedError, match="未找到"):
        FakePubMed(fetch_body="<PubmedArticleSet></PubmedArticleSet>").fetch("1")


def test_fetch_many_deduplicates_empty_ids() -> None:
    client = FakePubMed()
    articles = client.fetch_many(["33301246", "  ", ""])

    assert len(articles) == 1
    _endpoint, params = client.calls[0]
    assert params["id"] == "33301246", "空 id 不应进入请求"


def test_transport_error_becomes_pubmed_error() -> None:
    with pytest.raises(lit.PubMedError):
        FakePubMed(fail="连接超时").search("x")


def test_search_invalid_json_reports_parse_failure() -> None:
    with pytest.raises(lit.PubMedError, match="解析失败"):
        FakePubMed(search_body="not json").search("x")


# --- PICO-D 规则抽取 --------------------------------------------------------


def test_design_from_publication_type() -> None:
    article = lit.parse_pubmed_xml(SAMPLE_XML)[0]
    pico = lit.extract_pico_d_heuristic(article)

    assert pico.design.value == "随机对照试验（RCT）"
    assert pico.design.confidence > 0.8
    assert "Randomized Controlled Trial" in pico.design.evidence


@pytest.mark.parametrize(
    ("pub_type", "expected"),
    [
        ("Meta-Analysis", "Meta 分析"),
        ("Systematic Review", "系统评价"),
        ("Randomized Controlled Trial", "随机对照试验（RCT）"),
        ("Clinical Trial", "临床试验"),
        ("Observational Study", "观察性研究"),
        ("Review", "综述"),
    ],
)
def test_design_detection_covers_common_types(pub_type: str, expected: str) -> None:
    article = lit.Article(pmid="1", publication_types=[pub_type])
    assert lit.extract_pico_d_heuristic(article).design.value == expected


def test_design_falls_back_to_abstract_keywords() -> None:
    article = lit.Article(
        pmid="1",
        abstract="This was a retrospective cohort study of 500 patients.",
        publication_types=[],
    )
    pico = lit.extract_pico_d_heuristic(article)

    assert pico.design.value == "回顾性队列研究"
    assert pico.design.confidence < 0.8, "关键词判定的置信度应低于出版类型判定"


def test_design_unknown_when_no_signal() -> None:
    pico = lit.extract_pico_d_heuristic(lit.Article(pmid="1", abstract="Nothing useful here."))
    assert pico.design.value == ""
    assert pico.design.confidence == 0.0


@pytest.mark.parametrize(
    ("abstract", "expected_population", "expected_intervention"),
    [
        (
            "Patients with COVID-19 received BNT162b2 vaccine.",
            "COVID-19",
            "BNT162b2 vaccine",
        ),
        (
            "Adults with type 2 diabetes were randomized to metformin versus placebo.",
            "type 2 diabetes",
            "metformin",
        ),
        (
            "In patients with hypertension, amlodipine was compared with usual care.",
            "hypertension",
            "amlodipine",
        ),
    ],
)
def test_population_and_intervention_do_not_swallow_verbs(
    abstract: str, expected_population: str, expected_intervention: str
) -> None:
    """回归测试：贪婪捕获曾把谓语动词吞进人群字段。"""
    pico = lit.extract_pico_d_heuristic(lit.Article(pmid="1", abstract=abstract))

    assert pico.population.value == expected_population
    assert pico.intervention.value == expected_intervention


def test_comparator_detects_placebo_as_chinese_label() -> None:
    article = lit.Article(pmid="1", abstract="Participants received drug or placebo.")
    assert lit.extract_pico_d_heuristic(article).comparator.value == "安慰剂"


def test_comparator_empty_notes_possible_single_arm() -> None:
    pico = lit.extract_pico_d_heuristic(lit.Article(pmid="1", abstract="A case series."))
    assert pico.comparator.value == ""
    assert "单臂" in pico.comparator.evidence


def test_outcome_from_primary_outcome_sentence() -> None:
    article = lit.Article(pmid="1", abstract="The primary outcome was all-cause mortality at 28 days.")
    assert "all-cause mortality" in lit.extract_pico_d_heuristic(article).outcome.value


def test_outcome_falls_back_to_keywords() -> None:
    article = lit.Article(pmid="1", abstract="We report efficacy findings in this cohort.")
    pico = lit.extract_pico_d_heuristic(article)

    assert "efficacy" in pico.outcome.value
    assert pico.outcome.confidence <= 0.35


def test_population_falls_back_to_mesh_terms() -> None:
    article = lit.Article(pmid="1", abstract="No cohort wording.", mesh_terms=["Asthma", "Humans"])
    pico = lit.extract_pico_d_heuristic(article)

    assert "Asthma" in pico.population.value
    assert "Humans" not in pico.population.value, "Humans 不是疾病人群"
    assert pico.population.confidence < 0.5


def test_full_sample_pico_d_has_all_five_fields() -> None:
    pico = lit.extract_pico_d_heuristic(lit.parse_pubmed_xml(SAMPLE_XML)[0])

    assert pico.source == "heuristic"
    assert pico.filled_count >= 4
    for field in (pico.population, pico.intervention, pico.outcome, pico.design):
        assert field.value, "样本摘要应能抽出人群/干预/结局/设计"


def test_split_sentences() -> None:
    parts = lit.split_sentences("First one. Second one! Third?")
    assert parts == ["First one.", "Second one!", "Third?"]
    assert lit.split_sentences("") == []


# --- PICO-D：LLM 路径与降级 -------------------------------------------------


class FakeLLM:
    def __init__(self, response: LLMResponse | None = None, available: bool = True) -> None:
        self._response = response or LLMResponse(ok=False, error="未配置")
        self._available = available

    @property
    def available(self) -> bool:
        return self._available

    def complete_json(self, system_prompt, user_prompt, **kwargs) -> LLMResponse:  # noqa: ANN001
        return self._response


def test_extract_pico_d_uses_llm_when_available() -> None:
    article = lit.parse_pubmed_xml(SAMPLE_XML)[0]
    client = FakeLLM(
        LLMResponse(
            ok=True,
            data={
                "population": {"value": "健康成年人", "confidence": 0.9, "evidence": "participants"},
                "intervention": {"value": "BNT162b2", "confidence": 0.95, "evidence": "received BNT162b2"},
                "comparator": {"value": "安慰剂", "confidence": 0.9, "evidence": "or placebo"},
                "outcome": {"value": "Covid-19 发病率", "confidence": 0.85, "evidence": "incidence"},
                "design": {"value": "随机双盲对照试验", "confidence": 0.9, "evidence": "randomized"},
            },
        )
    )

    pico = lit.extract_pico_d(article, client=client)

    assert pico.source == "llm"
    assert pico.population.value == "健康成年人"
    assert pico.design.value == "随机双盲对照试验"
    assert pico.filled_count == 5


def test_extract_pico_d_falls_back_without_llm() -> None:
    article = lit.parse_pubmed_xml(SAMPLE_XML)[0]

    pico = lit.extract_pico_d(article, client=FakeLLM(available=False))

    assert pico.source == "heuristic"
    assert pico.design.value == "随机对照试验（RCT）"


def test_extract_pico_d_falls_back_on_llm_error() -> None:
    article = lit.parse_pubmed_xml(SAMPLE_XML)[0]

    pico = lit.extract_pico_d(article, client=FakeLLM(LLMResponse(ok=False, error="HTTP 500")))

    assert pico.source == "heuristic"
    assert pico.llm_error == "HTTP 500"


def test_extract_pico_d_fills_missing_llm_fields_from_rules() -> None:
    """LLM 只回部分字段时，用规则抽取补齐，而不是留空。"""
    article = lit.parse_pubmed_xml(SAMPLE_XML)[0]
    client = FakeLLM(
        LLMResponse(ok=True, data={"intervention": {"value": "BNT162b2", "confidence": 0.9}})
    )

    pico = lit.extract_pico_d(article, client=client)

    assert pico.source == "llm"
    assert pico.intervention.value == "BNT162b2"
    assert pico.design.value == "随机对照试验（RCT）", "缺失字段应由规则补齐"


def test_extract_pico_d_treats_string_fields() -> None:
    article = lit.Article(pmid="1", abstract="Patients with asthma received salbutamol.")
    client = FakeLLM(LLMResponse(ok=True, data={"intervention": "沙丁胺醇"}))

    assert lit.extract_pico_d(article, client=client).intervention.value == "沙丁胺醇"


def test_extract_pico_d_all_empty_llm_fields_falls_back() -> None:
    article = lit.parse_pubmed_xml(SAMPLE_XML)[0]
    client = FakeLLM(LLMResponse(ok=True, data={"population": {"value": ""}}))

    pico = lit.extract_pico_d(article, client=client)

    assert pico.source == "heuristic"
    assert pico.llm_error == "LLM 返回的字段全为空"


def test_build_draft_payload_shape() -> None:
    article = lit.parse_pubmed_xml(SAMPLE_XML)[0]
    payload = lit.build_draft_payload(article, lit.extract_pico_d_heuristic(article))

    assert set(payload) == {"article", "pico_d"}
    assert payload["article"]["pmid"] == "33301246"
    assert payload["article"]["url"].startswith("https://pubmed.ncbi.nlm.nih.gov/")
    assert set(payload["pico_d"]) >= {
        "population",
        "intervention",
        "comparator",
        "outcome",
        "design",
        "source",
    }


# --- 真实 PubMed（可选） ----------------------------------------------------


@pytest.mark.network
def test_live_pubmed_fetch_real_article() -> None:
    """真实访问 PubMed；网络不可用时跳过，不使测试失败。"""
    client = lit.PubMedClient(timeout=25)
    try:
        article = client.fetch("33301246")
    except lit.PubMedError as exc:
        pytest.skip(f"PubMed 不可达：{exc}")

    assert article.pmid == "33301246"
    assert article.title
    assert article.abstract
    assert article.publication_types


@pytest.mark.network
def test_live_pubmed_search_and_pico_d() -> None:
    client = lit.PubMedClient(timeout=25)
    try:
        pmids, total = client.search("aspirin COX mechanism", retmax=3)
        articles = client.fetch_many(pmids) if pmids else []
    except lit.PubMedError as exc:
        pytest.skip(f"PubMed 不可达：{exc}")

    assert total >= 0
    if not articles:
        pytest.skip("检索无结果")

    pico = lit.extract_pico_d_heuristic(articles[0])
    assert 0 <= pico.filled_count <= 5
