import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API_UNAUTHORIZED_EVENT, ApiError, api, clearAuth, normalizePage, saveAuth } from "./api";
import type { Account, AccountTestResult, AuthResponse, Content, ContentDetail, CrawlRun, PageResponse, Platform, PlatformSession, StorageInfo, Summary } from "./types";
import { formatBytes, formatDate, mediaUrl, platformNames } from "./utils";

type View = "overview" | "feed" | "accounts" | "sessions" | "runs";
type FeedFilters = { platform: string; account: string; query: string };
const platforms: Platform[] = ["bilibili", "weibo", "douyin", "xiaohongshu"];
const appTitle = "我会一直看着你";
const emptyFeedFilters: FeedFilters = { platform: "", account: "", query: "" };
const CONTENT_PAGE_SIZE = 24;
const RUN_PAGE_SIZE = 50;

type IconName = "overview" | "archive" | "accounts" | "sessions" | "runs" | "arrow" | "plus" | "close";

const statusNames: Record<string, string> = {
  pending: "待初始化",
  healthy: "运行正常",
  polling: "正在采集",
  error: "异常",
  blocked: "访问受阻",
  paused: "已暂停",
  complete: "已完成",
  failed: "失败",
  running: "执行中",
  baseline: "建立基线",
  logged_out: "未登录",
  starting: "启动中",
  qr_ready: "等待扫码",
  authenticated: "已登录",
  expired: "已过期",
  manual_verification_required: "需要验证"
};

function statusName(status: string): string { return statusNames[status] || status; }
function contentTypeName(type: string): string { return type === "video" ? "视频" : type === "image" ? "图集" : type === "audio" ? "音频" : type === "text" ? "文章" : "其他"; }

function feedParams(filters: FeedFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.platform) params.set("platform", filters.platform);
  if (filters.account) params.set("account_id", filters.account);
  if (filters.query) params.set("q", filters.query);
  return params;
}

function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const common = { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true };
  if (name === "overview") return <svg {...common}><rect x="3" y="3" width="7" height="7" rx="2" /><rect x="14" y="3" width="7" height="7" rx="2" /><rect x="3" y="14" width="7" height="7" rx="2" /><rect x="14" y="14" width="7" height="7" rx="2" /></svg>;
  if (name === "archive") return <svg {...common}><path d="M4 7.5h16v12.25A1.25 1.25 0 0 1 18.75 21H5.25A1.25 1.25 0 0 1 4 19.75Z" /><path d="M3 3h18v4.5H3zM9 12h6" /></svg>;
  if (name === "accounts") return <svg {...common}><circle cx="9" cy="8" r="3.25" /><path d="M3.5 20v-1.5A5.5 5.5 0 0 1 9 13h0a5.5 5.5 0 0 1 5.5 5.5V20M16 7.5h5M18.5 5v5" /></svg>;
  if (name === "sessions") return <svg {...common}><rect x="4" y="3" width="16" height="18" rx="3" /><path d="M8 8h8M8 12h5M9 17h6" /></svg>;
  if (name === "runs") return <svg {...common}><path d="M20 11a8 8 0 1 0-2.34 5.66L20 14.3" /><path d="M20 7v4h-4M12 7v5l3 2" /></svg>;
  if (name === "arrow") return <svg {...common}><path d="m9 18 6-6-6-6" /></svg>;
  if (name === "plus") return <svg {...common}><path d="M12 5v14M5 12h14" /></svg>;
  return <svg {...common}><path d="m6 6 12 12M18 6 6 18" /></svg>;
}

function BrandMark({ compact = false }: { compact?: boolean }) {
  return <span className={`brand-symbol${compact ? " compact" : ""}`} aria-hidden="true"><svg viewBox="0 0 32 32" fill="none"><path d="M3.5 16s4.2-7 12.5-7 12.5 7 12.5 7-4.2 7-12.5 7S3.5 16 3.5 16Z" stroke="currentColor" strokeWidth="2" /><circle cx="16" cy="16" r="4.25" fill="currentColor" /></svg></span>;
}

function useMobileLayout() {
  const query = "(max-width: 820px)";
  const [mobile, setMobile] = useState(() => typeof window !== "undefined" && (typeof window.matchMedia === "function" ? window.matchMedia(query).matches : window.innerWidth <= 820));
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(query);
    const update = () => setMobile(media.matches);
    update(); media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return mobile;
}

function ErrorBanner({ message, onClose }: { message: string; onClose: () => void }) {
  return <div className="error-banner" role="alert"><span className="error-icon" aria-hidden="true">!</span><span>{message}</span><button type="button" onClick={onClose} aria-label="关闭错误"><Icon name="close" size={17} /></button></div>;
}

function ConnectionError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <main className="connection-shell"><section className="connection-card" role="alert"><BrandMark /><p className="eyebrow">连接失败</p><h1>暂时无法连接归档服务</h1><p>{message}</p><button type="button" className="primary" onClick={onRetry}>重新连接</button><small>请确认服务已启动，稍后再试。</small></section></main>;
}

function Login({ onLogin }: { onLogin: (auth: AuthResponse) => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const auth = await api<AuthResponse>("/auth/login", { method: "POST", body: JSON.stringify({ token }) });
      saveAuth(auth); onLogin(auth);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "登录失败"); }
    finally { setBusy(false); }
  }
  return <main className="login-shell">
    <section className="login-story" aria-label="产品介绍">
      <div className="login-brand"><BrandMark /><div><strong>{appTitle}</strong><span>PUBLIC CONTENT ARCHIVE</span></div></div>
      <p className="eyebrow">你的私人公开内容档案馆</p>
      <h1>把散落的更新，<br />收进自己的时间里。</h1>
      <p>每小时轻量检查公开页面。文案、图片与视频完整落盘，由你永久保存。</p>
      <div className="platform-row">{platforms.map(item => <span key={item}>{platformNames[item]}</span>)}</div>
      <div className="archive-principles" aria-label="产品特点"><span><strong>公开页面</strong>只读取你指定的创作者主页</span><span><strong>本地存储</strong>归档文件完整保存在自己的设备</span><span><strong>私有访问</strong>媒体始终通过认证接口读取</span></div>
    </section>
    <section className="login-panel">
      <form onSubmit={submit} className="login-form">
        <div className="login-form-head"><span className="form-kicker">管理员控制台</span><h2>欢迎回来</h2><p className="muted">输入配置的 Token；未配置时请从 <code>data/state/webui-login-token.txt</code> 获取启动时生成的 Token。</p></div>
        {error && <ErrorBanner message={error} onClose={() => setError("")} />}
        <label>WebUI 登录 Token<input name="token" type="password" autoComplete="current-password" autoCapitalize="none" spellCheck={false} value={token} onChange={event => setToken(event.target.value)} required autoFocus /></label>
        <button className="primary wide" disabled={busy}>{busy ? "正在验证…" : "登录控制台"}</button>
        <p className="login-note"><span aria-hidden="true">●</span> Token 通过服务配置更新 · 修改后需重启服务</p>
      </form>
    </section>
  </main>;
}

function StatCard({ label, value, note, icon, tone = "default", loading = false }: { label: string; value: string | number; note: string; icon: IconName; tone?: string; loading?: boolean }) {
  return <article className={`stat-card ${tone}`}><div className="stat-card-head"><span>{label}</span><span className="stat-card-icon"><Icon name={icon} size={17} /></span></div>{loading ? <span className="skeleton stat-skeleton" /> : <strong>{value}</strong>}<p>{note}</p></article>;
}

