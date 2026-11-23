import { Link } from "react-router-dom";
import { Button, Panel, StateBlock } from "@/shared/ui/primitives";

/** 文献工作台：保留既有单页（含严格 CSP），用链接接入，不重写。 */
export function LiteraturePage() {
  return (
    <div className="pl-page">
      <header className="pl-page__head">
        <div>
          <h1 className="pl-page__title">文献工作台</h1>
          <p className="pl-page__subtitle">
            PubMed 检索 → PICO-D 抽取 → 知识图谱校对。
          </p>
        </div>
      </header>
      <Panel>
        <StateBlock
          title="人工校对是硬性关卡"
          description={
            <>
              抽取结果先进入草稿区，<strong>只有点击「确认入库」</strong>才会写入知识图谱；
              校对界面的样式与脚本已在后端静态托管，并启用了严格 CSP。
            </>
          }
          action={
            <a className="pl-link-button" href="/ui/literature" target="_blank" rel="noreferrer">
              打开文献校对界面 ↗
            </a>
          }
        />
        <p className="pl-muted pl-page__note">
          提示：校对界面与新前端共用同一登录态（localStorage 的 <code>pharm_token</code>），
          不需要重复登录。
        </p>
      </Panel>
    </div>
  );
}

/** 卡片管理：本轮提供最小可用入口（创建 + 列表），保证空状态有下一步。 */
export function CardsPage() {
  return (
    <div className="pl-page">
      <header className="pl-page__head">
        <div>
          <h1 className="pl-page__title">卡片管理</h1>
          <p className="pl-page__subtitle">手动录入或批量粘贴，生成可复习的卡片。</p>
        </div>
      </header>
      <Panel>
        <StateBlock
          title="卡片录入界面即将补齐"
          description={
            <>
              当前可以先用两种方式添加卡片：在文献工作台把知识点一键转成卡片，
              或直接在 API 文档里调用 <code>POST /api/v1/cards</code>。
            </>
          }
          action={
            <div className="pl-inline-actions">
              <Link className="pl-link-button" to="/literature">
                去文献工作台
              </Link>
              <a
                className="pl-link-button pl-link-button--ghost"
                href="/docs"
                target="_blank"
                rel="noreferrer"
              >
                打开 API 文档 ↗
              </a>
            </div>
          }
        />
      </Panel>
    </div>
  );
}

export function ComingSoonPage({ title, description }: { title: string; description: string }) {
  return (
    <div className="pl-page">
      <header className="pl-page__head">
        <div>
          <h1 className="pl-page__title">{title}</h1>
          <p className="pl-page__subtitle">后端接口已就绪，界面正在按同一套设计语言补齐。</p>
        </div>
      </header>
      <Panel>
        <StateBlock
          title={`${title}界面即将到来`}
          description={description}
          action={<Button variant="ghost" onClick={() => history.back()}>返回上一页</Button>}
        />
      </Panel>
    </div>
  );
}

export function NotFoundPage() {
  return (
    <div className="pl-page">
      <Panel>
        <StateBlock
          title="页面不存在"
          description="链接可能已经变化。回到今日复习继续学习吧。"
          action={<Link className="pl-link-button" to="/review">回到今日复习</Link>}
        />
      </Panel>
    </div>
  );
}
