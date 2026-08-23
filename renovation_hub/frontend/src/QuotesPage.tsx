import {
  IconArrowLeft,
  IconCalendarEvent,
  IconCheck,
  IconChevronLeft,
  IconChevronRight,
  IconCircleCheckFilled,
  IconDownload,
  IconEdit,
  IconHome,
  IconMapPin,
  IconPhoto,
  IconPlus,
  IconReceipt2,
  IconRefresh,
  IconSearch,
  IconUpload,
  IconUser,
  IconX,
  IconZoomIn,
  IconZoomOut,
} from "@tabler/icons-react";
import { FormEvent, ReactNode, forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { ApiError, api, assetUrl, uploadMedia } from "./api";
import type {
  Project,
  QuoteDetail,
  QuoteMediaAsset,
  QuoteMediaRole,
  QuoteOffer,
  QuoteOfferStatus,
  QuoteRequest,
  QuoteRequestStatus,
} from "./types";

const SHANGHAI = "Asia/Shanghai";

const REQUEST_STATUS: Record<QuoteRequestStatus, { label: string; tone: string }> = {
  inquiry: { label: "询价中", tone: "blue" },
  quoted: { label: "已有报价", tone: "terracotta" },
  review_required: { label: "待确认", tone: "amber" },
  selected: { label: "已选定", tone: "green" },
  purchased: { label: "已采购", tone: "green" },
  closed: { label: "已关闭", tone: "muted" },
  archived: { label: "已归档", tone: "muted" },
};

const OFFER_STATUS: Record<QuoteOfferStatus, { label: string; tone: string }> = {
  quoted: { label: "有效报价", tone: "terracotta" },
  review_required: { label: "识别待确认", tone: "amber" },
  selected: { label: "已选定", tone: "green" },
  rejected: { label: "未采用", tone: "muted" },
  expired: { label: "已过期", tone: "muted" },
  purchased: { label: "已采购", tone: "green" },
};

const MEDIA_ROLE: Record<QuoteMediaRole, string> = {
  source: "原始资料",
  product: "商品图片",
  quote_sheet: "报价单",
  business_card: "供应商名片",
  address: "地址信息",
  other: "其他资料",
};

type QuoteEditor =
  | { kind: "quote"; item?: QuoteRequest }
  | { kind: "offer"; item?: QuoteOffer }
  | { kind: "media" }
  | null;

interface QuotesPageProps {
  project: Project;
  items: QuoteRequest[];
  search: string;
  writable: boolean;
  onReload: () => Promise<void>;
  onError: (message: string) => void;
  onToast: (message: string) => void;
}

export interface QuotesPageHandle {
  openCreate: () => void;
}

function currency(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return "待报价";
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    minimumFractionDigits: 2,
  }).format(cents / 100);
}

function dateText(value?: string | null, fallback = "未设置"): string {
  if (!value) return fallback;
  const parsed = new Date(value.length === 10 ? `${value}T00:00:00+08:00` : value);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: SHANGHAI,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(parsed);
}

function dateTimeText(value?: string | null): string {
  if (!value) return "未设置";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: SHANGHAI,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function localShanghai(value?: string | null): string {
  const date = value ? new Date(value) : new Date();
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: SHANGHAI,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value || "00";
  return `${part("year")}-${part("month")}-${part("day")}T${part("hour")}:${part("minute")}`;
}

function shanghaiIso(value: string): string | null {
  return value ? `${value}:00+08:00` : null;
}

function quantityText(quantityMilli: number | null, unit: string): string {
  if (!quantityMilli) return "数量待确认";
  const quantity = quantityMilli / 1000;
  return `${Number.isInteger(quantity) ? quantity : quantity.toFixed(3).replace(/0+$/, "").replace(/\.$/, "")} ${unit || "件"}`;
}

function getError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "version_conflict") return "报价已在别处更新，页面已经刷新，请重新提交。";
    return error.message;
  }
  return error instanceof Error ? error.message : "报价操作失败，请稍后重试";
}

function specificationText(value: Record<string, string>): string {
  return Object.entries(value).map(([key, item]) => `${key}：${item}`).join("\n");
}

function parseSpecification(value: string): Record<string, string> {
  const result: Record<string, string> = {};
  value.split(/\r?\n/).forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const separator = trimmed.search(/[：:]/);
    if (separator < 1) return;
    const key = trimmed.slice(0, separator).trim();
    const item = trimmed.slice(separator + 1).trim();
    if (key && item) result[key] = item;
  });
  return result;
}

function yuanToCents(value: string): number | null {
  if (!value.trim()) return null;
  return Math.round(Number(value) * 100);
}