function Overview({ summary, storage, accounts, loading }: { summary: Summary | null; storage: StorageInfo | null; accounts: Account[]; loading: boolean }) {
  const recent = accounts.slice(0, 4);
  const usedRatio = storage?.total_bytes ? Math.min(100, storage.used_bytes / storage.total_bytes * 100) : 0;
  return <div className="view-stack">
    <header className="page-heading"><div><p className="eyebrow">工作台</p><h1>归档运行概览</h1></div><p>掌握公开内容的采集、归档与存储状态。系统会按账号设定的周期自动检查更新。</p></header>
    <section className="stats-grid">
      <StatCard label="监控账号" value={summary?.accounts ?? "—"} note={`${summary?.healthy_accounts ?? 0} 个运行正常`} icon="accounts" loading={loading} />
      <StatCard label="永久归档" value={summary?.contents ?? "—"} note="条公开原创内容" icon="archive" loading={loading} />
      <StatCard label="归档体积" value={storage ? formatBytes(storage.archive_bytes) : "—"} note={`磁盘剩余 ${storage ? formatBytes(storage.free_bytes) : "—"}`} icon="overview" loading={loading} />
      <StatCard label="失败任务" value={summary?.failed_runs ?? "—"} note="可在任务记录中诊断" icon="runs" tone={(summary?.failed_runs ?? 0) > 0 ? "danger" : "default"} loading={loading} />
    </section>
    {storage?.downloads_paused && <div className="warning" role="alert">磁盘剩余空间低于安全阈值，新增媒体下载已暂停。</div>}
    <section className="split-grid">
      <article className="panel"><div className="panel-title"><div><p className="eyebrow">账号状态</p><h2>最近监控</h2></div><span className="panel-count">{accounts.length} 个账号</span></div>
        <div className="compact-list">{loading && !recent.length ? <LoadingRows /> : recent.length ? recent.map(account => <div className="compact-row" key={account.id}><span className={`platform-dot ${account.platform}`} /> <div><strong>{account.display_name}</strong><small>{platformNames[account.platform]} · {formatDate(account.last_polled_at)}</small></div><span className={`status ${account.status}`}>{statusName(account.status)}</span></div>) : <Empty text="还没有添加监控账号" compact />}</div>
      </article>
      <article className="panel storage-panel"><div className="panel-title"><div><p className="eyebrow">存储空间</p><h2>本地归档</h2></div><span className="storage-percentage">{storage ? `${usedRatio.toFixed(0)}%` : "—"}</span></div>
        <div className="disk-visual" role="progressbar" aria-label="磁盘已使用比例" aria-valuemin={0} aria-valuemax={100} aria-valuenow={storage ? Math.round(usedRatio) : undefined}><span style={{ width: `${usedRatio}%` }} /></div>
        <div className="disk-labels"><strong>{storage ? formatBytes(storage.used_bytes) : "—"} 已使用</strong><span>{storage ? formatBytes(storage.total_bytes) : "—"} 总容量</span></div>
        <p className="muted">归档文件是最终数据源；SQLite 索引可随时从 metadata.json 重建。</p>
      </article>
    </section>
  </div>;
}

function LoadingRows() { return <div className="loading-rows" role="status" aria-label="正在加载"><span /><span /><span /></div>; }

function Empty({ text, compact = false }: { text: string; compact?: boolean }) { return <div className={`empty${compact ? " compact" : ""}`}><span className="empty-icon" aria-hidden="true"><Icon name="archive" size={22} /></span><p>{text}</p></div>; }

function Pagination({ total, offset, limit, hasMore, loading, onPage, label }: { total: number; offset: number; limit: number; hasMore: boolean; loading: boolean; onPage: (offset: number) => void; label: string }) {
  if (total <= limit && offset === 0 && !hasMore) return null;
  const start = total ? offset + 1 : 0;
  const end = Math.min(total, offset + limit);
  return <nav className="pagination" aria-label={`${label}分页`}>
    <span>第 {start}–{end} 条，共 {total} 条</span>
    <div><button type="button" className="ghost" disabled={loading || offset === 0} onClick={() => onPage(Math.max(0, offset - limit))}>上一页</button><button type="button" className="ghost" disabled={loading || !hasMore} onClick={() => onPage(offset + limit)}>下一页</button></div>
  </nav>;
}

