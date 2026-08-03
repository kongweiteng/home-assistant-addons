import {
  IconAdjustmentsHorizontal,
  IconArchive,
  IconArrowRight,
  IconCalendarEvent,
  IconCamera,
  IconCheck,
  IconChevronDown,
  IconCircleCheckFilled,
  IconCoinYuan,
  IconEdit,
  IconFlag,
  IconHome,
  IconLayoutDashboard,
  IconLock,
  IconPhoto,
  IconPlus,
  IconReceipt2,
  IconRefresh,
  IconSearch,
  IconSettings,
  IconTimeline,
  IconTrash,
  IconUpload,
  IconVideo,
  IconX,
} from "@tabler/icons-react";
import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, assetUrl, loadSession, uploadMedia } from "./api";
import type {
  Area,
  HubData,
  MediaAsset,
  PageKey,
  Project,
  SessionState,
  Stage,
  TimelineEvent,
  Transaction,
} from "./types";

const EMPTY_DATA: HubData = {
  dashboard: null,
  stages: [],
  areas: [],
  timeline: [],
  transactions: [],
  summary: null,
  media: [],
};

const NAV_ITEMS: Array<{ key: PageKey; label: string; icon: typeof IconHome }> = [
  { key: "overview", label: "总览", icon: IconLayoutDashboard },
  { key: "timeline", label: "时间线", icon: IconTimeline },
  { key: "ledger", label: "资金账目", icon: IconCoinYuan },
  { key: "media", label: "图片视频", icon: IconPhoto },
  { key: "stages", label: "装修阶段", icon: IconFlag },
  { key: "settings", label: "设置", icon: IconSettings },
];

const STAGE_LABELS: Record<Stage["status"], string> = {
  planned: "待开始",
  active: "进行中",
  completed: "已完成",
  archived: "已归档",
};

const EVENT_META: Record<TimelineEvent["event_type"], { label: string; tone: string }> = {
  progress: { label: "施工进展", tone: "terracotta" },
  note: { label: "现场记录", tone: "blue" },
  decision: { label: "设计决定", tone: "purple" },
  inspection: { label: "验收检查", tone: "green" },
  milestone: { label: "阶段节点", tone: "amber" },
};

type EditorState =
  | { kind: "payment"; item?: Transaction }
  | { kind: "refund"; item: Transaction }
  | { kind: "undo"; item: Transaction }
  | { kind: "stage"; item?: Stage }
  | { kind: "event"; item?: TimelineEvent }
  | { kind: "project"; item?: Project }
  | { kind: "area"; item?: Area }
  | { kind: "upload" }
  | null;

function currency(cents = 0): string {
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    minimumFractionDigits: 2,
  }).format(cents / 100);
}

function shortDate(value?: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value.length === 10 ? `${value}T00:00:00` : value);
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(parsed);
}

function fullDate(value?: string | null): string {
  if (!value) return "未设置";
  const parsed = new Date(value.length === 10 ? `${value}T00:00:00` : value);
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(parsed);
}

function timeText(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}

function percent(value: number | null | undefined): number {
  return Math.max(0, Math.min(100, Math.round((value || 0) * 100)));
}

function localDateTime(value?: string): string {
  const date = value ? new Date(value) : new Date();
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function getErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "version_conflict") return "数据已在别处更新，已为你刷新，请重新提交。";
    if (error.code === "stage_active_conflict") return "当前已有进行中阶段，请先完成或调整现阶段。";
    return error.message;
  }
  return error instanceof Error ? error.message : "操作失败，请稍后重试";
}