export const QuotesPage = forwardRef<QuotesPageHandle, QuotesPageProps>(function QuotesPage(props, ref) {
  const { onError } = props;
  const filtered = useMemo(() => {
    const keyword = props.search.trim().toLocaleLowerCase("zh-CN");
    return props.items.filter((item) => {
      if (!keyword) return true;
      return [
        item.title,
        item.category,
        item.description,
        ...item.supplier_names,
        ...Object.entries(item.specification).flat(),
      ].join(" ").toLocaleLowerCase("zh-CN").includes(keyword);
    });
  }, [props.items, props.search]);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<QuoteDetail | null>(null);
  const [saving, setSaving] = useState(false);
  const [editor, setEditor] = useState<QuoteEditor>(null);
  const [viewerIndex, setViewerIndex] = useState<number | null>(null);
  useImperativeHandle(ref, () => ({
    openCreate: () => {
      if (props.writable) setEditor({ kind: "quote" });
    },
  }), [props.writable]);

  const activeSelectedId = filtered.some((item) => item.id === selectedId) ? selectedId : filtered[0]?.id || "";
  const visibleDetail = detail?.quote.id === activeSelectedId ? detail : null;

  const loadDetail = async (requestId: string) => {
    if (!requestId) return;
    try {
      setDetail(await api.quote(requestId));
    } catch (error) {
      onError(getError(error));
    }
  };

  useEffect(() => {
    if (!activeSelectedId) return;
    let active = true;
    void api.quote(activeSelectedId).then((result) => {
      if (active) setDetail(result);
    }).catch((error: unknown) => {
      if (active) onError(getError(error));
    });
    return () => { active = false; };
  }, [activeSelectedId, onError]);

  const runWrite = async (work: () => Promise<unknown>, message: string, nextId = selectedId) => {
    setSaving(true);
    props.onError("");
    try {
      const result = await work();
      let requestId = nextId;
      if (result && typeof result === "object" && "quote" in result) {
        requestId = (result as { quote: QuoteRequest }).quote.id;
      }
      setSelectedId(requestId);
      await props.onReload();
      await loadDetail(requestId);
      setEditor(null);
      props.onToast(message);
    } catch (error) {
      props.onError(getError(error));
      if (error instanceof ApiError && error.code === "version_conflict") await loadDetail(nextId);
    } finally {
      setSaving(false);
    }
  };

  const counts = useMemo(() => ({
    followUp: props.items.filter((item) => item.status === "inquiry" || item.status === "quoted").length,
    review: props.items.filter((item) => item.status === "review_required").length,
    selected: props.items.filter((item) => item.status === "selected" || item.status === "purchased").length,
    offers: props.items.reduce((sum, item) => sum + item.offer_count, 0),
  }), [props.items]);

  const media = visibleDetail?.media || [];

  return (
    <div className="quotes-page single-page">
      <div className="page-title quotes-title">
        <div>
          <span className="eyebrow">INQUIRY & QUOTATION</span>
          <h1>询价报价</h1>
          <p>把商品规格、供应商名片、地址和报价单放在一起，同一物品可横向比较多家报价。</p>
        </div>
      </div>

      <div className="quote-metrics" aria-label="报价概览">
        <QuoteMetric label="待跟进" value={counts.followUp} detail="询价中或等待更多报价" tone="blue" />
        <QuoteMetric label="识别待确认" value={counts.review} detail="图片与名片信息需要复核" tone="amber" />
        <QuoteMetric label="已选定" value={counts.selected} detail="仅记录选择，不自动入账" tone="green" />
        <QuoteMetric label="供应商报价" value={counts.offers} detail="全部询价下的报价数量" tone="terracotta" />
      </div>

      <div className="quote-workbench">
        <section className="quote-list-panel" aria-label="询价列表">
          <header>
            <div><strong>询价清单</strong><span>{filtered.length} 项</span></div>
            <button type="button" className="icon-button" onClick={() => void props.onReload()} aria-label="刷新询价"><IconRefresh size={18} /></button>
          </header>
          <div className="quote-list">
            {filtered.map((item) => {
              const status = REQUEST_STATUS[item.status];
              return (
                  <button key={item.id} className={`quote-list-item ${activeSelectedId === item.id ? "active" : ""}`} type="button" onClick={() => setSelectedId(item.id)}>
                  <div className="quote-list-cover">
                    {item.cover_media ? <img src={assetUrl(item.cover_media.preview_url)} alt="" /> : <IconReceipt2 size={23} />}
                  </div>
                  <div className="quote-list-copy">
                    <div><strong>{item.title}</strong><span className={`quote-status ${status.tone}`}>{status.label}</span></div>
                    <p>{item.category || "未分类"} · {quantityText(item.quantity_milli, item.unit)}</p>
                    <small>{item.offer_count} 家报价 · 最低 {currency(item.best_total_cents)}</small>
                  </div>
                </button>
              );
            })}
            {!filtered.length && (
              <div className="quote-list-empty">
                <IconSearch size={28} />
                <strong>{props.search ? "没有匹配的询价" : "还没有询价记录"}</strong>
                <span>{props.search ? "换一个关键词试试" : "记录物品和规格后，就能持续补充不同供应商报价。"}</span>
              </div>
            )}
          </div>
        </section>

        <section className="quote-detail-panel" aria-live="polite">
          {activeSelectedId && !visibleDetail ? (
            <div className="quote-detail-loading"><span /><span /><span /></div>
          ) : visibleDetail ? (
            <QuoteDetailView
              detail={visibleDetail}
              writable={props.writable}
              onEditQuote={() => setEditor({ kind: "quote", item: visibleDetail.quote })}
              onAddOffer={() => setEditor({ kind: "offer" })}
              onEditOffer={(item) => setEditor({ kind: "offer", item })}
              onSelect={(offer) => void runWrite(
                () => api.selectQuoteOffer(visibleDetail.quote.id, { offer_id: offer.id, version: visibleDetail.quote.version }),
                `已选择 ${offer.supplier_name}，不会自动生成账目`,
              )}
              onUpload={() => setEditor({ kind: "media" })}
              onOpenMedia={(index) => setViewerIndex(index)}
            />
          ) : (
            <div className="quote-detail-empty"><IconArrowLeft size={28} /><strong>从左侧选择一项询价</strong><span>这里会显示全部供应商、规格差异、联系方式与原始图片。</span></div>
          )}
        </section>
      </div>

      {editor?.kind === "quote" && (
        <QuoteRequestDialog
          project={props.project}
          item={editor.item}
          saving={saving}
          onClose={() => setEditor(null)}
          onSubmit={(body, item) => void runWrite(
            () => item ? api.updateQuote(item.id, body) : api.createQuote(body),
            item ? "询价信息已更新" : "询价已创建",
            item?.id || selectedId,
          )}
        />
      )}
      {editor?.kind === "offer" && visibleDetail && (
        <QuoteOfferDialog
          quote={visibleDetail.quote}
          item={editor.item}
          saving={saving}
          onClose={() => setEditor(null)}
          onSubmit={(body, item) => void runWrite(
            () => item ? api.updateQuoteOffer(item.id, body) : api.addQuoteOffer(visibleDetail.quote.id, body),
            item ? "供应商报价已更新" : "供应商报价已添加",
          )}
        />
      )}
      {editor?.kind === "media" && visibleDetail && (
        <QuoteMediaDialog
          detail={visibleDetail}
          saving={saving}
          onClose={() => setEditor(null)}
          onSubmit={async (files, offerId, role, onProgress) => {
            setSaving(true);
            props.onError("");
            try {
              for (let index = 0; index < files.length; index += 1) {
                const asset = await uploadMedia(
                  files[index],
                  { project_id: props.project.id, captured_at: shanghaiIso(localShanghai()), links: [] },
                  (value) => onProgress(Math.round(((index + value / 100) / files.length) * 100)),
                );
                await api.linkQuoteMedia(visibleDetail.quote.id, { media_id: asset.id, offer_id: offerId || null, role });
              }
              await props.onReload();
              await loadDetail(visibleDetail.quote.id);
              setEditor(null);
              props.onToast(`${files.length} 张报价图片已归档并关联`);
            } catch (error) {
              props.onError(getError(error));
            } finally {
              setSaving(false);
            }
          }}
        />
      )}
      {viewerIndex !== null && media[viewerIndex] && (
        <QuoteMediaViewer
          key={`${media[viewerIndex].id}-${viewerIndex}`}
          items={media}
          index={viewerIndex}
          onIndex={setViewerIndex}
          onClose={() => setViewerIndex(null)}
        />
      )}
    </div>
  );
});