function Feed({ contents, accounts, filters, setFilters, onOpen, onRefresh, onPage, page, loading }: { contents: Content[]; accounts: Account[]; filters: FeedFilters; setFilters: (value: FeedFilters) => void; onOpen: (id: number) => void; onRefresh: (params: URLSearchParams) => void; onPage: (offset: number) => void; page: PageResponse<Content>; loading: boolean }) {
  const { platform, account, query } = filters;
  function filter(event: FormEvent) { event.preventDefault(); onRefresh(feedParams(filters)); }
  return <div className="view-stack"><header className="page-heading"><div><p className="eyebrow">内容归档</p><h1>统一信息流</h1></div><p>跨平台查看已归档内容。所有媒体均由认证接口读取，不会作为公开目录暴露。</p></header>
    <form className="filters" onSubmit={filter}><label className="search-field"><span>搜索归档</span><input value={query} onChange={e => setFilters({ ...filters, query: e.target.value })} placeholder="标题、作者或文案" /></label><label><span>平台</span><select value={platform} onChange={e => setFilters({ ...filters, platform: e.target.value })}><option value="">全部平台</option>{platforms.map(item => <option key={item} value={item}>{platformNames[item]}</option>)}</select></label><label><span>账号</span><select value={account} onChange={e => setFilters({ ...filters, account: e.target.value })}><option value="">全部账号</option>{accounts.map(item => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label><button className="secondary">筛选结果</button></form>
    {!loading && contents.length > 0 && <div className="result-summary"><span>共 {page.total} 条归档</span><span>按发布时间展示</span></div>}
    {loading ? <div className="feed-grid skeleton-grid" role="status" aria-label="正在读取归档">{[0, 1, 2].map(item => <span className="content-skeleton" key={item} />)}</div> : contents.length ? <section className="feed-grid">{contents.map(content => <article key={content.id} className="content-card"><div className={`content-cover ${content.platform}`}><span className="platform-chip">{platformNames[content.platform]}</span><strong>{contentTypeName(content.content_type)}</strong><i aria-hidden="true" /></div><div className="content-body"><small>{content.author || "未知作者"} · {formatDate(content.published_at)}</small><h2>{content.title}</h2><p>{content.summary || "暂无文案"}</p><div className="content-footer"><span className={content.integrity_status && content.integrity_status !== "complete" ? "integrity-warning" : undefined}>{content.integrity_status && content.integrity_status !== "complete" ? "完整性待核验" : `${content.media_count} 个媒体文件`}</span><span>查看详情 <Icon name="arrow" size={14} /></span></div></div><button className="card-hit-area" onClick={() => onOpen(content.id)} aria-label={`查看归档：${content.title}`} /></article>)}</section> : <Empty text="暂无归档内容；添加账号后会保存最近一条历史内容，后续持续归档新增内容" />}
    <Pagination total={page.total} offset={page.offset} limit={page.limit} hasMore={page.has_more} loading={loading} onPage={onPage} label="内容归档" />
  </div>;
}

function AccountsList({ accounts, loading, loadError, reload, setError }: { accounts: Account[]; loading: boolean; loadError: string; reload: () => Promise<void>; setError: (value: string) => void }) {
  const [showForm, setShowForm] = useState(false); const [busy, setBusy] = useState<number | string>("");
  const [testFeedback, setTestFeedback] = useState<Record<number, { tone: "success" | "error"; message: string }>>({});
  const [form, setForm] = useState({ platform: "bilibili" as Platform, display_name: "", source_url: "", interval_minutes: 60 });
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState({ display_name: "", source_url: "", interval_minutes: "60" });
  const [editOriginal, setEditOriginal] = useState({ display_name: "", interval_minutes: 60 });
  const [editError, setEditError] = useState("");
  const [deleteConfirmId, setDeleteConfirmId] = useState<number | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const [notice, setNotice] = useState("");
  const editButtonRefs = useRef<Record<number, HTMLButtonElement | null>>({});
  const deleteButtonRefs = useRef<Record<number, HTMLButtonElement | null>>({});
  const noticeRef = useRef<HTMLDivElement | null>(null);
  async function add(event: FormEvent) {
    event.preventDefault(); setBusy("add"); setError("");
    try {
      const created = await api<Account>("/accounts", { method: "POST", body: JSON.stringify(form) });
      let initializationError = "";
      try {
        const run = await api<CrawlRun>(`/accounts/${created.id}/poll`, { method: "POST" });
        if (run.status === "failed") {
          initializationError = `账号已保存，但最近一条内容归档失败：${run.error || "请稍后重试"}`;
        }
      } catch (caught) {
        if (!(caught instanceof ApiError && caught.status === 409)) {
          initializationError = `账号已保存，但首次归档未完成：${caught instanceof Error ? caught.message : "请稍后重试"}`;
        }
      }
      setForm(current => ({ ...current, display_name: "", source_url: "" }));
      setShowForm(false);
      await reload();
      if (initializationError) setError(initializationError);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) await reload();
      setError(caught instanceof Error ? caught.message : "添加失败");
    } finally { setBusy(""); }
  }
  async function testAccount(id: number) {
    const key = `${id}/test`; setBusy(key); setError("");
    setTestFeedback(current => { const next = { ...current }; delete next[id]; return next; });
    try {
      const result = await api<AccountTestResult>(`/accounts/${id}/test`, { method: "POST" });
      const message = result.found > 0 ? `测试成功，获取到 ${result.found} 条最新发布内容` : "连接成功，当前未发现公开作品";
      setTestFeedback(current => ({ ...current, [id]: { tone: "success", message } }));
      await reload();
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "测试失败";
      setTestFeedback(current => ({ ...current, [id]: { tone: "error", message } }));
    } finally { setBusy(""); }
  }
  function startEdit(account: Account) {
    setEditingId(account.id);
    setEditForm({ display_name: account.display_name, source_url: account.source_url, interval_minutes: String(account.interval_minutes) });
    setEditOriginal({ display_name: account.display_name, interval_minutes: account.interval_minutes });
    setEditError(""); setDeleteConfirmId(null); setDeleteError(""); setNotice(""); setError("");
  }
  function cancelEdit(id: number) {
    setEditingId(null); setEditError("");
    window.setTimeout(() => editButtonRefs.current[id]?.focus(), 0);
  }
  async function saveEdit(event: FormEvent, account: Account) {
    event.preventDefault();
    const displayName = editForm.display_name.trim();
    const intervalMinutes = Number(editForm.interval_minutes);
    if (!displayName) { setEditError("显示名称不能为空"); return; }
    if (!Number.isInteger(intervalMinutes) || intervalMinutes < 5 || intervalMinutes > 1440) { setEditError("轮询间隔必须是 5 到 1440 分钟之间的整数"); return; }
    const payload: { display_name?: string; interval_minutes?: number } = {};
    if (displayName !== editOriginal.display_name) payload.display_name = displayName;
    if (intervalMinutes !== editOriginal.interval_minutes) payload.interval_minutes = intervalMinutes;
    if (!Object.keys(payload).length) {
      setEditingId(null); setEditError(""); setNotice("没有需要保存的修改");
      window.setTimeout(() => noticeRef.current?.focus(), 0);
      return;
    }
    const key = `${account.id}/edit`; setBusy(key); setEditError(""); setNotice(""); setError("");
    try {
      const updated = await api<Account>(`/accounts/${account.id}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      await reload();
      setEditingId(null);
      setNotice(`已保存“${updated.display_name}”的监控设置`);
      window.setTimeout(() => noticeRef.current?.focus(), 0);
    } catch (caught) {
      setEditError(caught instanceof Error ? caught.message : "保存失败");
    } finally { setBusy(""); }
  }
  function requestDelete(account: Account) {
    setDeleteConfirmId(account.id); setDeleteError(""); setEditingId(null); setEditError(""); setNotice(""); setError("");
  }
  function cancelDelete(id: number) {
    setDeleteConfirmId(null); setDeleteError("");
    window.setTimeout(() => deleteButtonRefs.current[id]?.focus(), 0);
  }
  async function deleteAccount(account: Account) {
    const key = `${account.id}/delete`; setBusy(key); setDeleteError(""); setNotice(""); setError("");
    try {
      const result = await api<{ message: string }>(`/accounts/${account.id}`, { method: "DELETE" });
      setDeleteConfirmId(null);
      setNotice(result.message);
      setTestFeedback(current => { const next = { ...current }; delete next[account.id]; return next; });
      await reload();
      window.setTimeout(() => noticeRef.current?.focus(), 0);
    } catch (caught) {
      setDeleteError(caught instanceof Error ? caught.message : "删除失败");
    } finally { setBusy(""); }
  }
  async function action(id: number, path: string, options: RequestInit = { method: "POST" }) { setBusy(id + path); try { await api(`/accounts/${id}${path}`, options); await reload(); } catch (e) { setError(e instanceof Error ? e.message : "操作失败"); } finally { setBusy(""); } }
  return <div className="view-stack"><header className="page-heading"><div><p className="eyebrow">监控源</p><h1>监控账号</h1></div><div className="heading-actions"><p>管理需要持续关注的公开创作者主页。</p><button className="primary" aria-label={showForm ? "取消" : "+ 添加账号"} onClick={() => setShowForm(!showForm)}>{showForm ? "取消添加" : <><Icon name="plus" size={17} />添加账号</>}</button></div></header>
    {showForm && <form className="account-form panel" onSubmit={add}><div className="form-intro span-2"><div><span className="form-kicker">新监控源</span><h2>添加公开主页</h2></div><p>首次保存会建立内容基线并归档最近一条历史内容；更早内容不会下载。</p></div><label>平台<select value={form.platform} onChange={e => setForm({ ...form, platform: e.target.value as Platform })}>{platforms.map(item => <option key={item} value={item}>{platformNames[item]}</option>)}</select></label><label>显示名称（可选）<input value={form.display_name} onChange={e => setForm({ ...form, display_name: e.target.value })} placeholder="方便识别的名称" /></label><label className="span-2">公开主页 URL<input type="url" required value={form.source_url} onChange={e => setForm({ ...form, source_url: e.target.value })} placeholder="https://..." /></label><label>轮询间隔（分钟）<input type="number" min="5" max="1440" value={form.interval_minutes} onChange={e => setForm({ ...form, interval_minutes: Number(e.target.value) })} /></label><button className="primary" disabled={!!busy}>{busy === "add" ? "正在建立基线并归档…" : "添加并归档最新一条"}</button></form>}
    {notice && <div className="account-notice" role="status" aria-live="polite" tabIndex={-1} ref={noticeRef}><span aria-hidden="true">✓</span><p>{notice}</p><button type="button" aria-label="关闭操作提示" onClick={() => setNotice("")}><Icon name="close" size={16} /></button></div>}
    <section className="account-list" aria-busy={loading}>
      {loading && !accounts.length ? <div className="account-list-loading"><LoadingRows /><LoadingRows /></div> : loadError && !accounts.length ? <div className="empty" role="alert"><span className="empty-icon" aria-hidden="true">!</span><p>账号列表读取失败：{loadError}</p><button className="secondary" onClick={() => void reload()}>重试</button></div> : accounts.length ? accounts.map(account => {
        const isTesting = busy === `${account.id}/test`;
        const isSaving = busy === `${account.id}/edit`;
        const isDeleting = busy === `${account.id}/delete`;
        const accountPolling = account.status === "polling";
        const feedback = testFeedback[account.id];
        if (editingId === account.id) return <article className="account-card account-card-editing" key={account.id} aria-busy={isSaving}>
          <span className={`platform-logo ${account.platform}`}>{platformNames[account.platform].slice(0, 1)}</span>
          <form className="account-edit-form" onSubmit={event => void saveEdit(event, account)} onKeyDown={event => { if (event.key === "Escape" && !isSaving) { event.preventDefault(); cancelEdit(account.id); } }}>
            <div className="account-edit-heading"><div><span className="form-kicker">编辑监控源</span><h2>{account.display_name}</h2></div><span className={`status ${account.status}`}>{statusName(account.status)}</span></div>
            <div className="account-edit-grid">
              <label>平台（不可修改）<input value={platformNames[account.platform]} readOnly aria-readonly="true" className="readonly-field" /></label>
              <label>显示名称<input required maxLength={160} autoFocus value={editForm.display_name} onChange={event => setEditForm({ ...editForm, display_name: event.target.value })} /></label>
              <label className="span-2">公开主页 URL（不可修改）<input type="url" value={editForm.source_url} readOnly aria-readonly="true" aria-label="公开主页 URL（不可修改）" aria-describedby={`source-url-help-${account.id}`} className="readonly-field" /><small id={`source-url-help-${account.id}`}>如需更换主页，请添加新的监控账号。</small></label>
              <label>轮询间隔（分钟）<input type="number" required min="5" max="1440" value={editForm.interval_minutes} onChange={event => setEditForm({ ...editForm, interval_minutes: event.target.value })} /></label>
            </div>
            {editError && <p className="account-operation-error" role="alert">{editError}</p>}
            <div className="account-edit-actions"><button type="button" className="ghost" disabled={isSaving} onClick={() => cancelEdit(account.id)}>取消</button><button type="submit" className="primary" disabled={isSaving}>{isSaving ? "正在保存…" : "保存修改"}</button></div>
          </form>
        </article>;
        return <article className="account-card" key={account.id} aria-busy={isTesting || isDeleting}>
          <div className="account-main"><span className={`platform-logo ${account.platform}`}>{platformNames[account.platform].slice(0, 1)}</span><div>
            <div className="title-line"><h2>{account.display_name}</h2><span className={`status ${account.status}`}>{statusName(account.status)}</span></div>
            <a className="source-link" href={account.source_url} target="_blank" rel="noreferrer"><span className="source-url-text">{account.source_url}</span><span aria-hidden="true">↗</span></a>
            <div className="account-meta"><span>每 {account.interval_minutes} 分钟</span><span>上次 {formatDate(account.last_polled_at)}</span><span>{account.next_poll_at ? `下次 ${formatDate(account.next_poll_at)}` : "等待调度"}</span><span className={account.baseline_established ? "baseline-ready" : "baseline-waiting"}>{account.baseline_established ? "基线已建立" : "等待首次基线"}</span>{account.completeness_status === "gap_detected" && <span className="completeness-gap">监控窗口发现断层</span>}{account.completeness_status === "pending_retry" && <span className="completeness-gap">缺失媒体待重试</span>}{account.consecutive_failures > 0 && <span className="failure-count">连续失败 {account.consecutive_failures} 次</span>}</div>
            {account.completeness_status === "gap_detected" && <p className="account-completeness-warning" role="alert">最近一次发现窗口未与已见水位重叠，窗口之外可能存在尚未归档的发布内容。系统已保留该状态，不会把监控连续性误报为完整。</p>}
            {account.completeness_status === "pending_retry" && <p className="account-completeness-warning" role="alert">至少一条内容的媒体尚未完整下载；引用已写入持久重试队列，即使离开发现窗口也会继续重试。</p>}
            {feedback && <p className={`account-test-feedback ${feedback.tone}`} role={feedback.tone === "error" ? "alert" : "status"} aria-live="polite">{feedback.message}</p>}
            {account.last_error && <details><summary>查看错误</summary><pre>{account.last_error}</pre></details>}
          </div></div>
          <div className="account-actions"><button type="button" className="secondary" aria-label={`立即轮询 ${account.display_name}`} disabled={!!busy || deleteConfirmId === account.id} onClick={() => action(account.id, "/poll")}>立即轮询</button><button type="button" className="ghost" aria-label={isTesting ? `正在测试 ${account.display_name}` : `测试 ${account.display_name}`} disabled={!!busy || deleteConfirmId === account.id} onClick={() => void testAccount(account.id)}>{isTesting ? "测试中…" : "测试"}</button><button type="button" className="ghost subtle" aria-label={`${account.enabled ? "暂停" : "启用"} ${account.display_name}`} disabled={!!busy || deleteConfirmId === account.id} onClick={() => action(account.id, "", { method: "PATCH", body: JSON.stringify({ enabled: !account.enabled }) })}>{account.enabled ? "暂停" : "启用"}</button><button type="button" className="ghost" aria-label={`编辑 ${account.display_name}`} title={accountPolling ? "账号正在采集，完成后可编辑" : undefined} disabled={!!busy || deleteConfirmId === account.id || accountPolling} ref={node => { editButtonRefs.current[account.id] = node; }} onClick={() => startEdit(account)}>编辑</button><button type="button" className="ghost danger-action" aria-label={`删除 ${account.display_name}`} title={accountPolling ? "账号正在采集，完成后可删除" : undefined} disabled={!!busy || deleteConfirmId === account.id || accountPolling} ref={node => { deleteButtonRefs.current[account.id] = node; }} onClick={() => requestDelete(account)}>删除</button></div>
          {deleteConfirmId === account.id && <div className="account-delete-confirm" role="group" aria-labelledby={`delete-title-${account.id}`} aria-describedby={`delete-description-${account.id}`} onKeyDown={event => { if (event.key === "Escape" && !isDeleting) { event.preventDefault(); cancelDelete(account.id); } }}>
            <div><strong id={`delete-title-${account.id}`}>删除“{account.display_name}”？</strong><p id={`delete-description-${account.id}`}>将停止监控这个主页。若账号已有归档，为保护文件，系统只会停用账号，已有内容不会删除。</p>{deleteError && <p className="account-operation-error" role="alert">{deleteError}</p>}</div>
            <div className="account-delete-actions"><button type="button" className="ghost" autoFocus disabled={isDeleting} onClick={() => cancelDelete(account.id)}>取消删除</button><button type="button" className="delete-button" disabled={isDeleting} onClick={() => void deleteAccount(account)}>{isDeleting ? "正在处理…" : "确认删除"}</button></div>
          </div>}
        </article>;
      }) : <Empty text="添加第一个公开账号以开始监控" />}
    </section>
  </div>;
}

function AutoPlatformSessions({ accounts, setError, onImported }: { accounts: Account[]; setError: (value: string) => void; onImported: () => Promise<void> }) {
  const [sessions, setSessions] = useState<PlatformSession[]>([]);
  const [qr, setQr] = useState<PlatformSession | null>(null);
  const [activePlatform, setActivePlatform] = useState<Platform | null>(null);
  const [loginError, setLoginError] = useState("");
  const [busy, setBusy] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [importTarget, setImportTarget] = useState("new");
  const [loading, setLoading] = useState(true);
  const mountedRef = useRef(true);
  const activePlatformRef = useRef<Platform | null>(null);
  const attemptPlatformRef = useRef<Platform | null>(null);
  const qrFor = useRef<Platform | null>(null);
  const loginPanelRef = useRef<HTMLElement>(null);
  const loginTriggerRef = useRef<HTMLButtonElement | null>(null);
  const reloadSequence = useRef(0);

  const reload = useCallback(async () => {
    if (!mountedRef.current) return;
    const sequence = ++reloadSequence.current;
    try {
      const values = await api<PlatformSession[]>("/platform-sessions");
      if (!mountedRef.current || sequence !== reloadSequence.current) return;
      setSessions(values);
      let platform = activePlatformRef.current;
      if (!platform) {
        const pending = values.find(session => session.status === "starting" || session.status === "qr_ready");
        if (pending) {
          platform = pending.platform;
          activePlatformRef.current = platform;
          attemptPlatformRef.current = platform;
          setActivePlatform(platform);
        }
      }
      const session = values.find(value => value.platform === platform);
      if (session?.status === "authenticated") {
        qrFor.current = null;
        setQr(null);
        setLoginError("");
      } else if (session && ["expired", "manual_verification_required", "error"].includes(session.status)) {
        qrFor.current = null;
        setQr(null);
        setLoginError(session.message || "二维码已失效或平台要求额外验证，请重新尝试。");
      } else if (platform && attemptPlatformRef.current === platform && (!session || session.status === "logged_out")) {
        qrFor.current = null;
        setQr(null);
        setLoginError("登录会话已结束，请重新获取二维码。");
      } else if (session && (session.status === "starting" || session.status === "qr_ready") && qrFor.current !== session.platform) {
        try {
          const value = await api<PlatformSession>(`/platform-sessions/${session.platform}/qr`);
          if (!mountedRef.current || sequence !== reloadSequence.current) return;
          qrFor.current = session.platform;
          setQr(value);
          setLoginError("");
          setSessions(current => current.map(item => item.platform === value.platform ? { ...item, ...value } : item));
        } catch (caught) {
          if (!mountedRef.current || sequence !== reloadSequence.current) return;
          // A 404 is expected while the worker is still creating the QR image.
          if (!(caught instanceof ApiError && [404, 409].includes(caught.status))) {
            setLoginError(caught instanceof Error ? caught.message : "无法读取登录二维码");
          }
        }
      }
    } catch (caught) { if (mountedRef.current && sequence === reloadSequence.current) setError(caught instanceof Error ? caught.message : "无法读取平台登录状态"); }
    finally { if (mountedRef.current && sequence === reloadSequence.current) setLoading(false); }
  }, [setError]);

  useEffect(() => {
    mountedRef.current = true;
    let cancelled = false;
    let timer: number | undefined;
    async function poll() {
      await reload();
      if (!cancelled) timer = window.setTimeout(() => void poll(), 1500);
    }
    void poll();
    return () => { mountedRef.current = false; cancelled = true; if (timer !== undefined) window.clearTimeout(timer); reloadSequence.current += 1; };
  }, [reload]);

  useEffect(() => {
    if (!activePlatform || !loginPanelRef.current) return;
    const reducedMotion = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    loginPanelRef.current.scrollIntoView?.({ behavior: reducedMotion ? "auto" : "smooth", block: "center" });
    loginPanelRef.current.focus({ preventScroll: true });
  }, [activePlatform]);

  function closeLoginPanel() {
    const trigger = loginTriggerRef.current;
    activePlatformRef.current = null;
    attemptPlatformRef.current = null;
    qrFor.current = null;
    setActivePlatform(null);
    setQr(null);
    setLoginError("");
    window.setTimeout(() => {
      if (mountedRef.current && trigger?.isConnected) trigger.focus();
    }, 0);
  }

  async function sessionAction(platform: Platform, kind: "login" | "logout") {
    setBusy(`${platform}-${kind}`);
    try {
      if (kind === "login") {
        activePlatformRef.current = platform;
        attemptPlatformRef.current = null;
        setActivePlatform(platform);
        qrFor.current = null;
        setQr(null);
        setLoginError("");
        const value = await api<PlatformSession>(`/platform-sessions/${platform}/login`, { method: "POST" });
        attemptPlatformRef.current = platform;
        if (!mountedRef.current) return;
        setSessions(current => current.map(session => session.platform === platform ? { ...session, ...value } : session));
      } else {
        await api(`/platform-sessions/${platform}`, { method: "DELETE" });
        if (!mountedRef.current) return;
        if (activePlatformRef.current === platform) closeLoginPanel();
      }
      await reload();
    } catch (caught) {
      if (!mountedRef.current) return;
      const message = caught instanceof Error ? caught.message : "平台登录操作失败";
      if (kind === "login") setLoginError(message);
      else setError(message);
    } finally { if (mountedRef.current) setBusy(""); }
  }

  async function upload(event: FormEvent) {
    event.preventDefault(); if (!file) return; setBusy("import");
    try {
      const body = new FormData();
      body.append("file", file);
      body.append("account_mode", importTarget === "new" ? "new" : "existing");
      if (importTarget !== "new") body.append("target_account_id", importTarget);
      await api("/imports", { method: "POST", body });
      if (!mountedRef.current) return;
      setFile(null);
      await onImported();
    }
    catch (caught) { if (mountedRef.current) setError(caught instanceof Error ? caught.message : "导入失败"); }
    finally { if (mountedRef.current) setBusy(""); }
  }

  const activeSession = activePlatform ? sessions.find(session => session.platform === activePlatform) : undefined;
  const loginComplete = activeSession?.status === "authenticated";
  const loginFailed = !!loginError || !!activeSession && ["expired", "manual_verification_required", "error"].includes(activeSession.status);
  const loginPreparing = !!activePlatform && !qr?.image_data_url && !loginComplete && !loginFailed;
  const loginInProgress = !!activePlatform && (loginPreparing || activeSession?.status === "qr_ready");
  const needsManualVerification = activeSession?.status === "manual_verification_required";

  return <div className="view-stack">
    <header className="page-heading"><div><p className="eyebrow">外部 Provider</p><h1>平台登录</h1></div><p>二维码和人工验证由独立部署的 Provider 提供。SeeU 不安装采集器，也不会接收、记录或返回 Cookie。</p></header>
    <section className="session-grid" aria-busy={loading}>{sessions.map(session => {
      const waitingForQr = session.status === "starting" || session.status === "qr_ready";
      const loginLabel = waitingForQr ? "等待扫码" : session.status === "authenticated" ? "重新登录" : "开始登录";
      const description = session.message || (session.status === "authenticated"
        ? "登录状态可用，可以开始测试和采集账号。"
        : waitingForQr ? "登录二维码已在当前页面准备，扫码后状态会自动更新。" : "点击开始登录，二维码会直接显示在当前页面。");
      return <article className="panel session-card" key={session.platform}>
        <div className="session-card-head"><span className={`platform-logo ${session.platform}`}>{platformNames[session.platform].slice(0, 1)}</span><div><p>{session.platform}</p><h2>{platformNames[session.platform]}</h2></div><span className={`status ${session.status}`}>{statusName(session.status)}</span></div>
        <p className="muted">{description}</p>
        <div className="session-actions"><button className={session.status === "authenticated" ? "secondary" : "primary"} aria-label={`${loginLabel} ${platformNames[session.platform]}`} disabled={!!busy || waitingForQr || (loginInProgress && activePlatform !== session.platform)} onClick={event => { loginTriggerRef.current = event.currentTarget; void sessionAction(session.platform, "login"); }}>{busy === `${session.platform}-login` ? "正在启动…" : loginLabel}</button><button className="ghost subtle" aria-label={`注销 ${platformNames[session.platform]}`} disabled={!!busy || session.status === "logged_out"} onClick={() => void sessionAction(session.platform, "logout")}>注销</button></div>
      </article>;
    })}</section>
    {loading && !sessions.length && <div className="session-grid skeleton-grid" role="status" aria-label="正在读取平台登录状态"><span className="session-skeleton" /><span className="session-skeleton" /><span className="session-skeleton" /><span className="session-skeleton" /></div>}
    {!loading && !sessions.length && <Empty text="尚未配置外部 Provider，或 Provider 未声明可管理的平台会话" />}
    {activePlatform && <section ref={loginPanelRef} className={`panel qr-panel ${loginComplete ? "login-complete" : loginFailed ? "login-failed" : ""}`} role={loginFailed ? "alert" : "status"} aria-live={loginFailed ? "assertive" : "polite"} aria-busy={loginPreparing} aria-labelledby="platform-login-title" tabIndex={-1}>
      <div className="qr-panel-copy">
        <p className="eyebrow">{loginComplete ? "登录完成" : loginFailed ? "登录遇到问题" : qr?.image_data_url ? "扫码登录" : "正在生成二维码"}</p>
        <h2 id="platform-login-title">{loginComplete ? `${platformNames[activePlatform]} 登录成功` : loginFailed ? `${platformNames[activePlatform]} 登录未完成` : qr?.image_data_url ? `使用 ${platformNames[activePlatform]} App 扫码` : `正在准备 ${platformNames[activePlatform]} 登录二维码`}</h2>
        <p className="muted">{loginComplete ? "登录状态已保存在外部 Provider，可以开始测试和采集账号。" : loginFailed ? loginError || activeSession?.message || "二维码已失效或平台要求额外验证，请重新尝试。" : qr?.image_data_url ? "请在二维码失效前完成扫码；登录成功后，本页会自动更新。" : "外部 Provider 正在准备平台会话，二维码生成后会自动显示在这里。"}</p>
        {needsManualVerification && activeSession?.manual_verification_url && <div className="manual-verification"><a className="button-link" href={activeSession.manual_verification_url} target="_blank" rel="noreferrer">打开 Provider 人工验证界面 <span aria-hidden="true">↗</span></a><small>该地址由外部 Provider 提供；请按照 Provider 的部署文档安全访问。</small></div>}
        <div className="qr-panel-actions">
          {loginComplete ? <button type="button" className="secondary" onClick={closeLoginPanel}>完成</button> : loginFailed ? <><button type="button" className="primary" disabled={!!busy} onClick={() => void sessionAction(activePlatform, "login")}>{busy === `${activePlatform}-login` ? "正在重试…" : "重新获取二维码"}</button><button type="button" className="ghost" disabled={!!busy} onClick={() => activeSession?.status === "starting" || activeSession?.status === "qr_ready" ? void sessionAction(activePlatform, "logout") : closeLoginPanel()}>{activeSession?.status === "starting" || activeSession?.status === "qr_ready" ? "取消登录" : "关闭"}</button></> : <button type="button" className="ghost" disabled={!!busy} onClick={() => void sessionAction(activePlatform, "logout")}>取消登录</button>}
        </div>
      </div>
      <div className="qr-visual">
        {qr?.image_data_url && !loginComplete && !loginFailed ? <img src={qr.image_data_url} alt={`${platformNames[activePlatform]} 登录二维码`} /> : loginComplete ? <div className="qr-result success" aria-hidden="true"><span>✓</span></div> : loginFailed ? <div className="qr-result failure" aria-hidden="true"><span>!</span></div> : <div className="qr-loader" aria-hidden="true"><i /><span>正在安全生成</span></div>}
      </div>
    </section>}
    <form className="panel import-panel" onSubmit={upload}><div><p className="eyebrow">数据导入</p><h2>导入归档 ZIP</h2><p className="muted">选择明确的归属账号；文件仍需通过路径、大小、MIME 与 SHA-256 校验。</p></div><label><span>归属账号</span><select value={importTarget} onChange={event => setImportTarget(event.target.value)}><option value="new">按清单新建导入账号</option>{accounts.map(account => <option key={account.id} value={String(account.id)}>{platformNames[account.platform]} · {account.display_name}</option>)}</select></label><label><span>选择 ZIP 文件</span><input type="file" accept=".zip,application/zip" onChange={event => setFile(event.target.files?.[0] || null)} required /></label><button className="secondary" disabled={!file || busy === "import"}>{busy === "import" ? "正在验证…" : "验证并导入"}</button></form>
  </div>;
}

function Accounts(props: { accounts: Account[]; loading: boolean; loadError: string; reload: () => Promise<void>; setError: (value: string) => void }) {
  return <AccountsList {...props} />;
}

function Runs({ runs, accounts, page, onPage, loading }: { runs: CrawlRun[]; accounts: Account[]; page: PageResponse<CrawlRun>; onPage: (offset: number) => void; loading: boolean }) {
  const accountMap = useMemo(() => new Map(accounts.map(item => [item.id, item.display_name])), [accounts]);
  return <div className="view-stack"><header className="page-heading"><div><p className="eyebrow">采集活动</p><h1>任务记录</h1></div><p>查看每次轮询的发现、归档与诊断结果。单条内容失败不会阻塞同账号的其他新增内容。</p></header><section className="panel table-wrap">{loading && !runs.length ? <div className="table-loading"><LoadingRows /><LoadingRows /><LoadingRows /></div> : <><table><caption className="sr-only">账号采集任务记录</caption><thead><tr><th scope="col">账号</th><th scope="col">开始时间</th><th scope="col">状态</th><th scope="col">发现</th><th scope="col">归档</th><th scope="col">诊断</th></tr></thead><tbody>{runs.map(run => <tr key={run.id}><td data-label="账号"><strong>{accountMap.get(run.account_id) || `#${run.account_id}`}</strong></td><td data-label="开始时间">{formatDate(run.started_at)}</td><td data-label="状态"><span className={`status ${run.status}`}>{statusName(run.status)}</span></td><td data-label="发现">{run.discovered_count}</td><td data-label="归档">{run.archived_count}</td><td data-label="诊断" className="error-cell">{run.error || "—"}</td></tr>)}</tbody></table>{!runs.length && <Empty text="暂无任务记录" />}<Pagination total={page.total} offset={page.offset} limit={page.limit} hasMore={page.has_more} loading={loading} onPage={onPage} label="任务记录" /></>}</section></div>;
}

function MediaPreview({ item, contentId, title, index }: { item: ContentDetail["metadata"]["media"][number]; contentId: number; title: string; index: number }) {
  const src = mediaUrl(contentId, item.local_path);
  const kind = item.kind || item.mime_type.split("/", 1)[0];
  if (kind === "video" || item.mime_type.startsWith("video/")) return <video controls preload="metadata" src={src} />;
  if (kind === "audio" || item.mime_type.startsWith("audio/")) return <div className="audio-preview"><strong>音频 {index + 1}</strong><audio controls preload="metadata" src={src} /></div>;
  if (kind === "image" || item.mime_type.startsWith("image/")) return <img loading="lazy" src={src} alt={`${title} 图片 ${index + 1}`} />;
  return <a className="unknown-media" href={src} target="_blank" rel="noreferrer"><strong>此媒体类型无法在页面内预览</strong><span>{item.mime_type || item.kind || "未知类型"} · 打开认证文件</span></a>;
}

function DetailModal({ detail, onClose }: { detail: ContentDetail; onClose: () => void }) {
  const modalRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    modalRef.current?.querySelector<HTMLButtonElement>(".modal-close")?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
      if (event.key !== "Tab" || !modalRef.current) return;
      const focusable = Array.from(modalRef.current.querySelectorAll<HTMLElement>("button:not([disabled]), a[href], video[controls], audio[controls], [tabindex]:not([tabindex='-1'])"));
      if (!focusable.length) return;
      const first = focusable[0]; const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown); document.body.style.overflow = previousOverflow; previousFocus?.focus(); };
  }, [onClose]);
  const media = detail.metadata.media || [];
  return <div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><article ref={modalRef} className="detail-modal" role="dialog" aria-modal="true" aria-labelledby="detail-title"><button type="button" className="modal-close" onClick={onClose} aria-label="关闭详情"><Icon name="close" size={20} /></button><div className="detail-head"><div className="detail-meta"><span className={`platform-chip ${detail.platform}`}>{platformNames[detail.platform]}</span><span>{contentTypeName(detail.content_type)}</span></div><h1 id="detail-title">{detail.title}</h1><p>{detail.author || "未知作者"} · {formatDate(detail.published_at)}</p><a className="button-link" href={detail.source_url} target="_blank" rel="noreferrer">查看公开原文 <span aria-hidden="true">↗</span></a></div>{detail.integrity_status && detail.integrity_status !== "complete" && <p className="detail-integrity-warning" role="alert">此旧归档未通过当前完整性账本核验，请检查原始文件或重新采集。</p>}{media.length > 0 && <div className="media-gallery">{media.map((item, index) => <MediaPreview key={item.local_path} item={item} contentId={detail.id} title={detail.title} index={index} />)}</div>}<section className="detail-copy"><p className="eyebrow">内容正文</p><h2>文案</h2><p>{detail.metadata.text || "暂无文案"}</p></section><section className="file-list"><p className="eyebrow">文件信息</p><h2>归档文件</h2>{media.length ? media.map(item => <div key={item.local_path}><code>{item.local_path}</code><small>{formatBytes(item.size_bytes)} · SHA-256 {item.sha256.slice(0, 12)}…</small></div>) : <p className="muted">这条归档没有媒体文件。</p>}</section></article></div>;
}