export function App() {
  const [page, setPage] = useState<PageKey>("overview");
  const [session, setSession] = useState<SessionState | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [data, setData] = useState<HubData>(EMPTY_DATA);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [editor, setEditor] = useState<EditorState>(null);
  const [selectedMedia, setSelectedMedia] = useState<MediaAsset | null>(null);
  const [search, setSearch] = useState("");
  const [stageFilter, setStageFilter] = useState("");
  const [areaFilter, setAreaFilter] = useState("");
  const [filtersOpen, setFiltersOpen] = useState(false);

  const activeProject = projects.find((item) => item.id === projectId) || null;
  const activeStage = data.dashboard?.active_stage || data.stages.find((item) => item.status === "active") || null;
  const writable = Boolean(session?.writable);

  const loadProject = useCallback(async (targetProjectId: string) => {
    if (!targetProjectId) {
      setData(EMPTY_DATA);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const [dashboard, stages, areas, timeline, ledger, media] = await Promise.all([
        api.dashboard(targetProjectId),
        api.stages(targetProjectId),
        api.areas(targetProjectId),
        api.timeline(targetProjectId),
        api.ledger(targetProjectId),
        api.media(targetProjectId),
      ]);
      setData({
        dashboard,
        stages: stages.items,
        areas: areas.items,
        timeline: timeline.items,
        transactions: ledger.items,
        summary: ledger.summary,
        media: media.items,
      });
    } catch (caught) {
      setError(getErrorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshProjects = useCallback(async (preferred?: string) => {
    const result = await api.projects();
    setProjects(result.items);
    const remembered = preferred || localStorage.getItem("renovation-hub-project") || "";
    const selected = result.items.some((item) => item.id === remembered) ? remembered : result.items[0]?.id || "";
    setProjectId(selected);
    if (selected) localStorage.setItem("renovation-hub-project", selected);
    return selected;
  }, []);

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const currentSession = await loadSession();
        if (!active) return;
        setSession(currentSession);
        const selected = await refreshProjects();
        if (active) await loadProject(selected);
      } catch (caught) {
        if (active) {
          setError(getErrorMessage(caught));
          setLoading(false);
        }
      }
    })();
    return () => {
      active = false;
    };
  }, [loadProject, refreshProjects]);

  useEffect(() => {
    if (!toast) return;
    const timeout = window.setTimeout(() => setToast(""), 3200);
    return () => window.clearTimeout(timeout);
  }, [toast]);

  const refresh = useCallback(async () => {
    const currentSession = await loadSession();
    setSession(currentSession);
    await refreshProjects(projectId);
    await loadProject(projectId);
  }, [loadProject, projectId, refreshProjects]);

  const runWrite = async (work: () => Promise<unknown>, message: string, projectChanged = false) => {
    setSaving(true);
    setError("");
    try {
      const result = await work();
      let nextProject = projectId;
      if (projectChanged && result && typeof result === "object" && "project" in result) {
        nextProject = (result as { project: Project }).project.id;
      }
      await refreshProjects(nextProject);
      await loadProject(nextProject);
      setEditor(null);
      setToast(message);
    } catch (caught) {
      setError(getErrorMessage(caught));
      if (caught instanceof ApiError && caught.code === "version_conflict") await loadProject(projectId);
    } finally {
      setSaving(false);
    }
  };

  const openEditor = (next: EditorState) => {
    if (!writable && next?.kind !== "project") {
      setToast("当前为只读模式；正式切换 writer 后即可编辑。 ");
      return;
    }
    setEditor(next);
  };

  const changeProject = (nextId: string) => {
    setProjectId(nextId);
    localStorage.setItem("renovation-hub-project", nextId);
    setStageFilter("");
    setAreaFilter("");
    void loadProject(nextId);
  };

  const filteredTransactions = useMemo(() => {
    const folded = search.trim().toLocaleLowerCase("zh-CN");
    return data.transactions.filter((item) => {
      if (stageFilter && item.context?.stage_id !== stageFilter) return false;
      if (areaFilter && item.context?.area_id !== areaFilter) return false;
      if (!folded) return true;
      return [item.main_category, item.merchant, item.note, ...item.tags].join(" ").toLocaleLowerCase("zh-CN").includes(folded);
    });
  }, [areaFilter, data.transactions, search, stageFilter]);

  const filteredTimeline = useMemo(() => {
    const folded = search.trim().toLocaleLowerCase("zh-CN");
    return data.timeline.filter((item) => {
      if (stageFilter && item.stage_id !== stageFilter) return false;
      if (areaFilter && item.area_id !== areaFilter) return false;
      return !folded || `${item.title} ${item.description} ${item.area_name || ""}`.toLocaleLowerCase("zh-CN").includes(folded);
    });
  }, [areaFilter, data.timeline, search, stageFilter]);

  const filteredMedia = useMemo(() => {
    const folded = search.trim().toLocaleLowerCase("zh-CN");
    return data.media.filter((item) => {
      if (stageFilter && !item.links.some((link) => link.target_type === "stage" && link.target_id === stageFilter)) return false;
      if (areaFilter && !item.links.some((link) => link.target_type === "area" && link.target_id === areaFilter)) return false;
      return !folded || item.original_filename.toLocaleLowerCase("zh-CN").includes(folded);
    });
  }, [areaFilter, data.media, search, stageFilter]);

  return (
    <div className="app-shell">
      <Sidebar
        page={page}
        project={activeProject}
        onNavigate={setPage}
      />
      <main className="workspace">
        <Header
          projects={projects}
          projectId={projectId}
          activeStage={activeStage}
          search={search}
          stageFilter={stageFilter}
          areaFilter={areaFilter}
          stages={data.stages}
          areas={data.areas}
          writable={writable}
          filtersOpen={filtersOpen}
          onProjectChange={changeProject}
          onSearch={setSearch}
          onStageFilter={setStageFilter}
          onAreaFilter={setAreaFilter}
          onToggleFilters={() => setFiltersOpen((value) => !value)}
          onAdd={() => openEditor({ kind: "payment" })}
        />

        {error && (
          <div className="error-banner" role="alert">
            <span>{error}</span>
            <button type="button" onClick={() => setError("")} aria-label="关闭错误"><IconX size={18} /></button>
          </div>
        )}

        {!writable && session && (
          <div className="readonly-banner">
            <IconLock size={17} /> 当前为 {session.writer_mode}：数据可完整查看，写操作保持锁定。
          </div>
        )}

        <section className="page-content" aria-busy={loading}>
          {loading ? (
            <LoadingState />
          ) : !activeProject ? (
            <EmptyProject onCreate={() => setEditor({ kind: "project" })} />
          ) : (
            <>
              {page === "overview" && (
                <OverviewPage
                  data={data}
                  project={activeProject}
                  filteredMedia={filteredMedia}
                  onNavigate={setPage}
                  onMedia={setSelectedMedia}
                  onEditEvent={(item) => openEditor({ kind: "event", item })}
                />
              )}
              {page === "timeline" && (
                <TimelinePage
                  events={filteredTimeline}
                  media={data.media}
                  areas={data.areas}
                  onCreate={() => openEditor({ kind: "event" })}
                  onEdit={(item) => openEditor({ kind: "event", item })}
                  onMedia={setSelectedMedia}
                  writable={writable}
                />
              )}
              {page === "ledger" && (
                <LedgerPage
                  transactions={filteredTransactions}
                  allTransactions={data.transactions}
                  summary={data.summary}
                  project={activeProject}
                  stages={data.stages}
                  areas={data.areas}
                  writable={writable}
                  onAdd={() => openEditor({ kind: "payment" })}
                  onEdit={(item) => openEditor({ kind: "payment", item })}
                  onRefund={(item) => openEditor({ kind: "refund", item })}
                  onUndo={(item) => openEditor({ kind: "undo", item })}
                />
              )}
              {page === "media" && (
                <MediaPage
                  items={filteredMedia}
                  areas={data.areas}
                  stages={data.stages}
                  writable={writable}
                  onUpload={() => openEditor({ kind: "upload" })}
                  onOpen={setSelectedMedia}
                />
              )}
              {page === "stages" && (
                <StagesPage
                  stages={data.stages}
                  events={data.timeline}
                  writable={writable}
                  onCreate={() => openEditor({ kind: "stage" })}
                  onEdit={(item) => openEditor({ kind: "stage", item })}
                />
              )}
              {page === "settings" && (
                <SettingsPage
                  project={activeProject}
                  areas={data.areas}
                  session={session}
                  writable={writable}
                  mediaCount={data.media.length}
                  onEditProject={() => openEditor({ kind: "project", item: activeProject })}
                  onAddArea={() => openEditor({ kind: "area" })}
                  onEditArea={(item) => openEditor({ kind: "area", item })}
                  onRefresh={() => void refresh()}
                />
              )}
            </>
          )}
        </section>
      </main>

      <MobileNav page={page} onNavigate={setPage} />

      {editor && activeProject && (
        <EditorDialog
          editor={editor}
          project={activeProject}
          stages={data.stages}
          areas={data.areas}
          saving={saving}
          onClose={() => setEditor(null)}
          onPayment={(body, item) => void runWrite(
            () => item ? api.correctPayment(item.id, body) : api.addPayment(body),
            item ? "账目已更新" : "账目已记录",
          )}
          onRefund={(body) => void runWrite(() => api.addRefund(body), "退款已记录")}
          onUndo={(item, reason) => void runWrite(() => api.undoTransaction(item.id, { version: item.version, reason }), "账目已撤销")}
          onStage={(body, item) => void runWrite(
            () => item ? api.updateStage(item.id, body) : api.createStage(body),
            item ? "阶段已更新" : "阶段已创建",
          )}
          onEvent={(body, item) => void runWrite(
            () => item ? api.updateEvent(item.id, body) : api.createEvent(body),
            item ? "记录已更新" : "现场记录已创建",
          )}
          onProject={(body, item) => void runWrite(
            () => item ? api.updateProject(item.id, body) : api.createProject(body),
            item ? "项目信息已更新" : "装修项目已创建",
            true,
          )}
          onArea={(body, item) => void runWrite(
            () => item ? api.updateArea(item.id, body) : api.createArea(body),
            item ? "空间已更新" : "空间已创建",
          )}
          onUpload={async (files, metadata, onProgress) => {
            setSaving(true);
            try {
              for (let index = 0; index < files.length; index += 1) {
                await uploadMedia(files[index], metadata, (value) => onProgress(Math.round(((index + value / 100) / files.length) * 100)));
              }
              await loadProject(projectId);
              setEditor(null);
              setToast(`${files.length} 个媒体文件已归档`);
            } catch (caught) {
              setError(getErrorMessage(caught));
            } finally {
              setSaving(false);
            }
          }}
        />
      )}

      {editor?.kind === "project" && !activeProject && (
        <ProjectDialog
          item={editor.item}
          saving={saving}
          onClose={() => setEditor(null)}
          onSubmit={(body, item) => void runWrite(
            () => item ? api.updateProject(item.id, body) : api.createProject(body),
            item ? "项目信息已更新" : "装修项目已创建",
            true,
          )}
        />
      )}

      {selectedMedia && (
        <MediaViewer
          item={selectedMedia}
          area={areaNameFor(selectedMedia, data.areas)}
          stage={stageNameFor(selectedMedia, data.stages)}
          onClose={() => setSelectedMedia(null)}
        />
      )}
      {toast && <div className="toast" role="status"><IconCircleCheckFilled size={18} />{toast}</div>}
    </div>
  );
}