function QuoteMetric({ label, value, detail, tone }: { label: string; value: number; detail: string; tone: string }) {
  return <article className={`quote-metric ${tone}`}><span>{label}</span><strong>{value}</strong><small>{detail}</small></article>;
}

function QuoteDetailView(props: {
  detail: QuoteDetail;
  writable: boolean;
  onEditQuote: () => void;
  onAddOffer: () => void;
  onEditOffer: (item: QuoteOffer) => void;
  onSelect: (item: QuoteOffer) => void;
  onUpload: () => void;
  onOpenMedia: (index: number) => void;
}) {
  const { quote, offers, media } = props.detail;
  const status = REQUEST_STATUS[quote.status];
  const validOffers = offers.filter((item) => item.total_cents !== null && !["rejected", "expired"].includes(item.effective_status));
  const bestOffer = validOffers.length ? [...validOffers].sort((a, b) => (a.total_cents || 0) - (b.total_cents || 0))[0] : null;
  return (
    <div className="quote-detail">
      <header className="quote-detail-header">
        <div>
          <div className="quote-detail-kicker"><span className={`quote-status ${status.tone}`}>{status.label}</span><span>{quote.category || "未分类"}</span></div>
          <h2>{quote.title}</h2>
          <p>{quote.description || "暂无补充说明"}</p>
        </div>
        <button className="secondary-button" type="button" disabled={!props.writable} onClick={props.onEditQuote}><IconEdit size={17} />编辑询价</button>
      </header>

      <div className="quote-facts">
        <div><span>需求数量</span><strong>{quantityText(quote.quantity_milli, quote.unit)}</strong></div>
        <div><span>已收报价</span><strong>{quote.offer_count} 家</strong></div>
        <div><span>当前最低</span><strong>{currency(quote.best_total_cents)}</strong></div>
        <div><span>下次跟进</span><strong>{dateTimeText(quote.follow_up_at)}</strong></div>
      </div>

      <section className="quote-spec-section">
        <header><h3>需求规格</h3><span>{Object.keys(quote.specification).length} 项</span></header>
        <dl className="quote-spec-grid">
          {Object.entries(quote.specification).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}
          {!Object.keys(quote.specification).length && <div className="quote-spec-empty">规格待补充，可从商品图片或报价单识别后确认。</div>}
        </dl>
      </section>

      <section className="quote-offers-section">
        <header>
          <div><h3>供应商报价</h3><span>按总价、规格、交期和有效期综合比较</span></div>
          <button className="secondary-button" type="button" disabled={!props.writable} onClick={props.onAddOffer}><IconPlus size={17} />添加报价</button>
        </header>
        <div className="offer-grid">
          {offers.map((offer) => (
            <OfferCard
              key={offer.id}
              offer={offer}
              selected={quote.selected_offer_id === offer.id}
              best={bestOffer?.id === offer.id}
              writable={props.writable}
              onEdit={() => props.onEditOffer(offer)}
              onSelect={() => props.onSelect(offer)}
            />
          ))}
          {!offers.length && <div className="offer-empty"><IconReceipt2 size={28} /><strong>还没有供应商报价</strong><span>可以从微信发送报价单图片，也可以在页面手动添加。</span></div>}
        </div>
      </section>

      <section className="quote-media-section">
        <header>
          <div><h3>报价图片与资料</h3><span>原图、名片、地址和商品规格统一保存</span></div>
          <button className="secondary-button" type="button" disabled={!props.writable} onClick={props.onUpload}><IconUpload size={17} />上传图片</button>
        </header>
        <div className="quote-media-grid">
          {media.map((item, index) => (
            <button key={`${item.id}-${item.offer_id || "request"}-${item.role}`} type="button" onClick={() => props.onOpenMedia(index)}>
              <div>{item.media_type === "image" ? <img src={assetUrl(item.preview_url || item.content_url)} alt={item.original_filename} /> : <video src={assetUrl(item.content_url)} muted preload="metadata" />}</div>
              <span>{MEDIA_ROLE[item.role]}</span>
              <strong>{item.original_filename}</strong>
            </button>
          ))}
          {!media.length && <div className="quote-media-empty"><IconPhoto size={28} /><strong>暂无图片</strong><span>上传报价单、商品图、名片或地址截图后，可在这里查看原图。</span></div>}
        </div>
      </section>
    </div>
  );
}