export default function App() {
  const [auth, setAuth] = useState<AuthResponse | null>(null);
  const [checking, setChecking] = useState(true);
  const [startupError, setStartupError] = useState("");
  const [view, setView] = useState<View>("overview");
  const [error, setError] = useState("");
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [contents, setContents] = useState<Content[]>([]);
  const [runs, setRuns] = useState<CrawlRun[]>([]);
  const [contentPage, setContentPage] = useState<PageResponse<Content>>({ items: [], total: 0, offset: 0, limit: CONTENT_PAGE_SIZE, has_more: false });
  const [runPage, setRunPage] = useState<PageResponse<CrawlRun>>({ items: [], total: 0, offset: 0, limit: RUN_PAGE_SIZE, has_more: false });
  const [summary, setSummary] = useState<Summary | null>(null);
  const [storage, setStorage] = useState<StorageInfo | null>(null);
  const [feedFilters, setFeedFilters] = useState<FeedFilters>(emptyFeedFilters);
  const [loadingFeed, setLoadingFeed] = useState(false);
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [dashboardLoading, setDashboardLoading] = useState(false);
  const [detail, setDetail] = useState<ContentDetail | null>(null);
  const [accountsLoading, setAccountsLoading] = useState(false);
  const [accountsLoadError, setAccountsLoadError] = useState("");
  const feedGeneration = useRef(0);
  const feedRequest = useRef<{ controller: AbortController; generation: number } | null>(null);
  const feedLocation = useRef({ query: "", offset: 0 });
  const mobileLayout = useMobileLayout();
  const loadAccounts = useCallback(async () => { setAccountsLoading(true); try { setAccounts(await api<Account[]>("/accounts")); setAccountsLoadError(""); } catch (caught) { setAccountsLoadError(caught instanceof Error ? caught.message : "读取失败"); throw caught; } finally { setAccountsLoading(false); } }, []);
  const cancelFeedRequest = useCallback((clearLoading = false) => {
    feedGeneration.current += 1;
    feedRequest.current?.controller.abort();
    feedRequest.current = null;
    if (clearLoading) setLoadingFeed(false);
  }, []);
  const loadFeed = useCallback(async (
    params = new URLSearchParams(),
    offset = 0,
    options: { showLoading?: boolean; skipIfBusy?: boolean; updateLocation?: boolean } = {},
  ) => {
    const { showLoading = true, skipIfBusy = false, updateLocation = true } = options;
    if (skipIfBusy && feedRequest.current) return;
    feedRequest.current?.controller.abort();
    const request = new AbortController();
    const generation = feedGeneration.current + 1;
    feedGeneration.current = generation;
    feedRequest.current = { controller: request, generation };
    if (showLoading) setLoadingFeed(true);
    const query = new URLSearchParams(params);
    query.delete("offset"); query.delete("limit");
    const queryString = query.toString();
    if (updateLocation) feedLocation.current = { query: queryString, offset };
    query.set("offset", String(offset)); query.set("limit", String(CONTENT_PAGE_SIZE));
    try {
      const page = normalizePage(await api<PageResponse<Content> | Content[]>(`/contents?${query}`, { signal: request.signal }), offset, CONTENT_PAGE_SIZE);
      if (request.signal.aborted || feedGeneration.current !== generation) return;
      feedLocation.current = { query: queryString, offset: page.offset };
      setContents(page.items); setContentPage(page);
    }
    catch (caught) {
      if (feedGeneration.current !== generation || request.signal.aborted || (caught instanceof Error && caught.name === "AbortError")) return;
      setError(caught instanceof Error ? caught.message : "读取归档失败");
    }
    finally {
      if (feedRequest.current?.generation === generation) {
        feedRequest.current = null;
        setLoadingFeed(false);
      }
    }
  }, []);
  const loadRuns = useCallback(async (offset = 0) => {
    setLoadingRuns(true);
    try {
      const page = normalizePage(await api<PageResponse<CrawlRun> | CrawlRun[]>(`/runs?offset=${offset}&limit=${RUN_PAGE_SIZE}`), offset, RUN_PAGE_SIZE);
      setRuns(page.items); setRunPage(page);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "读取任务记录失败"); }
    finally { setLoadingRuns(false); }
  }, []);
  const loadAll = useCallback(async () => {
    setDashboardLoading(true);
    setFeedFilters(emptyFeedFilters);
    try {
      const results = await Promise.allSettled([
        loadAccounts(),
        loadFeed(new URLSearchParams(), 0, { showLoading: false }),
        api<PageResponse<CrawlRun> | CrawlRun[]>(`/runs?offset=0&limit=${RUN_PAGE_SIZE}`).then(value => { const page = normalizePage(value, 0, RUN_PAGE_SIZE); setRuns(page.items); setRunPage(page); }),
        api<Summary>("/summary").then(setSummary),
        api<StorageInfo>("/storage").then(setStorage)
      ]);
      const failures = results.flatMap(result => result.status === "rejected" ? [result.reason] : []);
      if (!failures.length) return;
      const unauthorized = failures.find(caught => caught instanceof ApiError && caught.status === 401);
      if (unauthorized) { clearAuth(); setAuth(null); return; }
      const caught = failures[0]; setError(caught instanceof Error ? caught.message : "部分数据读取失败");
    } finally { setDashboardLoading(false); }
  }, [loadAccounts, loadFeed]);
  const refreshVisible = useCallback(async () => {
    if (document.visibilityState === "hidden") return;
    const currentFeed = feedLocation.current;
    const results = await Promise.allSettled([
      api<Account[]>("/accounts").then(setAccounts),
      loadFeed(new URLSearchParams(currentFeed.query), currentFeed.offset, { showLoading: false, skipIfBusy: true, updateLocation: false }),
      api<PageResponse<CrawlRun> | CrawlRun[]>(`/runs?offset=${runPage.offset}&limit=${RUN_PAGE_SIZE}`).then(value => { const page = normalizePage(value, runPage.offset, RUN_PAGE_SIZE); setRuns(page.items); setRunPage(page); }),
      api<Summary>("/summary").then(setSummary),
      api<StorageInfo>("/storage").then(setStorage)
    ]);
    const failure = results.find(result => result.status === "rejected") as PromiseRejectedResult | undefined;
    if (failure && !(failure.reason instanceof ApiError && failure.reason.status === 401)) setError(failure.reason instanceof Error ? failure.reason.message : "自动刷新失败");
  }, [loadFeed, runPage.offset]);
  const checkAuth = useCallback(async () => {
    setChecking(true); setStartupError("");
    try { const value = await api<AuthResponse>("/auth/me"); saveAuth(value); setAuth(value); }
    catch (caught) {
      clearAuth(); setAuth(null);
      if (!(caught instanceof ApiError && caught.status === 401)) setStartupError(caught instanceof Error ? caught.message : "归档服务暂时不可用");
    } finally { setChecking(false); }
  }, []);
  useEffect(() => {
    function unauthorized() { cancelFeedRequest(true); clearAuth(); setAuth(null); setDetail(null); }
    window.addEventListener(API_UNAUTHORIZED_EVENT, unauthorized);
    return () => window.removeEventListener(API_UNAUTHORIZED_EVENT, unauthorized);
  }, [cancelFeedRequest]);
  useEffect(() => { void checkAuth(); return () => cancelFeedRequest(); }, [cancelFeedRequest, checkAuth]);
  useEffect(() => { if (auth) void loadAll(); }, [auth, loadAll]);
  const polling = accounts.some(account => account.status === "polling");
  useEffect(() => {
    if (!auth) return;
    let timer: number | undefined;
    let cancelled = false;
    const schedule = () => {
      if (timer !== undefined) window.clearTimeout(timer);
      if (cancelled || document.visibilityState === "hidden") return;
      timer = window.setTimeout(async () => { await refreshVisible(); schedule(); }, polling ? 5000 : 30000);
    };
    const visibilityChanged = () => {
      if (document.visibilityState === "hidden") { if (timer !== undefined) window.clearTimeout(timer); return; }
      void refreshVisible().finally(schedule);
    };
    document.addEventListener("visibilitychange", visibilityChanged);
    schedule();
    return () => { cancelled = true; if (timer !== undefined) window.clearTimeout(timer); document.removeEventListener("visibilitychange", visibilityChanged); };
  }, [auth, polling, refreshVisible]);
  async function logout() { try { await api("/auth/logout", { method: "POST" }); } finally { cancelFeedRequest(true); clearAuth(); setAuth(null); } }
  async function openDetail(id: number) { try { setDetail(await api<ContentDetail>(`/contents/${id}`)); } catch (caught) { setError(caught instanceof Error ? caught.message : "读取详情失败"); } }
  function changeView(next: View) {
    setView(next);
    const reducedMotion = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: 0, behavior: reducedMotion ? "auto" : "smooth" });
  }
  const closeDetail = useCallback(() => setDetail(null), []);
  if (checking) return <div className="app-loader" role="status"><BrandMark /><span>正在连接归档服务</span><i aria-hidden="true" /></div>;
  if (startupError) return <ConnectionError message={startupError} onRetry={() => void checkAuth()} />;
  if (!auth) return <Login onLogin={setAuth} />;
  const nav: { id: View; label: string; icon: IconName }[] = [
    { id: "overview", label: "概览", icon: "overview" },
    { id: "feed", label: "内容归档", icon: "archive" },
    { id: "accounts", label: "监控账号", icon: "accounts" },
    { id: "sessions", label: "平台登录", icon: "sessions" },
    { id: "runs", label: "任务记录", icon: "runs" }
  ];
  const currentLabel = nav.find(item => item.id === view)?.label || "概览";
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">跳到主要内容</a>
    {!mobileLayout && <aside className="sidebar" aria-label="应用侧栏">
      <div className="sidebar-brand"><BrandMark compact /><div><strong>{appTitle}</strong><small>公开内容归档</small></div></div>
      <nav aria-label="主导航">{nav.map(item => <button type="button" key={item.id} className={view === item.id ? "active" : ""} aria-current={view === item.id ? "page" : undefined} onClick={() => changeView(item.id)}><span className="nav-icon"><Icon name={item.icon} /></span><span>{item.label}</span></button>)}</nav>
      <div className="sidebar-foot"><div className="admin-profile"><span className="admin-avatar">{auth.username.slice(0, 1).toUpperCase()}</span><p><strong>{auth.username}</strong><small>Token 认证 · 配置后重启生效</small></p></div><div className="sidebar-actions"><button type="button" onClick={logout}>退出登录</button></div></div>
    </aside>}
    <main className="main" id="main-content" tabIndex={-1}>
      {mobileLayout && <header className="mobile-top"><div className="mobile-brand"><BrandMark compact /><div><strong>公开内容归档</strong><span>{currentLabel}</span></div></div><div className="mobile-actions"><button type="button" className="mobile-logout" onClick={logout}>退出</button></div></header>}
      {error && <ErrorBanner message={error} onClose={() => setError("")} />}
      {view === "overview" && <Overview summary={summary} storage={storage} accounts={accounts} loading={dashboardLoading} />}
      {view === "feed" && <Feed contents={contents} accounts={accounts} filters={feedFilters} setFilters={setFeedFilters} onOpen={openDetail} onRefresh={params => void loadFeed(params)} onPage={offset => void loadFeed(new URLSearchParams(feedLocation.current.query), offset)} page={contentPage} loading={loadingFeed || (dashboardLoading && !contents.length)} />}
      {view === "accounts" && <Accounts accounts={accounts} loading={accountsLoading} loadError={accountsLoadError} reload={loadAll} setError={setError} />}
      {view === "sessions" && <AutoPlatformSessions accounts={accounts} setError={setError} onImported={loadAll} />}
      {view === "runs" && <Runs runs={runs} accounts={accounts} page={runPage} onPage={offset => void loadRuns(offset)} loading={loadingRuns || (dashboardLoading && !runs.length)} />}
    </main>
    {mobileLayout && <nav className="mobile-nav" aria-label="移动端主导航">{nav.map(item => <button type="button" key={item.id} className={view === item.id ? "active" : ""} aria-current={view === item.id ? "page" : undefined} onClick={() => changeView(item.id)}><Icon name={item.icon} size={19} /><span>{item.label}</span></button>)}</nav>}
    {detail && <DetailModal detail={detail} onClose={closeDetail} />}
  </div>;
}