function Sidebar({ page, project, onNavigate }: { page: PageKey; project: Project | null; onNavigate: (page: PageKey) => void }) {
  return (
    <aside className="sidebar">
      <div className="brand"><span>筑记</span><i>·</i><strong>装修档案</strong></div>
      <nav className="side-nav" aria-label="主导航">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          return (
            <button key={item.key} className={page === item.key ? "active" : ""} type="button" onClick={() => onNavigate(item.key)}>
              <Icon size={21} stroke={1.7} />
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>
      <div className="project-mini-card">
        <div className="project-mini-title"><IconHome size={16} /><span>{project?.name || "尚未创建项目"}</span></div>
        <dl>
          <div><dt>预算</dt><dd>{project ? currency(project.budget_cents) : "—"}</dd></div>
          <div><dt>状态</dt><dd>{project?.status === "active" ? "装修中" : project?.status || "—"}</dd></div>
        </dl>
      </div>
    </aside>
  );
}

function MobileNav({ page, onNavigate }: { page: PageKey; onNavigate: (page: PageKey) => void }) {
  return (
    <nav className="mobile-nav" aria-label="手机导航">
      {NAV_ITEMS.slice(0, 5).map((item) => {
        const Icon = item.icon;
        return <button key={item.key} className={page === item.key ? "active" : ""} type="button" onClick={() => onNavigate(item.key)}><Icon size={20} /><span>{item.label.replace("资金", "")}</span></button>;
      })}
    </nav>
  );
}

function Header(props: {
  projects: Project[];
  projectId: string;
  activeStage: Stage | null;
  search: string;
  stageFilter: string;
  areaFilter: string;
  stages: Stage[];
  areas: Area[];
  writable: boolean;
  filtersOpen: boolean;
  onProjectChange: (value: string) => void;
  onSearch: (value: string) => void;
  onStageFilter: (value: string) => void;
  onAreaFilter: (value: string) => void;
  onToggleFilters: () => void;
  onAdd: () => void;
}) {
  const project = props.projects.find((item) => item.id === props.projectId);
  return (
    <header className="topbar">
      <div className="project-heading">
        <label className="project-picker">
          <span>{project?.name || "装修档案"}</span>
          <IconChevronDown size={17} />
          <select value={props.projectId} onChange={(event) => props.onProjectChange(event.target.value)} aria-label="选择装修项目">
            {props.projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <div className="project-subline">
          <span>当前阶段：</span>
          <b className="stage-dot"><i />{props.activeStage?.name || "尚未开始"}</b>
          <span className="planned-date"><IconCalendarEvent size={15} />预计完成：{fullDate(props.activeStage?.planned_end)}</span>
        </div>
      </div>
      <div className="topbar-actions">
        <label className="search-box"><IconSearch size={18} /><input value={props.search} onChange={(event) => props.onSearch(event.target.value)} placeholder="搜索记录、标签、内容..." /></label>
        <select className="select-control" value={props.stageFilter} onChange={(event) => props.onStageFilter(event.target.value)} aria-label="筛选阶段"><option value="">全部阶段</option>{props.stages.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
        <select className="select-control" value={props.areaFilter} onChange={(event) => props.onAreaFilter(event.target.value)} aria-label="筛选空间"><option value="">全部空间</option>{props.areas.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
        <button className={`filter-button ${props.filtersOpen ? "active" : ""}`} type="button" onClick={props.onToggleFilters}><IconAdjustmentsHorizontal size={18} />筛选</button>
        <button className="primary-button" type="button" onClick={props.onAdd} disabled={!props.writable}><IconPlus size={19} />新增记录</button>
      </div>
      {props.filtersOpen && <div className="filter-summary">已显示：{props.stageFilter ? "指定阶段" : "全部阶段"} · {props.areaFilter ? "指定空间" : "全部空间"} · 输入关键词可跨账目、现场记录和媒体过滤。</div>}
    </header>
  );
}

function LoadingState() {
  return <div className="loading-grid" aria-label="正在加载"><div /><div /><div /><div /><div /><div /></div>;
}

function EmptyProject({ onCreate }: { onCreate: () => void }) {
  return <div className="empty-state"><div className="empty-icon"><IconHome size={38} /></div><h1>从一个装修项目开始</h1><p>创建项目后即可管理阶段、账目、现场记录以及图片视频档案。</p><button className="primary-button" type="button" onClick={onCreate}><IconPlus size={19} />创建装修项目</button></div>;
}

function SectionCard({ title, action, children, className = "" }: { title: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`section-card ${className}`}><header><h2>{title}</h2>{action}</header>{children}</section>;
}

function OverviewPage(props: {
  data: HubData;
  project: Project;
  filteredMedia: MediaAsset[];
  onNavigate: (page: PageKey) => void;
  onMedia: (item: MediaAsset) => void;
  onEditEvent: (item: TimelineEvent) => void;
}) {
  const { data, project } = props;
  const used = data.summary?.net_amount_cents || 0;
  const budgetRatio = percent(data.dashboard?.budget_used_ratio);
  const videos = data.media.filter((item) => item.media_type === "video").length;
  const categories = Object.entries(data.summary?.category_totals || {}).sort((a, b) => b[1] - a[1]);
  return (
    <div className="overview-grid">
      <div className="metric-row">
        <MetricCard label="总支出" value={currency(used)} detail={`预算总额 ${currency(project.budget_cents)}`} icon={<IconReceipt2 />} progress={budgetRatio} tone="terracotta" />
        <MetricCard label="预算使用" value={`${budgetRatio}%`} detail={`已用 ${currency(used)} / 预算 ${currency(project.budget_cents)}`} icon={<IconCoinYuan />} progress={budgetRatio} tone="terracotta" />
        <MetricCard label="当前阶段" value={data.dashboard?.active_stage?.name || "待启动"} detail={`开始 ${fullDate(data.dashboard?.active_stage?.actual_start || data.dashboard?.active_stage?.planned_start)}`} icon={<IconFlag />} tone="green" />
        <MetricCard label="图片视频" value={`${data.media.length}`} suffix={`/ ${videos}`} detail={`本阶段记录 ${stageMediaCount(data.media, data.dashboard?.active_stage?.id)}`} icon={<IconPhoto />} progress={Math.min(100, data.media.length * 6)} tone="purple" />
      </div>

      <SectionCard title="施工进度" action={<button className="text-button" type="button" onClick={() => props.onNavigate("stages")}>查看全部阶段<IconArrowRight size={16} /></button>} className="progress-card">
        <StageRail stages={data.stages} />
        <div className="active-stage-panel">
          <div className="active-stage-copy">
            <div className="active-stage-title"><strong>{data.dashboard?.active_stage?.name || "当前阶段"}</strong><span>进行中</span></div>
            {(data.dashboard?.recent_events || []).slice(0, 5).map((item) => <button type="button" key={item.id} onClick={() => props.onEditEvent(item)}><IconCheck size={14} />{item.title}<time>{shortDate(item.occurred_at)}</time></button>)}
            {!data.dashboard?.recent_events.length && <p className="muted-empty">还没有阶段记录，新增一条现场动态吧。</p>}
          </div>
          {props.filteredMedia[0] ? <button className="stage-hero" type="button" onClick={() => props.onMedia(props.filteredMedia[0])}><img src={assetUrl(props.filteredMedia[0].preview_url)} alt={props.filteredMedia[0].original_filename} /><span>查看本阶段 <IconArrowRight size={15} /></span></button> : <div className="stage-hero placeholder"><IconCamera size={36} /><span>等待现场影像</span></div>}
        </div>
      </SectionCard>

      <SectionCard title="空间影像" action={<button className="text-button" type="button" onClick={() => props.onNavigate("media")}>全部空间<IconArrowRight size={16} /></button>} className="space-media-card">
        <MediaMosaic items={props.filteredMedia.slice(0, 6)} areas={data.areas} onOpen={props.onMedia} />
      </SectionCard>

      <SectionCard title="近期动态" action={<button className="text-button" type="button" onClick={() => props.onNavigate("timeline")}>查看全部动态<IconArrowRight size={16} /></button>} className="recent-card">
        <CompactTimeline events={data.timeline.slice(0, 5)} media={data.media} onEdit={props.onEditEvent} />
      </SectionCard>

      <SectionCard title="资金使用构成" action={<button className="text-button" type="button" onClick={() => props.onNavigate("ledger")}>查看明细<IconArrowRight size={16} /></button>} className="budget-card">
        <div className="budget-table">
          <div className="budget-head"><span>项目</span><span>已用金额</span><span>占比</span><span>预算余额</span><span>使用进度</span></div>
          {categories.slice(0, 6).map(([name, amount]) => {
            const ratio = used ? Math.round((amount / used) * 100) : 0;
            return <div className="budget-row" key={name}><strong>{name}</strong><span>{currency(amount)}</span><span>{ratio}%</span><span>{currency(Math.max(0, project.budget_cents - amount))}</span><i><b style={{ width: `${Math.min(100, ratio)}%` }} /></i></div>;
          })}
          {!categories.length && <p className="muted-empty">新增账目后，这里会自动形成资金构成。</p>}
          <div className="budget-total"><strong>合计</strong><b>{currency(used)}</b><span>{budgetRatio}%</span><span>{currency(data.dashboard?.budget_remaining_cents)}</span><i><b style={{ width: `${budgetRatio}%` }} /></i></div>
        </div>
      </SectionCard>
    </div>
  );
}

function MetricCard({ label, value, suffix, detail, icon, progress, tone }: { label: string; value: string; suffix?: string; detail: string; icon: ReactNode; progress?: number; tone: string }) {
  return <article className={`metric-card ${tone}`}><div className="metric-label"><span>{label}</span><i>{icon}</i></div><strong>{value}<small>{suffix}</small></strong><p>{detail}</p>{typeof progress === "number" && <div className="progress-line"><i style={{ width: `${progress}%` }} /></div>}</article>;
}

function StageRail({ stages }: { stages: Stage[] }) {
  return <div className="stage-rail">{stages.filter((item) => item.status !== "archived").map((item, index) => <div className={`stage-node ${item.status}`} key={item.id}><div className="node-track"><span>{item.status === "completed" ? <IconCheck size={18} /> : index + 1}</span></div><strong>{item.name}</strong><small>{shortDate(item.planned_start)}–{shortDate(item.planned_end)}</small></div>)}</div>;
}

function MediaMosaic({ items, areas, onOpen }: { items: MediaAsset[]; areas: Area[]; onOpen: (item: MediaAsset) => void }) {
  if (!items.length) return <div className="media-empty"><IconPhoto size={38} /><p>上传图片或视频后，可按空间形成施工影像档案。</p></div>;
  return <div className="media-mosaic">{items.map((item) => <button key={item.id} type="button" className="media-tile" onClick={() => onOpen(item)}><img src={assetUrl(item.preview_url)} alt={item.original_filename} />{item.media_type === "video" && <span className="play-badge"><IconVideo size={21} /></span>}<span className="media-caption"><b>{areaNameFor(item, areas)}</b><small>{item.media_type === "video" ? formatDuration(item.duration_ms) : `${item.width || "—"}×${item.height || "—"}`}</small></span></button>)}</div>;
}

function CompactTimeline({ events, media, onEdit }: { events: TimelineEvent[]; media: MediaAsset[]; onEdit: (item: TimelineEvent) => void }) {
  if (!events.length) return <p className="muted-empty">现场记录将按时间自动汇聚到这里。</p>;
  return <div className="compact-timeline">{events.map((item) => { const attached = media.filter((asset) => asset.links.some((link) => link.target_type === "event" && link.target_id === item.id)).slice(0, 4); return <button type="button" key={item.id} onClick={() => onEdit(item)}><time><b>{shortDate(item.occurred_at)}</b><span>{timeText(item.occurred_at)}</span></time><i className={EVENT_META[item.event_type].tone}><IconCalendarEvent size={17} /></i><div className="compact-copy"><strong>{item.title}<em>{item.stage_name}</em></strong><p>{item.description || "无补充说明"}</p></div><div className="compact-thumbs">{attached.map((asset) => <img key={asset.id} src={assetUrl(asset.preview_url)} alt="" />)}{attached.length > 0 && <span>+{attached.length}</span>}</div><IconChevronDown className="row-menu" size={17} /></button>; })}</div>;
}

function TimelinePage({ events, media, areas, onCreate, onEdit, onMedia, writable }: { events: TimelineEvent[]; media: MediaAsset[]; areas: Area[]; onCreate: () => void; onEdit: (item: TimelineEvent) => void; onMedia: (item: MediaAsset) => void; writable: boolean }) {
  return <div className="single-page"><div className="page-title"><div><span className="eyebrow">CONSTRUCTION JOURNAL</span><h1>施工时间线</h1><p>把现场进展、验收、决定和影像串成一条连续记录。</p></div><button className="primary-button" type="button" onClick={onCreate} disabled={!writable}><IconPlus size={19} />新增动态</button></div><div className="timeline-page-list">{events.map((item) => { const attached = media.filter((asset) => asset.links.some((link) => link.target_type === "event" && link.target_id === item.id)); return <article key={item.id} className="timeline-entry"><div className="timeline-date"><strong>{shortDate(item.occurred_at)}</strong><span>{timeText(item.occurred_at)}</span></div><div className={`timeline-pin ${EVENT_META[item.event_type].tone}`}><IconCalendarEvent size={19} /></div><div className="timeline-body"><header><div><span className={`event-badge ${EVENT_META[item.event_type].tone}`}>{EVENT_META[item.event_type].label}</span><h2>{item.title}</h2></div><button type="button" onClick={() => onEdit(item)} disabled={!writable}><IconEdit size={18} /></button></header><p>{item.description || "无补充说明"}</p><div className="timeline-meta"><span><IconFlag size={15} />{item.stage_name || "未关联阶段"}</span><span><IconHome size={15} />{item.area_name || "全屋"}</span></div>{attached.length > 0 && <div className="timeline-gallery">{attached.map((asset) => <button type="button" key={asset.id} onClick={() => onMedia(asset)}><img src={assetUrl(asset.preview_url)} alt={asset.original_filename} />{asset.media_type === "video" && <IconVideo size={18} />}</button>)}</div>}</div></article>; })}{!events.length && <div className="empty-inline"><IconTimeline size={38} /><h2>还没有符合条件的现场记录</h2><p>调整筛选条件，或新增第一条施工动态。</p></div>}</div><aside className="timeline-area-index"><h3>空间索引</h3>{areas.map((area) => <span key={area.id}>{area.name}<b>{events.filter((item) => item.area_id === area.id).length}</b></span>)}</aside></div>;
}

function LedgerPage(props: { transactions: Transaction[]; allTransactions: Transaction[]; summary: HubData["summary"]; project: Project; stages: Stage[]; areas: Area[]; writable: boolean; onAdd: () => void; onEdit: (item: Transaction) => void; onRefund: (item: Transaction) => void; onUndo: (item: Transaction) => void }) {
  const refunds = props.allTransactions.filter((item) => item.type === "refund" && item.status === "active").reduce((sum, item) => sum + item.amount_cents, 0);
  const payments = props.allTransactions.filter((item) => item.type === "payment" && item.status === "active");
  return <div className="single-page"><div className="page-title"><div><span className="eyebrow">COST CONTROL</span><h1>资金账目</h1><p>统一管理付款、订金、退款、纠正与撤销，并保留完整审计轨迹。</p></div><button className="primary-button" type="button" onClick={props.onAdd} disabled={!props.writable}><IconPlus size={19} />新增账目</button></div><div className="ledger-metrics"><MetricCard label="净支出" value={currency(props.summary?.net_amount_cents)} detail={`${props.summary?.transaction_count || 0} 笔有效流水`} icon={<IconCoinYuan />} tone="terracotta" /><MetricCard label="累计付款" value={currency(payments.reduce((sum, item) => sum + item.amount_cents, 0))} detail={`${payments.length} 笔付款`} icon={<IconReceipt2 />} tone="green" /><MetricCard label="累计退款" value={currency(refunds)} detail="退款自动冲减净支出" icon={<IconRefresh />} tone="purple" /><MetricCard label="预算余额" value={currency(props.project.budget_cents - (props.summary?.net_amount_cents || 0))} detail={`总预算 ${currency(props.project.budget_cents)}`} icon={<IconArchive />} tone="blue" /></div><SectionCard title="账目明细" className="ledger-table-card"><div className="ledger-table"><div className="ledger-header"><span>日期</span><span>分类 / 商家</span><span>阶段 / 空间</span><span>标签</span><span>金额</span><span>操作</span></div>{props.transactions.map((item) => <div className={`ledger-row ${item.status === "voided" ? "voided" : ""}`} key={item.id}><time>{fullDate(item.occurred_on)}</time><div><strong>{item.main_category || "退款"}</strong><small>{item.merchant || item.note || "—"}</small></div><div><strong>{props.stages.find((stage) => stage.id === item.context?.stage_id)?.name || "未关联"}</strong><small>{props.areas.find((area) => area.id === item.context?.area_id)?.name || "全屋"}</small></div><div className="tag-list">{item.tags.length ? item.tags.map((tag) => <span key={tag}>{tag}</span>) : <small>无标签</small>}</div><b className={item.type === "refund" ? "refund-amount" : ""}>{item.type === "refund" ? "−" : ""}{currency(item.amount_cents)}</b><div className="row-actions"><button type="button" title="编辑" onClick={() => props.onEdit(item)} disabled={!props.writable || item.type !== "payment" || item.status !== "active"}><IconEdit size={17} /></button><button type="button" title="退款" onClick={() => props.onRefund(item)} disabled={!props.writable || item.type !== "payment" || item.status !== "active"}><IconRefresh size={17} /></button><button type="button" title="撤销" onClick={() => props.onUndo(item)} disabled={!props.writable || item.status !== "active"}><IconTrash size={17} /></button></div></div>)}{!props.transactions.length && <div className="empty-inline"><IconReceipt2 size={36} /><h2>没有符合条件的账目</h2><p>新增付款，或调整顶部筛选。</p></div>}</div></SectionCard></div>;
}

function MediaPage({ items, areas, stages, writable, onUpload, onOpen }: { items: MediaAsset[]; areas: Area[]; stages: Stage[]; writable: boolean; onUpload: () => void; onOpen: (item: MediaAsset) => void }) {
  const images = items.filter((item) => item.media_type === "image").length;
  const videos = items.length - images;
  return <div className="single-page"><div className="page-title"><div><span className="eyebrow">SPATIAL ARCHIVE</span><h1>图片视频</h1><p>按时间、装修阶段和空间回看每一次施工变化。</p></div><button className="primary-button" type="button" onClick={onUpload} disabled={!writable}><IconUpload size={19} />上传媒体</button></div><div className="media-summary"><span><IconPhoto size={20} /><b>{images}</b> 张图片</span><span><IconVideo size={20} /><b>{videos}</b> 个视频</span><span><IconHome size={20} /><b>{new Set(items.map((item) => areaNameFor(item, areas))).size}</b> 个空间</span></div>{items.length ? <div className="archive-grid">{items.map((item) => <button type="button" className="archive-card" key={item.id} onClick={() => onOpen(item)}><div className="archive-image"><img src={assetUrl(item.preview_url)} alt={item.original_filename} />{item.media_type === "video" && <span className="video-pill"><IconVideo size={16} />{formatDuration(item.duration_ms)}</span>}</div><div className="archive-copy"><div><strong>{areaNameFor(item, areas)}</strong><span>{stageNameFor(item, stages)}</span></div><small>{fullDate(item.captured_at || item.uploaded_at)} · {formatBytes(item.size_bytes)}</small></div></button>)}</div> : <div className="empty-state compact"><div className="empty-icon"><IconCamera size={38} /></div><h2>还没有影像档案</h2><p>上传现场图片或视频，建立可按阶段和空间检索的施工记忆。</p></div>}</div>;
}

function StagesPage({ stages, events, writable, onCreate, onEdit }: { stages: Stage[]; events: TimelineEvent[]; writable: boolean; onCreate: () => void; onEdit: (item: Stage) => void }) {
  return <div className="single-page"><div className="page-title"><div><span className="eyebrow">PROJECT PHASES</span><h1>装修阶段</h1><p>规划施工顺序、起止时间和当前唯一进行中阶段。</p></div><button className="primary-button" type="button" onClick={onCreate} disabled={!writable}><IconPlus size={19} />新增阶段</button></div><StageRail stages={stages} /><div className="stage-board">{stages.map((item, index) => { const count = events.filter((event) => event.stage_id === item.id).length; return <article className={`stage-card ${item.status}`} key={item.id}><header><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{item.name}</strong><small>{STAGE_LABELS[item.status]}</small></div><button type="button" onClick={() => onEdit(item)} disabled={!writable}><IconEdit size={18} /></button></header><dl><div><dt>计划周期</dt><dd>{fullDate(item.planned_start)} — {fullDate(item.planned_end)}</dd></div><div><dt>实际周期</dt><dd>{fullDate(item.actual_start)} — {fullDate(item.actual_end)}</dd></div><div><dt>现场记录</dt><dd>{count} 条</dd></div></dl><div className="stage-card-footer"><i style={{ backgroundColor: item.color }} /><span>版本 {item.version}</span></div></article>; })}{!stages.length && <div className="empty-inline"><IconFlag size={38} /><h2>还没有装修阶段</h2><p>先建立设计、拆除、水电、泥木、油漆、安装等阶段。</p></div>}</div></div>;
}

function SettingsPage({ project, areas, session, writable, mediaCount, onEditProject, onAddArea, onEditArea, onRefresh }: { project: Project; areas: Area[]; session: SessionState | null; writable: boolean; mediaCount: number; onEditProject: () => void; onAddArea: () => void; onEditArea: (item: Area) => void; onRefresh: () => void }) {
  return <div className="single-page settings-page"><div className="page-title"><div><span className="eyebrow">PROJECT SETTINGS</span><h1>设置</h1><p>维护项目基础信息、空间与本地数据健康状态。</p></div><button className="secondary-button" type="button" onClick={onRefresh}><IconRefresh size={18} />刷新状态</button></div><div className="settings-grid"><SectionCard title="项目信息" action={<button className="text-button" type="button" onClick={onEditProject} disabled={!writable}><IconEdit size={16} />编辑</button>}><dl className="settings-list"><div><dt>项目名称</dt><dd>{project.name}</dd></div><div><dt>时区</dt><dd>{project.timezone}</dd></div><div><dt>总预算</dt><dd>{currency(project.budget_cents)}</dd></div><div><dt>项目状态</dt><dd><span className="status-chip active">{project.status}</span></dd></div></dl></SectionCard><SectionCard title="运行与数据状态"><dl className="settings-list"><div><dt>Writer 模式</dt><dd><span className={`status-chip ${writable ? "active" : "locked"}`}>{session?.writer_mode || "unknown"}</span></dd></div><div><dt>便携导出</dt><dd>{session?.portable_export_state || "unknown"}</dd></div><div><dt>媒体档案</dt><dd>{mediaCount} 个文件</dd></div><div><dt>页面权限</dt><dd>{writable ? "可读写" : "只读"}</dd></div></dl></SectionCard><SectionCard title="空间管理" action={<button className="text-button" type="button" onClick={onAddArea} disabled={!writable}><IconPlus size={16} />新增空间</button>} className="areas-card"><div className="area-list">{areas.map((area) => <button type="button" key={area.id} onClick={() => onEditArea(area)} disabled={!writable}><span><IconHome size={18} /><b>{area.name}</b></span><small>{area.status === "active" ? "使用中" : "已归档"}</small><IconEdit size={16} /></button>)}</div></SectionCard><SectionCard title="安全边界"><div className="safety-copy"><IconLock size={25} /><div><strong>正式环境切换保持独立授权</strong><p>页面不会自行启用 primary writer，也不会连接 Hermes、正式微信或读取真实账本。媒体原件不进入 SQLite 和便携账本 ZIP。</p></div></div></SectionCard></div></div>;
}

function EditorDialog(props: {
  editor: NonNullable<EditorState>;
  project: Project;
  stages: Stage[];
  areas: Area[];
  saving: boolean;
  onClose: () => void;
  onPayment: (body: unknown, item?: Transaction) => void;
  onRefund: (body: unknown) => void;
  onUndo: (item: Transaction, reason: string) => void;
  onStage: (body: unknown, item?: Stage) => void;
  onEvent: (body: unknown, item?: TimelineEvent) => void;
  onProject: (body: unknown, item?: Project) => void;
  onArea: (body: unknown, item?: Area) => void;
  onUpload: (files: File[], metadata: { project_id: string; captured_at?: string | null; links: Array<{ target_type: "stage" | "area"; target_id: string }> }, onProgress: (value: number) => void) => Promise<void>;
}) {
  const { editor } = props;
  if (editor.kind === "payment") return <PaymentDialog item={editor.item} project={props.project} stages={props.stages} areas={props.areas} saving={props.saving} onClose={props.onClose} onSubmit={props.onPayment} />;
  if (editor.kind === "refund") return <RefundDialog item={editor.item} project={props.project} saving={props.saving} onClose={props.onClose} onSubmit={props.onRefund} />;
  if (editor.kind === "undo") return <UndoDialog item={editor.item} saving={props.saving} onClose={props.onClose} onSubmit={props.onUndo} />;
  if (editor.kind === "stage") return <StageDialog item={editor.item} project={props.project} saving={props.saving} onClose={props.onClose} onSubmit={props.onStage} />;
  if (editor.kind === "event") return <EventDialog item={editor.item} project={props.project} stages={props.stages} areas={props.areas} saving={props.saving} onClose={props.onClose} onSubmit={props.onEvent} />;
  if (editor.kind === "project") return <ProjectDialog item={editor.item} saving={props.saving} onClose={props.onClose} onSubmit={props.onProject} />;
  if (editor.kind === "area") return <AreaDialog item={editor.item} project={props.project} saving={props.saving} onClose={props.onClose} onSubmit={props.onArea} />;
  return <UploadDialog project={props.project} stages={props.stages} areas={props.areas} saving={props.saving} onClose={props.onClose} onSubmit={props.onUpload} />;
}

function Dialog({ title, subtitle, onClose, children }: { title: string; subtitle: string; onClose: () => void; children: ReactNode }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => closeRef.current?.focus(), []);
  return <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title"><header><div><h2 id="dialog-title">{title}</h2><p>{subtitle}</p></div><button ref={closeRef} className="icon-button" type="button" onClick={onClose} aria-label="关闭"><IconX size={20} /></button></header>{children}</section></div>;
}

function Field({ label, children, wide = false }: { label: string; children: ReactNode; wide?: boolean }) {
  return <label className={wide ? "field wide" : "field"}><span>{label}</span>{children}</label>;
}

function DialogActions({ saving, onClose, submitLabel = "保存" }: { saving: boolean; onClose: () => void; submitLabel?: string }) {
  return <div className="dialog-actions"><button className="secondary-button" type="button" onClick={onClose}>取消</button><button className="primary-button" type="submit" disabled={saving}>{saving ? "正在保存..." : submitLabel}</button></div>;
}

function PaymentDialog({ item, project, stages, areas, saving, onClose, onSubmit }: { item?: Transaction; project: Project; stages: Stage[]; areas: Area[]; saving: boolean; onClose: () => void; onSubmit: (body: unknown, item?: Transaction) => void }) {
  const [amount, setAmount] = useState(item ? String(item.amount_cents / 100) : "");
  const [occurredOn, setOccurredOn] = useState(item?.occurred_on || new Date().toISOString().slice(0, 10));
  const [category, setCategory] = useState(item?.main_category || "水电工程");
  const [merchant, setMerchant] = useState(item?.merchant || "");
  const [note, setNote] = useState(item?.note || "");
  const [tags, setTags] = useState(item?.tags.join("，") || "");
  const [deposit, setDeposit] = useState(item?.is_deposit || false);
  const [stageId, setStageId] = useState(item?.context?.stage_id || stages.find((stage) => stage.status === "active")?.id || "");
  const [areaId, setAreaId] = useState(item?.context?.area_id || "");
  const submit = (event: FormEvent) => { event.preventDefault(); const common = { amount_cents: Math.round(Number(amount) * 100), occurred_on: occurredOn, main_category: category, merchant, note, is_deposit: deposit, tags: tags.split(/[，,]/).map((value) => value.trim()).filter(Boolean) }; onSubmit(item ? { version: item.version, changes: common, reason: "页面编辑账目" } : { ...common, project_id: project.id, stage_id: stageId || null, area_id: areaId || null }, item); };
  return <Dialog title={item ? "编辑账目" : "新增账目"} subtitle="金额、分类与标签会进入统一账本和审计记录。" onClose={onClose}><form onSubmit={submit}><div className="form-grid"><Field label="金额（元）"><input type="number" min="0.01" step="0.01" required value={amount} onChange={(event) => setAmount(event.target.value)} /></Field><Field label="发生日期"><input type="date" required value={occurredOn} onChange={(event) => setOccurredOn(event.target.value)} /></Field><Field label="主分类"><input required value={category} onChange={(event) => setCategory(event.target.value)} /></Field><Field label="商家 / 收款方"><input value={merchant} onChange={(event) => setMerchant(event.target.value)} /></Field>{!item && <><Field label="装修阶段"><select value={stageId} onChange={(event) => setStageId(event.target.value)}><option value="">不关联</option>{stages.map((stage) => <option key={stage.id} value={stage.id}>{stage.name}</option>)}</select></Field><Field label="空间"><select value={areaId} onChange={(event) => setAreaId(event.target.value)}><option value="">全屋 / 不关联</option>{areas.map((area) => <option key={area.id} value={area.id}>{area.name}</option>)}</select></Field></>}<Field label="标签（逗号分隔）" wide><input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="材料，人工，水电" /></Field><Field label="备注" wide><textarea value={note} onChange={(event) => setNote(event.target.value)} rows={3} /></Field><label className="check-field wide"><input type="checkbox" checked={deposit} onChange={(event) => setDeposit(event.target.checked)} /><span>这是一笔订金</span></label></div><DialogActions saving={saving} onClose={onClose} submitLabel={item ? "保存修改" : "记录账目"} /></form></Dialog>;
}

function RefundDialog({ item, project, saving, onClose, onSubmit }: { item: Transaction; project: Project; saving: boolean; onClose: () => void; onSubmit: (body: unknown) => void }) {
  const [amount, setAmount] = useState(""); const [date, setDate] = useState(new Date().toISOString().slice(0, 10)); const [note, setNote] = useState("");
  return <Dialog title="记录退款" subtitle={`原付款：${item.main_category} · ${currency(item.amount_cents)}`} onClose={onClose}><form onSubmit={(event) => { event.preventDefault(); onSubmit({ original_payment_id: item.id, amount_cents: Math.round(Number(amount) * 100), occurred_on: date, note, project_id: project.id, stage_id: item.context?.stage_id || null, area_id: item.context?.area_id || null }); }}><div className="form-grid"><Field label="退款金额（元）"><input type="number" min="0.01" max={item.amount_cents / 100} step="0.01" required value={amount} onChange={(event) => setAmount(event.target.value)} /></Field><Field label="退款日期"><input type="date" required value={date} onChange={(event) => setDate(event.target.value)} /></Field><Field label="退款说明" wide><textarea rows={3} required value={note} onChange={(event) => setNote(event.target.value)} /></Field></div><DialogActions saving={saving} onClose={onClose} submitLabel="确认退款" /></form></Dialog>;
}

function UndoDialog({ item, saving, onClose, onSubmit }: { item: Transaction; saving: boolean; onClose: () => void; onSubmit: (item: Transaction, reason: string) => void }) {
  const [reason, setReason] = useState("");
  return <Dialog title="撤销账目" subtitle="撤销会保留原流水和审计记录，不会物理删除。" onClose={onClose}><form onSubmit={(event) => { event.preventDefault(); onSubmit(item, reason); }}><div className="warning-box"><IconTrash size={22} /><span>即将撤销 {item.main_category || "退款"} · {currency(item.amount_cents)}</span></div><Field label="撤销原因" wide><textarea required rows={3} value={reason} onChange={(event) => setReason(event.target.value)} /></Field><DialogActions saving={saving} onClose={onClose} submitLabel="确认撤销" /></form></Dialog>;
}

function StageDialog({ item, project, saving, onClose, onSubmit }: { item?: Stage; project: Project; saving: boolean; onClose: () => void; onSubmit: (body: unknown, item?: Stage) => void }) {
  const [name, setName] = useState(item?.name || ""); const [status, setStatus] = useState<Stage["status"]>(item?.status || "planned"); const [color, setColor] = useState(item?.color || "#5f8f55"); const [start, setStart] = useState(item?.planned_start || ""); const [end, setEnd] = useState(item?.planned_end || ""); const [actualStart, setActualStart] = useState(item?.actual_start || ""); const [actualEnd, setActualEnd] = useState(item?.actual_end || "");
  return <Dialog title={item ? "编辑装修阶段" : "新增装修阶段"} subtitle="同一项目最多只有一个进行中阶段。" onClose={onClose}><form onSubmit={(event) => { event.preventDefault(); const changes = { name, status, color, planned_start: start || null, planned_end: end || null, actual_start: actualStart || null, actual_end: actualEnd || null }; onSubmit(item ? { version: item.version, changes } : { project_id: project.id, ...changes, position: 100 }, item); }}><div className="form-grid"><Field label="阶段名称"><input required value={name} onChange={(event) => setName(event.target.value)} /></Field><Field label="状态"><select value={status} onChange={(event) => setStatus(event.target.value as Stage["status"])}><option value="planned">待开始</option><option value="active">进行中</option><option value="completed">已完成</option><option value="archived">已归档</option></select></Field><Field label="标识色"><input className="color-input" type="color" value={color} onChange={(event) => setColor(event.target.value)} /></Field><Field label="计划开始"><input type="date" value={start} onChange={(event) => setStart(event.target.value)} /></Field><Field label="计划结束"><input type="date" value={end} onChange={(event) => setEnd(event.target.value)} /></Field><Field label="实际开始"><input type="date" value={actualStart} onChange={(event) => setActualStart(event.target.value)} /></Field><Field label="实际结束"><input type="date" value={actualEnd} onChange={(event) => setActualEnd(event.target.value)} /></Field></div><DialogActions saving={saving} onClose={onClose} submitLabel={item ? "保存阶段" : "创建阶段"} /></form></Dialog>;
}

function EventDialog({ item, project, stages, areas, saving, onClose, onSubmit }: { item?: TimelineEvent; project: Project; stages: Stage[]; areas: Area[]; saving: boolean; onClose: () => void; onSubmit: (body: unknown, item?: TimelineEvent) => void }) {
  const [title, setTitle] = useState(item?.title || ""); const [description, setDescription] = useState(item?.description || ""); const [type, setType] = useState<TimelineEvent["event_type"]>(item?.event_type || "progress"); const [occurredAt, setOccurredAt] = useState(localDateTime(item?.occurred_at)); const [stageId, setStageId] = useState(item?.stage_id || stages.find((stage) => stage.status === "active")?.id || ""); const [areaId, setAreaId] = useState(item?.area_id || "");
  return <Dialog title={item ? "编辑现场记录" : "新增现场记录"} subtitle="时间线会按发生时间、阶段和空间自动组织。" onClose={onClose}><form onSubmit={(event) => { event.preventDefault(); const changes = { title, description, event_type: type, occurred_at: new Date(occurredAt).toISOString(), stage_id: stageId || null, area_id: areaId || null }; onSubmit(item ? { version: item.version, changes } : { project_id: project.id, ...changes }, item); }}><div className="form-grid"><Field label="记录标题" wide><input required value={title} onChange={(event) => setTitle(event.target.value)} /></Field><Field label="记录类型"><select value={type} onChange={(event) => setType(event.target.value as TimelineEvent["event_type"])}>{Object.entries(EVENT_META).map(([key, meta]) => <option key={key} value={key}>{meta.label}</option>)}</select></Field><Field label="发生时间"><input type="datetime-local" required value={occurredAt} onChange={(event) => setOccurredAt(event.target.value)} /></Field><Field label="装修阶段"><select value={stageId} onChange={(event) => setStageId(event.target.value)}><option value="">不关联</option>{stages.map((stage) => <option key={stage.id} value={stage.id}>{stage.name}</option>)}</select></Field><Field label="空间"><select value={areaId} onChange={(event) => setAreaId(event.target.value)}><option value="">全屋</option>{areas.map((area) => <option key={area.id} value={area.id}>{area.name}</option>)}</select></Field><Field label="详细说明" wide><textarea rows={4} value={description} onChange={(event) => setDescription(event.target.value)} /></Field></div><DialogActions saving={saving} onClose={onClose} submitLabel={item ? "保存记录" : "创建记录"} /></form></Dialog>;
}

function ProjectDialog({ item, saving, onClose, onSubmit }: { item?: Project; saving: boolean; onClose: () => void; onSubmit: (body: unknown, item?: Project) => void }) {
  const [name, setName] = useState(item?.name || ""); const [budget, setBudget] = useState(item ? String(item.budget_cents / 100) : ""); const [timezone, setTimezone] = useState(item?.timezone || "Asia/Shanghai"); const [status, setStatus] = useState<Project["status"]>(item?.status || "active");
  return <Dialog title={item ? "编辑项目信息" : "创建装修项目"} subtitle="项目是阶段、账目、时间线和媒体档案的边界。" onClose={onClose}><form onSubmit={(event) => { event.preventDefault(); const changes = { name, budget_cents: Math.round(Number(budget) * 100), timezone, status }; onSubmit(item ? { version: item.version, changes } : changes, item); }}><div className="form-grid"><Field label="项目名称" wide><input required value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：新家 8-2-501" /></Field><Field label="装修预算（元）"><input type="number" min="0" step="0.01" required value={budget} onChange={(event) => setBudget(event.target.value)} /></Field><Field label="时区"><input required value={timezone} onChange={(event) => setTimezone(event.target.value)} /></Field><Field label="项目状态"><select value={status} onChange={(event) => setStatus(event.target.value as Project["status"])}><option value="active">装修中</option><option value="completed">已完成</option><option value="archived">已归档</option></select></Field></div><DialogActions saving={saving} onClose={onClose} submitLabel={item ? "保存项目" : "创建项目"} /></form></Dialog>;
}

function AreaDialog({ item, project, saving, onClose, onSubmit }: { item?: Area; project: Project; saving: boolean; onClose: () => void; onSubmit: (body: unknown, item?: Area) => void }) {
  const [name, setName] = useState(item?.name || ""); const [status, setStatus] = useState<Area["status"]>(item?.status || "active");
  return <Dialog title={item ? "编辑空间" : "新增空间"} subtitle="空间用于组织现场记录与图片视频。" onClose={onClose}><form onSubmit={(event) => { event.preventDefault(); const changes = { name, status }; onSubmit(item ? { version: item.version, changes } : { project_id: project.id, ...changes, position: 100 }, item); }}><div className="form-grid"><Field label="空间名称"><input required value={name} onChange={(event) => setName(event.target.value)} placeholder="厨房、客厅、主卧..." /></Field><Field label="状态"><select value={status} onChange={(event) => setStatus(event.target.value as Area["status"])}><option value="active">使用中</option><option value="archived">已归档</option></select></Field></div><DialogActions saving={saving} onClose={onClose} submitLabel={item ? "保存空间" : "创建空间"} /></form></Dialog>;
}

function UploadDialog({ project, stages, areas, saving, onClose, onSubmit }: { project: Project; stages: Stage[]; areas: Area[]; saving: boolean; onClose: () => void; onSubmit: (files: File[], metadata: { project_id: string; captured_at?: string | null; links: Array<{ target_type: "stage" | "area"; target_id: string }> }, onProgress: (value: number) => void) => Promise<void> }) {
  const [files, setFiles] = useState<File[]>([]); const [stageId, setStageId] = useState(stages.find((stage) => stage.status === "active")?.id || ""); const [areaId, setAreaId] = useState(""); const [capturedAt, setCapturedAt] = useState(localDateTime()); const [progress, setProgress] = useState(0);
  const submit = async (event: FormEvent) => { event.preventDefault(); const links: Array<{ target_type: "stage" | "area"; target_id: string }> = []; if (stageId) links.push({ target_type: "stage", target_id: stageId }); if (areaId) links.push({ target_type: "area", target_id: areaId }); await onSubmit(files, { project_id: project.id, captured_at: new Date(capturedAt).toISOString(), links }, setProgress); };
  return <Dialog title="上传图片视频" subtitle="支持 JPEG、PNG、WebP、HEIC、MP4、MOV 和 WebM；原件流式写入媒体目录。" onClose={onClose}><form onSubmit={(event) => void submit(event)}><label className="drop-zone"><IconUpload size={32} /><strong>{files.length ? `已选择 ${files.length} 个文件` : "选择图片或视频"}</strong><span>{files.length ? files.map((file) => file.name).join("、") : "可一次选择多个现场文件"}</span><input type="file" multiple accept="image/jpeg,image/png,image/webp,image/heic,image/heif,video/mp4,video/quicktime,video/webm" onChange={(event) => setFiles(Array.from(event.target.files || []))} /></label><div className="form-grid"><Field label="装修阶段"><select value={stageId} onChange={(event) => setStageId(event.target.value)}><option value="">不关联</option>{stages.map((stage) => <option key={stage.id} value={stage.id}>{stage.name}</option>)}</select></Field><Field label="空间"><select value={areaId} onChange={(event) => setAreaId(event.target.value)}><option value="">全屋</option>{areas.map((area) => <option key={area.id} value={area.id}>{area.name}</option>)}</select></Field><Field label="拍摄时间" wide><input type="datetime-local" value={capturedAt} onChange={(event) => setCapturedAt(event.target.value)} /></Field></div>{saving && <div className="upload-progress"><div><i style={{ width: `${progress}%` }} /></div><span>{progress}% · 正在校验并归档</span></div>}<div className="dialog-actions"><button className="secondary-button" type="button" onClick={onClose}>取消</button><button className="primary-button" type="submit" disabled={saving || files.length === 0}>{saving ? "正在上传..." : "开始上传"}</button></div></form></Dialog>;
}

function MediaViewer({ item, area, stage, onClose }: { item: MediaAsset; area: string; stage: string; onClose: () => void }) {
  return <div className="media-viewer" role="dialog" aria-modal="true"><button className="viewer-close" type="button" onClick={onClose} aria-label="关闭"><IconX size={24} /></button><div className="viewer-stage">{item.media_type === "video" ? <video src={assetUrl(item.content_url)} controls autoPlay poster={assetUrl(item.preview_url)} /> : <img src={assetUrl(item.content_url)} alt={item.original_filename} />}</div><aside><span className="eyebrow">MEDIA DETAIL</span><h2>{item.original_filename}</h2><dl><div><dt>空间</dt><dd>{area}</dd></div><div><dt>阶段</dt><dd>{stage}</dd></div><div><dt>拍摄时间</dt><dd>{fullDate(item.captured_at || item.uploaded_at)}</dd></div><div><dt>文件大小</dt><dd>{formatBytes(item.size_bytes)}</dd></div><div><dt>尺寸 / 时长</dt><dd>{item.media_type === "video" ? formatDuration(item.duration_ms) : `${item.width || "—"} × ${item.height || "—"}`}</dd></div><div><dt>处理状态</dt><dd><span className="status-chip active">{item.processing_status}</span></dd></div></dl></aside></div>;
}

function areaNameFor(item: MediaAsset, areas: Area[]): string {
  const link = item.links.find((candidate) => candidate.target_type === "area");
  return areas.find((area) => area.id === link?.target_id)?.name || "全屋";
}

function stageNameFor(item: MediaAsset, stages: Stage[]): string {
  const link = item.links.find((candidate) => candidate.target_type === "stage");
  return stages.find((stage) => stage.id === link?.target_id)?.name || "未关联阶段";
}

function stageMediaCount(items: MediaAsset[], stageId?: string): number {
  if (!stageId) return 0;
  return items.filter((item) => item.links.some((link) => link.target_type === "stage" && link.target_id === stageId)).length;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function formatDuration(durationMs: number | null): string {
  if (!durationMs) return "视频";
  const seconds = Math.round(durationMs / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