function OfferCard(props: { offer: QuoteOffer; selected: boolean; best: boolean; writable: boolean; onEdit: () => void; onSelect: () => void }) {
  const { offer } = props;
  const status = OFFER_STATUS[offer.effective_status];
  const selectable = !props.selected && !["rejected", "expired", "purchased"].includes(offer.effective_status);
  return (
    <article className={`offer-card ${props.selected ? "selected" : ""}`}>
      <header>
        <div>
          <div className="offer-badges"><span className={`quote-status ${status.tone}`}>{status.label}</span>{props.best && <span className="best-badge">当前最低</span>}</div>
          <h4>{offer.supplier_name}</h4>
          <p>{[offer.brand, offer.model].filter(Boolean).join(" · ") || "品牌型号待确认"}</p>
        </div>
        <button type="button" className="icon-button" disabled={!props.writable} onClick={props.onEdit} aria-label={`编辑 ${offer.supplier_name} 报价`}><IconEdit size={17} /></button>
      </header>
      <div className="offer-price"><strong>{currency(offer.total_cents)}</strong><span>{offer.unit_price_cents === null ? "暂无标准化单价" : `${currency(offer.unit_price_cents)} / ${offer.unit || "件"}`}</span></div>
      <dl>
        <div><dt>数量</dt><dd>{quantityText(offer.quantity_milli, offer.unit)}</dd></div>
        <div><dt>交期</dt><dd>{offer.lead_time_days === null ? "待确认" : `${offer.lead_time_days} 天`}</dd></div>
        <div><dt>有效期</dt><dd>{dateText(offer.valid_until)}</dd></div>
        <div><dt>含税</dt><dd>{offer.price_includes_tax ? "是" : "未注明"}</dd></div>
      </dl>
      {(offer.contact_name || offer.contact_phone || offer.supplier_address) && (
        <div className="offer-contact">
          {(offer.contact_name || offer.contact_phone) && <span><IconUser size={14} />{[offer.contact_name, offer.contact_phone].filter(Boolean).join(" · ")}</span>}
          {offer.supplier_address && <span><IconMapPin size={14} />{offer.supplier_address}</span>}
        </div>
      )}
      {offer.extraction_confidence !== null && <div className="confidence-line"><span>图片识别置信度 {offer.extraction_confidence}%</span><i><b style={{ width: `${offer.extraction_confidence}%` }} /></i></div>}
      <footer>
        {props.selected ? <span className="selected-copy"><IconCircleCheckFilled size={17} />已选定该供应商</span> : <button className="select-offer-button" type="button" disabled={!props.writable || !selectable} onClick={props.onSelect}><IconCheck size={17} />选择此报价</button>}
      </footer>
    </article>
  );
}

function Dialog({ title, subtitle, onClose, children }: { title: string; subtitle: string; onClose: () => void; children: ReactNode }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current?.focus();
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [onClose]);
  return <div className="dialog-backdrop" role="presentation"><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="quote-dialog-title"><header><div><h2 id="quote-dialog-title">{title}</h2><p>{subtitle}</p></div><button ref={closeRef} className="icon-button" type="button" onClick={onClose} aria-label="关闭"><IconX size={20} /></button></header>{children}</section></div>;
}

function Field({ label, children, wide = false, hint }: { label: string; children: ReactNode; wide?: boolean; hint?: string }) {
  return <label className={wide ? "field wide" : "field"}><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>;
}

function Actions({ saving, onClose, label }: { saving: boolean; onClose: () => void; label: string }) {
  return <div className="dialog-actions"><button className="secondary-button" type="button" onClick={onClose}>取消</button><button className="primary-button" type="submit" disabled={saving}>{saving ? "正在保存..." : label}</button></div>;
}

function QuoteRequestDialog(props: { project: Project; item?: QuoteRequest; saving: boolean; onClose: () => void; onSubmit: (body: unknown, item?: QuoteRequest) => void }) {
  const item = props.item;
  const [title, setTitle] = useState(item?.title || "");
  const [category, setCategory] = useState(item?.category || "");
  const [description, setDescription] = useState(item?.description || "");
  const [quantity, setQuantity] = useState(item?.quantity_milli ? String(item.quantity_milli / 1000) : "");
  const [unit, setUnit] = useState(item?.unit || "");
  const [status, setStatus] = useState<QuoteRequestStatus>(item?.status || "inquiry");
  const [followUp, setFollowUp] = useState(item?.follow_up_at ? localShanghai(item.follow_up_at) : "");
  const [specification, setSpecification] = useState(item ? specificationText(item.specification) : "");
  const [note, setNote] = useState(item?.note || "");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const values = {
      title,
      category,
      description,
      specification: parseSpecification(specification),
      quantity_milli: quantity ? Math.round(Number(quantity) * 1000) : null,
      unit,
      status,
      follow_up_at: shanghaiIso(followUp),
      note,
    };
    props.onSubmit(item ? { version: item.version, changes: values } : { project_id: props.project.id, ...values }, item);
  };
  return (
    <Dialog title={item ? "编辑询价" : "新增询价"} subtitle="先记录要买什么和关键规格，后续可以不断补充不同供应商报价。" onClose={props.onClose}>
      <form onSubmit={submit}>
        <div className="form-grid">
          <Field label="物品 / 服务名称" wide><input required value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：厨房墙砖 600×1200" /></Field>
          <Field label="分类"><input value={category} onChange={(event) => setCategory(event.target.value)} placeholder="主材、灯具、设备..." /></Field>
          <Field label="状态"><select value={status} onChange={(event) => setStatus(event.target.value as QuoteRequestStatus)}><option value="inquiry">询价中</option><option value="quoted">已有报价</option><option value="review_required">待确认</option><option value="selected">已选定</option><option value="purchased">已采购</option><option value="closed">已关闭</option><option value="archived">已归档</option></select></Field>
          <Field label="需求数量"><input type="number" min="0.001" step="0.001" value={quantity} onChange={(event) => setQuantity(event.target.value)} /></Field>
          <Field label="单位"><input value={unit} onChange={(event) => setUnit(event.target.value)} placeholder="片、套、米、平方米" /></Field>
          <Field label="下次跟进（上海时间）" wide><input type="datetime-local" value={followUp} onChange={(event) => setFollowUp(event.target.value)} /></Field>
          <Field label="规格（每行一项）" wide hint="格式示例：尺寸：600×1200mm"><textarea rows={5} value={specification} onChange={(event) => setSpecification(event.target.value)} placeholder={"尺寸：600×1200mm\n颜色：暖灰\n表面：柔光"} /></Field>
          <Field label="需求说明" wide><textarea rows={3} value={description} onChange={(event) => setDescription(event.target.value)} /></Field>
          <Field label="内部备注" wide><textarea rows={2} value={note} onChange={(event) => setNote(event.target.value)} /></Field>
        </div>
        <Actions saving={props.saving} onClose={props.onClose} label={item ? "保存询价" : "创建询价"} />
      </form>
    </Dialog>
  );
}

function QuoteOfferDialog(props: { quote: QuoteRequest; item?: QuoteOffer; saving: boolean; onClose: () => void; onSubmit: (body: unknown, item?: QuoteOffer) => void }) {
  const item = props.item;
  const [supplier, setSupplier] = useState(item?.supplier_name || "");
  const [contact, setContact] = useState(item?.contact_name || "");
  const [phone, setPhone] = useState(item?.contact_phone || "");
  const [address, setAddress] = useState(item?.supplier_address || "");
  const [subtotal, setSubtotal] = useState(item?.subtotal_cents === null || item?.subtotal_cents === undefined ? "" : String(item.subtotal_cents / 100));
  const [tax, setTax] = useState(item ? String(item.tax_cents / 100) : "0");
  const [shipping, setShipping] = useState(item ? String(item.shipping_cents / 100) : "0");
  const [installation, setInstallation] = useState(item ? String(item.installation_cents / 100) : "0");
  const [discount, setDiscount] = useState(item ? String(item.discount_cents / 100) : "0");
  const [total, setTotal] = useState(item?.total_cents === null || item?.total_cents === undefined ? "" : String(item.total_cents / 100));
  const [quantity, setQuantity] = useState(item?.quantity_milli ? String(item.quantity_milli / 1000) : props.quote.quantity_milli ? String(props.quote.quantity_milli / 1000) : "");
  const [unit, setUnit] = useState(item?.unit || props.quote.unit || "");
  const [brand, setBrand] = useState(item?.brand || "");
  const [model, setModel] = useState(item?.model || "");
  const [leadTime, setLeadTime] = useState(item?.lead_time_days === null || item?.lead_time_days === undefined ? "" : String(item.lead_time_days));
  const [validUntil, setValidUntil] = useState(item?.valid_until || "");
  const [includesTax, setIncludesTax] = useState(item?.price_includes_tax || false);
  const [status, setStatus] = useState<"quoted" | "review_required" | "rejected" | "purchased">(
    item?.status === "selected" || item?.status === "expired" ? "quoted" : item?.status || "quoted",
  );
  const [confidence, setConfidence] = useState(item?.extraction_confidence === null || item?.extraction_confidence === undefined ? "" : String(item.extraction_confidence));
  const [specification, setSpecification] = useState(item ? specificationText(item.specification) : specificationText(props.quote.specification));
  const [paymentTerms, setPaymentTerms] = useState(item?.payment_terms || "");
  const [warranty, setWarranty] = useState(item?.warranty || "");
  const [note, setNote] = useState(item?.note || "");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const values = {
      supplier_name: supplier,
      contact_name: contact,
      contact_phone: phone,
      supplier_address: address,
      subtotal_cents: yuanToCents(subtotal),
      tax_cents: yuanToCents(tax) || 0,
      shipping_cents: yuanToCents(shipping) || 0,
      installation_cents: yuanToCents(installation) || 0,
      discount_cents: yuanToCents(discount) || 0,
      total_cents: yuanToCents(total),
      quantity_milli: quantity ? Math.round(Number(quantity) * 1000) : null,
      unit,
      brand,
      model,
      specification: parseSpecification(specification),
      price_includes_tax: includesTax,
      lead_time_days: leadTime ? Number(leadTime) : null,
      valid_until: validUntil || null,
      payment_terms: paymentTerms,
      warranty,
      note,
      status,
      extraction_confidence: confidence ? Number(confidence) : null,
    };
    props.onSubmit(item ? { version: item.version, changes: values } : values, item);
  };
  return (
    <Dialog title={item ? "编辑供应商报价" : "添加供应商报价"} subtitle={`询价：${props.quote.title}。识别自图片的数据建议先标记为待确认。`} onClose={props.onClose}>
      <form onSubmit={submit}>
        <div className="form-grid">
          <Field label="供应商" wide><input required value={supplier} onChange={(event) => setSupplier(event.target.value)} /></Field>
          <Field label="联系人"><input value={contact} onChange={(event) => setContact(event.target.value)} /></Field>
          <Field label="联系电话"><input value={phone} onChange={(event) => setPhone(event.target.value)} /></Field>
          <Field label="供应商地址" wide><input value={address} onChange={(event) => setAddress(event.target.value)} /></Field>
          <Field label="商品小计（元）"><input type="number" min="0" step="0.01" value={subtotal} onChange={(event) => setSubtotal(event.target.value)} /></Field>
          <Field label="总价（元）"><input type="number" min="0" step="0.01" value={total} onChange={(event) => setTotal(event.target.value)} placeholder="留空时按小计和费用计算" /></Field>
          <Field label="运费（元）"><input type="number" min="0" step="0.01" value={shipping} onChange={(event) => setShipping(event.target.value)} /></Field>
          <Field label="安装费（元）"><input type="number" min="0" step="0.01" value={installation} onChange={(event) => setInstallation(event.target.value)} /></Field>
          <Field label="税费（元）"><input type="number" min="0" step="0.01" value={tax} onChange={(event) => setTax(event.target.value)} /></Field>
          <Field label="优惠（元）"><input type="number" min="0" step="0.01" value={discount} onChange={(event) => setDiscount(event.target.value)} /></Field>
          <Field label="报价数量"><input type="number" min="0.001" step="0.001" value={quantity} onChange={(event) => setQuantity(event.target.value)} /></Field>
          <Field label="单位"><input value={unit} onChange={(event) => setUnit(event.target.value)} /></Field>
          <Field label="品牌"><input value={brand} onChange={(event) => setBrand(event.target.value)} /></Field>
          <Field label="型号"><input value={model} onChange={(event) => setModel(event.target.value)} /></Field>
          <Field label="交期（天）"><input type="number" min="0" step="1" value={leadTime} onChange={(event) => setLeadTime(event.target.value)} /></Field>
          <Field label="有效期"><input type="date" value={validUntil} onChange={(event) => setValidUntil(event.target.value)} /></Field>
          <Field label="状态"><select value={status} onChange={(event) => setStatus(event.target.value as typeof status)}><option value="quoted">有效报价</option><option value="review_required">识别待确认</option><option value="rejected">未采用</option><option value="purchased">已采购</option></select></Field>
          <Field label="识别置信度"><input type="number" min="0" max="100" step="1" value={confidence} onChange={(event) => setConfidence(event.target.value)} placeholder="0 - 100" /></Field>
          <Field label="规格（每行一项）" wide><textarea rows={5} value={specification} onChange={(event) => setSpecification(event.target.value)} /></Field>
          <Field label="付款条件" wide><textarea rows={2} value={paymentTerms} onChange={(event) => setPaymentTerms(event.target.value)} /></Field>
          <Field label="质保" wide><textarea rows={2} value={warranty} onChange={(event) => setWarranty(event.target.value)} /></Field>
          <Field label="备注" wide><textarea rows={2} value={note} onChange={(event) => setNote(event.target.value)} /></Field>
          <label className="check-field wide"><input type="checkbox" checked={includesTax} onChange={(event) => setIncludesTax(event.target.checked)} /><span>报价明确含税</span></label>
        </div>
        <Actions saving={props.saving} onClose={props.onClose} label={item ? "保存报价" : "添加报价"} />
      </form>
    </Dialog>
  );
}

function QuoteMediaDialog(props: { detail: QuoteDetail; saving: boolean; onClose: () => void; onSubmit: (files: File[], offerId: string, role: QuoteMediaRole, onProgress: (value: number) => void) => Promise<void> }) {
  const [files, setFiles] = useState<File[]>([]);
  const [offerId, setOfferId] = useState("");
  const [role, setRole] = useState<QuoteMediaRole>("quote_sheet");
  const [progress, setProgress] = useState(0);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    await props.onSubmit(files, offerId, role, setProgress);
  };
  return (
    <Dialog title="上传报价图片" subtitle="原件保存在私有媒体库，可关联到整项询价或某一家供应商报价。" onClose={props.onClose}>
      <form onSubmit={(event) => void submit(event)}>
        <label className="drop-zone">
          <IconUpload size={32} />
          <strong>{files.length ? `已选择 ${files.length} 个文件` : "选择图片或视频"}</strong>
          <span>{files.length ? files.map((file) => file.name).join("、") : "报价单、商品图、名片和地址截图均可"}</span>
          <input type="file" multiple accept="image/jpeg,image/png,image/webp,image/heic,image/heif,video/mp4,video/quicktime,video/webm" onChange={(event) => setFiles(Array.from(event.target.files || []))} />
        </label>
        <div className="form-grid">
          <Field label="资料类型"><select value={role} onChange={(event) => setRole(event.target.value as QuoteMediaRole)}>{Object.entries(MEDIA_ROLE).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>
          <Field label="关联供应商"><select value={offerId} onChange={(event) => setOfferId(event.target.value)}><option value="">关联整项询价</option>{props.detail.offers.map((item) => <option key={item.id} value={item.id}>{item.supplier_name}</option>)}</select></Field>
        </div>
        {props.saving && <div className="upload-progress"><div><i style={{ width: `${progress}%` }} /></div><span>{progress}% · 正在校验、归档并建立报价关联</span></div>}
        <div className="dialog-actions"><button className="secondary-button" type="button" onClick={props.onClose}>取消</button><button className="primary-button" type="submit" disabled={props.saving || files.length === 0}>{props.saving ? "正在上传..." : "上传并关联"}</button></div>
      </form>
    </Dialog>
  );
}

function QuoteMediaViewer(props: { items: QuoteMediaAsset[]; index: number; onIndex: (index: number) => void; onClose: () => void }) {
  const item = props.items[props.index];
  const [zoom, setZoom] = useState(1);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") props.onClose();
      if (event.key === "ArrowLeft" && props.items.length > 1) props.onIndex((props.index - 1 + props.items.length) % props.items.length);
      if (event.key === "ArrowRight" && props.items.length > 1) props.onIndex((props.index + 1) % props.items.length);
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [props]);
  const previous = () => props.onIndex((props.index - 1 + props.items.length) % props.items.length);
  const next = () => props.onIndex((props.index + 1) % props.items.length);
  return (
    <div className="quote-media-viewer" role="dialog" aria-modal="true" aria-label="报价原图查看器">
      <header>
        <button type="button" onClick={props.onClose} aria-label="关闭"><IconX size={22} /></button>
        <div><strong>{item.original_filename}</strong><span>{props.index + 1} / {props.items.length} · {MEDIA_ROLE[item.role]}</span></div>
        <div className="viewer-tools">
          {item.media_type === "image" && <><button type="button" onClick={() => setZoom((value) => Math.max(0.5, value - 0.25))} aria-label="缩小"><IconZoomOut size={20} /></button><span>{Math.round(zoom * 100)}%</span><button type="button" onClick={() => setZoom((value) => Math.min(4, value + 0.25))} aria-label="放大"><IconZoomIn size={20} /></button></>}
          <a href={assetUrl(item.content_url)} download={item.original_filename} aria-label="下载原图"><IconDownload size={20} /></a>
        </div>
      </header>
      <div className="quote-viewer-stage">
        {props.items.length > 1 && <button className="quote-viewer-prev" type="button" onClick={previous} aria-label="上一张"><IconChevronLeft size={27} /></button>}
        <div className="quote-viewer-canvas">
          {item.media_type === "video" ? <video src={assetUrl(item.content_url)} controls autoPlay poster={assetUrl(item.preview_url)} /> : <img src={assetUrl(item.content_url)} alt={item.original_filename} style={{ transform: `scale(${zoom})` }} />}
        </div>
        {props.items.length > 1 && <button className="quote-viewer-next" type="button" onClick={next} aria-label="下一张"><IconChevronRight size={27} /></button>}
      </div>
      <aside>
        <span><IconPhoto size={16} />{MEDIA_ROLE[item.role]}</span>
        <span><IconCalendarEvent size={16} />{dateTimeText(item.captured_at || item.uploaded_at)}</span>
        <span><IconHome size={16} />{item.offer_id ? "已关联供应商报价" : "已关联整项询价"}</span>
      </aside>
    </div>
  );
}
