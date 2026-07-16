import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import App from "./App";

const account = {
  id: 1,
  platform: "douyin",
  display_name: "已有抖音账号",
  slug: "existing-douyin",
  source_url: "https://v.douyin.com/existing/",
  enabled: true,
  interval_minutes: 10,
  baseline_established: true,
  status: "healthy",
  consecutive_failures: 0,
  last_error: null,
  last_polled_at: "2026-07-11T07:13:06Z",
  next_poll_at: "2026-07-11T07:23:06Z",
  created_at: "2026-07-10T22:16:27Z",
};

const storage = {
  total_bytes: 100,
  used_bytes: 10,
  free_bytes: 90,
  archive_bytes: 0,
  minimum_free_bytes: 5,
  downloads_paused: false,
};

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

function requestPath(input: RequestInfo | URL): string {
  return new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://localhost").pathname;
}

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://localhost");
}

function urlHasFilter(input: RequestInfo | URL): boolean {
  return requestUrl(input).searchParams.has("platform");
}

function archivedContent(id: number, title: string, platform = "douyin") {
  return {
    id,
    account_id: 1,
    platform,
    remote_id: `post-${id}`,
    title,
    author: "Race Test",
    content_type: "video",
    source_url: `https://example.com/post-${id}`,
    published_at: "2026-07-15T08:00:00Z",
    collected_at: "2026-07-15T08:10:00Z",
    summary: `Summary for ${id}`,
    media_count: 1,
    status: "complete",
    error: null,
  };
}

function expectAborted(signal: AbortSignal | null): void {
  expect(signal).not.toBeNull();
  expect((signal as AbortSignal).aborted).toBe(true);
}

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

describe("dashboard account loading", () => {
  it("keeps a filtered feed when an older automatic refresh resolves last", async () => {
    let contentReads = 0;
    let finishAutomaticRefresh: ((response: Response) => void) | undefined;
    let automaticRefreshSignal: AbortSignal | null = null;
    const pendingAutomaticRefresh = new Promise<Response>(resolve => { finishAutomaticRefresh = resolve; });
    const filtered = archivedContent(2, "FILTERED RESULT");
    const stale = archivedContent(3, "STALE AUTOMATIC RESULT");

    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      const path = url.pathname;
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts" || path === "/api/runs") return json([]);
      if (path === "/api/summary") return json({ accounts: 0, healthy_accounts: 0, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      if (path === "/api/contents") {
        contentReads += 1;
        if (contentReads === 1) return json({ items: [], total: 0, offset: 0, limit: 24, has_more: false });
        if (url.searchParams.get("platform") === "douyin") {
          return json({ items: [filtered], total: 1, offset: 0, limit: 24, has_more: false });
        }
        automaticRefreshSignal = init?.signal || null;
        return pendingAutomaticRefresh;
      }
      return json({ detail: "not found" }, 404);
    }));

    const { container } = render(<App />);
    await waitFor(() => expect(contentReads).toBe(1));
    const feedNav = container.querySelector<HTMLButtonElement>(".sidebar nav button:nth-child(2)");
    expect(feedNav).not.toBeNull();
    fireEvent.click(feedNav!);

    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() => expect(contentReads).toBe(2));
    const platformFilter = container.querySelectorAll<HTMLSelectElement>(".filters select")[0];
    fireEvent.change(platformFilter, { target: { value: "douyin" } });
    fireEvent.submit(container.querySelector<HTMLFormElement>("form.filters")!);

    expect(await screen.findByText("FILTERED RESULT")).toBeInTheDocument();
    expectAborted(automaticRefreshSignal);
    await act(async () => finishAutomaticRefresh?.(json({ items: [stale], total: 1, offset: 0, limit: 24, has_more: false })));
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText("FILTERED RESULT")).toBeInTheDocument();
    expect(screen.queryByText("STALE AUTOMATIC RESULT")).not.toBeInTheDocument();
  });

  it("keeps a new filter when an older page request resolves last", async () => {
    let finishOldPage: ((response: Response) => void) | undefined;
    let oldPageSignal: AbortSignal | null = null;
    const pendingOldPage = new Promise<Response>(resolve => { finishOldPage = resolve; });
    const first = archivedContent(10, "FIRST PAGE");
    const stalePage = archivedContent(11, "STALE SECOND PAGE");
    const filtered = archivedContent(12, "NEW FILTER RESULT", "weibo");

    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      const path = url.pathname;
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts" || path === "/api/runs") return json([]);
      if (path === "/api/summary") return json({ accounts: 0, healthy_accounts: 0, contents: 48, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      if (path === "/api/contents") {
        if (url.searchParams.get("platform") === "weibo") {
          return json({ items: [filtered], total: 1, offset: 0, limit: 24, has_more: false });
        }
        if (url.searchParams.get("offset") === "24") {
          oldPageSignal = init?.signal || null;
          return pendingOldPage;
        }
        return json({ items: [first], total: 48, offset: 0, limit: 24, has_more: true });
      }
      return json({ detail: "not found" }, 404);
    }));

    const { container } = render(<App />);
    await waitFor(() => expect(container.querySelector(".sidebar")).not.toBeNull());
    fireEvent.click(container.querySelector<HTMLButtonElement>(".sidebar nav button:nth-child(2)")!);
    expect(await screen.findByText("FIRST PAGE")).toBeInTheDocument();
    fireEvent.click(container.querySelectorAll<HTMLButtonElement>(".pagination button")[1]);
    await waitFor(() => expect(oldPageSignal).not.toBeNull());

    const platformFilter = container.querySelectorAll<HTMLSelectElement>(".filters select")[0];
    fireEvent.change(platformFilter, { target: { value: "weibo" } });
    fireEvent.submit(container.querySelector<HTMLFormElement>("form.filters")!);

    expect(await screen.findByText("NEW FILTER RESULT")).toBeInTheDocument();
    expectAborted(oldPageSignal);
    await act(async () => finishOldPage?.(json({ items: [stalePage], total: 48, offset: 24, limit: 24, has_more: false })));
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText("NEW FILTER RESULT")).toBeInTheDocument();
    expect(screen.queryByText("STALE SECOND PAGE")).not.toBeInTheDocument();
  });

  it("aborts an active feed request when the app unmounts", async () => {
    let feedSignal: AbortSignal | null = null;
    const pendingFeed = new Promise<Response>(() => undefined);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/contents") { feedSignal = init?.signal || null; return pendingFeed; }
      if (path === "/api/accounts" || path === "/api/runs") return json([]);
      if (path === "/api/summary") return json({ accounts: 0, healthy_accounts: 0, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    }));

    const view = render(<App />);
    await waitFor(() => expect(feedSignal).not.toBeNull());
    view.unmount();

    expectAborted(feedSignal);
  });

  it("aborts an active feed request when another API call returns 401", async () => {
    let storageReads = 0;
    let feedSignal: AbortSignal | null = null;
    const pendingFeed = new Promise<Response>(() => undefined);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts" || path === "/api/runs") return json([]);
      if (path === "/api/summary") return json({ accounts: 0, healthy_accounts: 0, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return storageReads++ === 0 ? json(storage) : json({ detail: "session expired" }, 401);
      if (path === "/api/contents") {
        if (!urlHasFilter(input)) return json([]);
        feedSignal = init?.signal || null;
        return pendingFeed;
      }
      return json({ detail: "not found" }, 404);
    }));

    const { container } = render(<App />);
    await waitFor(() => expect(container.querySelector(".sidebar")).not.toBeNull());
    fireEvent.click(container.querySelector<HTMLButtonElement>(".sidebar nav button:nth-child(2)")!);
    const platformFilter = container.querySelectorAll<HTMLSelectElement>(".filters select")[0];
    fireEvent.change(platformFilter, { target: { value: "douyin" } });
    fireEvent.submit(container.querySelector<HTMLFormElement>("form.filters")!);
    await waitFor(() => expect(feedSignal).not.toBeNull());

    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() => expect(container.querySelector(".login-form")).not.toBeNull());

    expectAborted(feedSignal);
  });
  it("shows a retryable service error instead of treating server failure as logged out", async () => {
    let authReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path === "/api/auth/me" && authReads++ === 0) return json({ detail: "服务正在启动" }, 503);
      if (path === "/api/auth/me") return json({ detail: "未登录" }, 401);
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);

    expect(await screen.findByRole("heading", { name: "暂时无法连接归档服务" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "欢迎回来" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新连接" }));
    expect(await screen.findByRole("heading", { name: "欢迎回来" })).toBeInTheDocument();
  });

  it("keeps a successful account response when another dashboard request fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts") return json([account]);
      if (path === "/api/contents") return json([]);
      if (path === "/api/runs") return json({ detail: "任务记录暂时不可用" }, 500);
      if (path === "/api/summary") return json({ detail: "统计暂时不可用" }, 500);
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);

    expect(await screen.findByText("已有抖音账号")).toBeInTheDocument();
    expect(screen.getAllByText("我会一直看着你")).not.toHaveLength(0);
    expect(screen.queryByText("还没有添加监控账号")).not.toBeInTheDocument();
  });

  it("reloads the account list when adding a URL that already exists", async () => {
    let accountReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = (init?.method || "GET").toUpperCase();
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts" && method === "GET") return json(accountReads++ === 0 ? [] : [account]);
      if (path === "/api/accounts" && method === "POST") return json({ detail: "该账号已经存在" }, 409);
      if (path === "/api/contents" || path === "/api/runs" || path === "/api/platform-sessions") return json([]);
      if (path === "/api/summary") return json({ accounts: 1, healthy_accounts: 1, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);
    await screen.findByText("还没有添加监控账号");
    fireEvent.click(screen.getByRole("button", { name: /监控账号/ }));
    fireEvent.click(screen.getByRole("button", { name: "+ 添加账号" }));
    fireEvent.change(screen.getByLabelText("平台"), { target: { value: "douyin" } });
    fireEvent.change(screen.getByLabelText("公开主页 URL"), { target: { value: account.source_url } });
    fireEvent.click(screen.getByRole("button", { name: "添加并归档最新一条" }));

    expect(await screen.findByText("已有抖音账号")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("该账号已经存在");
  });

  it("polls immediately after adding an account and refreshes the saved latest item", async () => {
    let accountReads = 0;
    let initialized = false;
    let finishPoll: ((response: Response) => void) | undefined;
    const pendingPoll = new Promise<Response>(resolve => { finishPoll = resolve; });
    const latest = {
      id: 9,
      account_id: 1,
      platform: "douyin",
      remote_id: "history-latest",
      title: "最近一条历史内容",
      author: "已有抖音账号",
      content_type: "video",
      source_url: "https://example.com/history-latest",
      published_at: "2026-07-15T08:00:00Z",
      collected_at: "2026-07-15T08:10:00Z",
      summary: "添加账号后保存的最近内容",
      media_count: 1,
      status: "complete",
      error: null,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = (init?.method || "GET").toUpperCase();
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts" && method === "GET") return json(accountReads++ === 0 ? [] : [account]);
      if (path === "/api/accounts" && method === "POST") return json({ ...account, baseline_established: false, status: "pending" }, 201);
      if (path === "/api/accounts/1/poll" && method === "POST") return pendingPoll;
      if (path === "/api/contents") return json(initialized ? [latest] : []);
      if (path === "/api/runs") return json([]);
      if (path === "/api/platform-sessions") return json([]);
      if (path === "/api/summary") return json({ accounts: initialized ? 1 : 0, healthy_accounts: initialized ? 1 : 0, contents: initialized ? 1 : 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await screen.findByText("还没有添加监控账号");
    fireEvent.click(screen.getByRole("button", { name: /监控账号/ }));
    fireEvent.click(screen.getByRole("button", { name: "+ 添加账号" }));
    fireEvent.change(screen.getByLabelText("平台"), { target: { value: "douyin" } });
    fireEvent.change(screen.getByLabelText("公开主页 URL"), { target: { value: account.source_url } });
    fireEvent.click(screen.getByRole("button", { name: "添加并归档最新一条" }));

    expect(await screen.findByRole("button", { name: "正在建立基线并归档…" })).toBeDisabled();
    const pollCall = fetchMock.mock.calls.find(([input]) => requestPath(input) === "/api/accounts/1/poll");
    expect(pollCall?.[1]?.method).toBe("POST");
    expect(new Headers(pollCall?.[1]?.headers).get("X-CSRF-Token")).toBe("csrf");

    initialized = true;
    await act(async () => finishPoll?.(json({
      id: 12,
      account_id: 1,
      started_at: "2026-07-15T08:00:00Z",
      finished_at: "2026-07-15T08:10:00Z",
      status: "baseline",
      discovered_count: 3,
      archived_count: 1,
      error: null,
      details: { seed_content_id: "history-latest", seed_archived: true },
    })));

    expect(await screen.findByText("已有抖音账号")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "内容归档" }));
    expect(await screen.findByText("最近一条历史内容")).toBeInTheDocument();
  });

  it("shows progress and the number of published items returned by account testing", async () => {
    let finishTest: ((response: Response) => void) | undefined;
    const pendingTest = new Promise<Response>(resolve => { finishTest = resolve; });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = (init?.method || "GET").toUpperCase();
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts/1/test" && method === "POST") return pendingTest;
      if (path === "/api/accounts") return json([account]);
      if (path === "/api/contents" || path === "/api/runs" || path === "/api/platform-sessions") return json([]);
      if (path === "/api/summary") return json({ accounts: 1, healthy_accounts: 1, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await screen.findByText("已有抖音账号");
    fireEvent.click(screen.getByRole("button", { name: /监控账号/ }));
    fireEvent.click(screen.getByRole("button", { name: "测试 已有抖音账号" }));

    expect(screen.getByRole("button", { name: "正在测试 已有抖音账号" })).toBeDisabled();
    const testCall = fetchMock.mock.calls.find(([input]) => requestPath(input) === "/api/accounts/1/test");
    expect(testCall?.[1]?.method).toBe("POST");
    expect(new Headers(testCall?.[1]?.headers).get("X-CSRF-Token")).toBe("csrf");

    await act(async () => finishTest?.(json({ ok: true, found: 4, latest_ids: ["post-1"] })));

    expect(await screen.findByRole("status")).toHaveTextContent("测试成功，获取到 4 条最新发布内容");
    expect(screen.getByRole("button", { name: "测试 已有抖音账号" })).toBeEnabled();
  });

  it("shows a platform login error on the account card and allows retrying", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = (init?.method || "GET").toUpperCase();
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts/1/test" && method === "POST") return json({ detail: { code: "login_required", message: "平台登录态已失效，请重新扫码登录" } }, 409);
      if (path === "/api/accounts") return json([account]);
      if (path === "/api/contents" || path === "/api/runs" || path === "/api/platform-sessions") return json([]);
      if (path === "/api/summary") return json({ accounts: 1, healthy_accounts: 1, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);
    await screen.findByText("已有抖音账号");
    fireEvent.click(screen.getByRole("button", { name: /监控账号/ }));
    fireEvent.click(screen.getByRole("button", { name: "测试 已有抖音账号" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("平台登录态已失效，请重新扫码登录");
    expect(screen.getByRole("button", { name: "测试 已有抖音账号" })).toBeEnabled();
  });

  it("edits supported account fields while keeping platform and source URL read only", async () => {
    let currentAccount = { ...account };
    let finishPatch: ((response: Response) => void) | undefined;
    const pendingPatch = new Promise<Response>(resolve => { finishPatch = resolve; });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = (init?.method || "GET").toUpperCase();
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts/1" && method === "PATCH") return pendingPatch;
      if (path === "/api/accounts") return json([currentAccount]);
      if (path === "/api/contents" || path === "/api/runs" || path === "/api/platform-sessions") return json([]);
      if (path === "/api/summary") return json({ accounts: 1, healthy_accounts: 1, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await screen.findByText("已有抖音账号");
    fireEvent.click(screen.getByRole("button", { name: /监控账号/ }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 已有抖音账号" }));

    expect(screen.getByLabelText("平台（不可修改）")).toHaveValue("抖音");
    expect(screen.getByLabelText("平台（不可修改）")).toHaveAttribute("readonly");
    expect(screen.getByLabelText("公开主页 URL（不可修改）")).toHaveValue(account.source_url);
    expect(screen.getByLabelText("公开主页 URL（不可修改）")).toHaveAttribute("readonly");

    fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: " 更新后的账号 " } });
    fireEvent.change(screen.getByLabelText("轮询间隔（分钟）"), { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    expect(screen.getByRole("button", { name: "正在保存…" })).toBeDisabled();
    const patchCall = fetchMock.mock.calls.find(([input, init]) => requestPath(input) === "/api/accounts/1" && (init?.method || "GET").toUpperCase() === "PATCH");
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({ display_name: "更新后的账号", interval_minutes: 30 });
    expect(new Headers(patchCall?.[1]?.headers).get("X-CSRF-Token")).toBe("csrf");

    currentAccount = { ...currentAccount, display_name: "更新后的账号", interval_minutes: 30 };
    await act(async () => finishPatch?.(json(currentAccount)));

    expect(await screen.findByText("已保存“更新后的账号”的监控设置")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "编辑 更新后的账号" })).toBeEnabled();
  });

  it("keeps account edits open on validation and API errors, then cancels with Escape", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = (init?.method || "GET").toUpperCase();
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts/1" && method === "PATCH") return json({ detail: "名称与现有账号冲突" }, 409);
      if (path === "/api/accounts") return json([account]);
      if (path === "/api/contents" || path === "/api/runs" || path === "/api/platform-sessions") return json([]);
      if (path === "/api/summary") return json({ accounts: 1, healthy_accounts: 1, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await screen.findByText("已有抖音账号");
    fireEvent.click(screen.getByRole("button", { name: /监控账号/ }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 已有抖音账号" }));
    const nameInput = screen.getByLabelText("显示名称");

    fireEvent.change(nameInput, { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("显示名称不能为空");
    expect(fetchMock.mock.calls.filter(([input, init]) => requestPath(input) === "/api/accounts/1" && (init?.method || "GET").toUpperCase() === "PATCH")).toHaveLength(0);

    fireEvent.change(nameInput, { target: { value: "冲突账号" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("名称与现有账号冲突");
    expect(screen.getByLabelText("显示名称")).toHaveValue("冲突账号");

    fireEvent.keyDown(screen.getByLabelText("显示名称"), { key: "Escape" });
    await waitFor(() => expect(screen.getByRole("button", { name: "编辑 已有抖音账号" })).toHaveFocus());
    expect(screen.queryByLabelText("显示名称")).not.toBeInTheDocument();
  });

  it("requires explicit delete confirmation and reports a protected archive soft-delete", async () => {
    let currentAccount = { ...account };
    let finishDelete: ((response: Response) => void) | undefined;
    const pendingDelete = new Promise<Response>(resolve => { finishDelete = resolve; });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = (init?.method || "GET").toUpperCase();
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts/1" && method === "DELETE") return pendingDelete;
      if (path === "/api/accounts") return json([currentAccount]);
      if (path === "/api/contents" || path === "/api/runs" || path === "/api/platform-sessions") return json([]);
      if (path === "/api/summary") return json({ accounts: 1, healthy_accounts: currentAccount.enabled ? 1 : 0, contents: 1, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await screen.findByText("已有抖音账号");
    fireEvent.click(screen.getByRole("button", { name: /监控账号/ }));
    fireEvent.click(screen.getByRole("button", { name: "删除 已有抖音账号" }));

    const confirmation = screen.getByRole("group", { name: "删除“已有抖音账号”？" });
    expect(confirmation).toHaveTextContent("已有内容不会删除");
    expect(fetchMock.mock.calls.filter(([input, init]) => requestPath(input) === "/api/accounts/1" && (init?.method || "GET").toUpperCase() === "DELETE")).toHaveLength(0);
    fireEvent.keyDown(confirmation, { key: "Escape" });
    await waitFor(() => expect(screen.getByRole("button", { name: "删除 已有抖音账号" })).toHaveFocus());

    fireEvent.click(screen.getByRole("button", { name: "删除 已有抖音账号" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    const deletingButton = screen.getByRole("button", { name: "正在处理…" });
    expect(deletingButton).toBeDisabled();
    fireEvent.click(deletingButton);
    const deleteCalls = fetchMock.mock.calls.filter(([input, init]) => requestPath(input) === "/api/accounts/1" && (init?.method || "GET").toUpperCase() === "DELETE");
    expect(deleteCalls).toHaveLength(1);
    expect(new Headers(deleteCalls[0]?.[1]?.headers).get("X-CSRF-Token")).toBe("csrf");

    currentAccount = { ...currentAccount, enabled: false, status: "paused" };
    await act(async () => finishDelete?.(json({ message: "账号已有归档，已停用而未删除" })));

    expect(await screen.findByText("账号已有归档，已停用而未删除")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "已有抖音账号" })).toBeInTheDocument();
    expect(screen.getByText("已暂停")).toBeInTheDocument();
  });

  it("keeps the delete confirmation and focus available when deletion fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = (init?.method || "GET").toUpperCase();
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts/1" && method === "DELETE") return json({ detail: "账号正在被其他任务使用，请稍后重试" }, 409);
      if (path === "/api/accounts") return json([account]);
      if (path === "/api/contents" || path === "/api/runs" || path === "/api/platform-sessions") return json([]);
      if (path === "/api/summary") return json({ accounts: 1, healthy_accounts: 1, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);
    await screen.findByText("已有抖音账号");
    fireEvent.click(screen.getByRole("button", { name: /监控账号/ }));
    fireEvent.click(screen.getByRole("button", { name: "删除 已有抖音账号" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("账号正在被其他任务使用，请稍后重试");
    const confirmation = screen.getByRole("group", { name: "删除“已有抖音账号”？" });
    expect(confirmation).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认删除" })).toBeEnabled();
    expect(confirmation).toContainElement(document.activeElement as HTMLElement);
  });

  it("disables account editing and deletion while that account is polling", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts") return json([{ ...account, status: "polling" }]);
      if (path === "/api/contents" || path === "/api/runs" || path === "/api/platform-sessions") return json([]);
      if (path === "/api/summary") return json({ accounts: 1, healthy_accounts: 0, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);
    await screen.findByText("已有抖音账号");
    fireEvent.click(screen.getByRole("button", { name: /监控账号/ }));

    expect(screen.getByRole("button", { name: "编辑 已有抖音账号" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "删除 已有抖音账号" })).toBeDisabled();
  });

  it("keeps platform sessions in a separate primary view with localized status labels", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts" || path === "/api/contents" || path === "/api/runs") return json([]);
      if (path === "/api/summary") return json({ accounts: 0, healthy_accounts: 0, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      if (path === "/api/platform-sessions") return json([
        { platform: "bilibili", status: "authenticated", updated_at: null, message: null, manual_verification_url: "http://localhost:7900" },
        { platform: "weibo", status: "logged_out", updated_at: null, message: null, manual_verification_url: "http://localhost:7900" },
      ]);
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);
    await screen.findByRole("heading", { name: "归档运行概览" });
    fireEvent.click(screen.getByRole("button", { name: "平台登录" }));

    expect(await screen.findByRole("heading", { name: "平台登录" })).toBeInTheDocument();
    expect(screen.getByText("已登录")).toBeInTheDocument();
    expect(screen.getByText("未登录")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "监控账号" })).not.toBeInTheDocument();
  });

  it("shows the platform QR code inside the WebUI without opening another window", async () => {
    let status = "logged_out";
    let finishQr: ((response: Response) => void) | undefined;
    const pendingQr = new Promise<Response>(resolve => { finishQr = resolve; });
    const openWindow = vi.fn();
    vi.stubGlobal("open", openWindow);
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = (init?.method || "GET").toUpperCase();
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts" || path === "/api/contents" || path === "/api/runs") return json([]);
      if (path === "/api/summary") return json({ accounts: 0, healthy_accounts: 0, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      if (path === "/api/platform-sessions" && method === "GET") return json([
        { platform: "bilibili", status, updated_at: null, message: null, manual_verification_url: "http://127.0.0.1:7900" },
      ]);
      if (path === "/api/platform-sessions/bilibili/login" && method === "POST") {
        status = "starting";
        return json({ platform: "bilibili", status, updated_at: null, message: null, manual_verification_url: "http://127.0.0.1:7900" }, 202);
      }
      if (path === "/api/platform-sessions/bilibili/qr") return pendingQr;
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await screen.findByRole("heading", { name: "归档运行概览" });
    fireEvent.click(screen.getByRole("button", { name: "平台登录" }));
    const loginButton = await screen.findByRole("button", { name: "开始登录 Bilibili" });
    fireEvent.click(loginButton);

    expect(await screen.findByRole("heading", { name: "正在准备 Bilibili 登录二维码" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "正在准备 Bilibili 登录二维码" })).toHaveAttribute("aria-busy", "true");
    expect(openWindow).not.toHaveBeenCalled();
    const loginCall = fetchMock.mock.calls.find(([input]) => requestPath(input) === "/api/platform-sessions/bilibili/login");
    expect(loginCall?.[1]?.method).toBe("POST");
    expect(new Headers(loginCall?.[1]?.headers).get("X-CSRF-Token")).toBe("csrf");

    status = "qr_ready";
    await act(async () => finishQr?.(json({
      platform: "bilibili",
      status,
      updated_at: null,
      message: null,
      manual_verification_url: "http://127.0.0.1:7900",
      image_data_url: "data:image/png;base64,cXI=",
    })));

    expect(await screen.findByRole("img", { name: "Bilibili 登录二维码" })).toHaveAttribute("src", "data:image/png;base64,cXI=");
    expect(screen.getByRole("heading", { name: "使用 Bilibili App 扫码" })).toBeInTheDocument();
    expect(openWindow).not.toHaveBeenCalled();
  });

  it("announces platform login success in the current page", async () => {
    let status = "logged_out";
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = (init?.method || "GET").toUpperCase();
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts" || path === "/api/contents" || path === "/api/runs") return json([]);
      if (path === "/api/summary") return json({ accounts: 0, healthy_accounts: 0, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      if (path === "/api/platform-sessions" && method === "GET") return json([
        { platform: "weibo", status, updated_at: null, message: null, manual_verification_url: "http://127.0.0.1:7900" },
      ]);
      if (path === "/api/platform-sessions/weibo/login" && method === "POST") {
        status = "authenticated";
        return json({ platform: "weibo", status: "starting", updated_at: null, message: null, manual_verification_url: "http://127.0.0.1:7900" }, 202);
      }
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);
    await screen.findByRole("heading", { name: "归档运行概览" });
    fireEvent.click(screen.getByRole("button", { name: "平台登录" }));
    fireEvent.click(await screen.findByRole("button", { name: "开始登录 微博" }));

    expect(await screen.findByRole("heading", { name: "微博 登录成功" })).toBeInTheDocument();
    const success = screen.getByRole("status", { name: "微博 登录成功" });
    expect(success).toHaveTextContent("登录状态已安全保存在服务器");
    expect(success).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "完成" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "重新登录 微博" })).toHaveFocus());
  });

  it("stops platform-session follow-up work when leaving the login view", async () => {
    let finishLogin: ((response: Response) => void) | undefined;
    const pendingLogin = new Promise<Response>(resolve => { finishLogin = resolve; });
    let sessionReads = 0;
    let qrReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = (init?.method || "GET").toUpperCase();
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts" || path === "/api/contents" || path === "/api/runs") return json([]);
      if (path === "/api/summary") return json({ accounts: 0, healthy_accounts: 0, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      if (path === "/api/platform-sessions" && method === "GET") {
        sessionReads += 1;
        return json([{ platform: "xiaohongshu", status: "logged_out", updated_at: null, message: null, manual_verification_url: "http://127.0.0.1:7900" }]);
      }
      if (path === "/api/platform-sessions/xiaohongshu/login" && method === "POST") return pendingLogin;
      if (path === "/api/platform-sessions/xiaohongshu/qr") {
        qrReads += 1;
        return json({ detail: "QR code is not ready" }, 409);
      }
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);
    await screen.findByRole("heading", { name: "归档运行概览" });
    fireEvent.click(screen.getByRole("button", { name: "平台登录" }));
    fireEvent.click(await screen.findByRole("button", { name: "开始登录 小红书" }));
    expect(await screen.findByRole("heading", { name: "正在准备 小红书 登录二维码" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "概览" }));
    expect(await screen.findByRole("heading", { name: "归档运行概览" })).toBeInTheDocument();
    const readsBeforeCompletion = sessionReads;
    await act(async () => finishLogin?.(json({
      platform: "xiaohongshu",
      status: "starting",
      updated_at: null,
      message: null,
      manual_verification_url: "http://127.0.0.1:7900",
    }, 202)));
    await act(async () => { await Promise.resolve(); });

    expect(sessionReads).toBe(readsBeforeCompletion);
    expect(qrReads).toBe(0);
    expect(screen.queryByRole("heading", { name: /小红书 登录二维码/ })).not.toBeInTheDocument();
  });

  it("removes a stale QR code when the backend login session ends", async () => {
    let status = "logged_out";
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = (init?.method || "GET").toUpperCase();
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts" || path === "/api/contents" || path === "/api/runs") return json([]);
      if (path === "/api/summary") return json({ accounts: 0, healthy_accounts: 0, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      if (path === "/api/platform-sessions" && method === "GET") return json([
        { platform: "douyin", status, updated_at: null, message: null, manual_verification_url: "http://127.0.0.1:7900" },
      ]);
      if (path === "/api/platform-sessions/douyin/login" && method === "POST") {
        status = "starting";
        return json({ platform: "douyin", status, updated_at: null, message: null, manual_verification_url: "http://127.0.0.1:7900" }, 202);
      }
      if (path === "/api/platform-sessions/douyin/qr") {
        status = "qr_ready";
        return json({ platform: "douyin", status, updated_at: null, message: null, manual_verification_url: "http://127.0.0.1:7900", image_data_url: "data:image/png;base64,cXI=" });
      }
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);
    await screen.findByRole("heading", { name: "归档运行概览" });
    fireEvent.click(screen.getByRole("button", { name: "平台登录" }));
    fireEvent.click(await screen.findByRole("button", { name: "开始登录 抖音" }));
    expect(await screen.findByRole("img", { name: "抖音 登录二维码" })).toBeInTheDocument();

    status = "logged_out";
    await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 1600)); });

    const alert = await screen.findByRole("alert", { name: "抖音 登录未完成" });
    expect(alert).toHaveTextContent("登录会话已结束，请重新获取二维码");
    expect(screen.queryByRole("img", { name: "抖音 登录二维码" })).not.toBeInTheDocument();
  });

  it("keeps platform login errors beside the inline QR flow and allows retrying", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = (init?.method || "GET").toUpperCase();
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts" || path === "/api/contents" || path === "/api/runs") return json([]);
      if (path === "/api/summary") return json({ accounts: 0, healthy_accounts: 0, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      if (path === "/api/platform-sessions" && method === "GET") return json([
        { platform: "douyin", status: "logged_out", updated_at: null, message: null, manual_verification_url: "http://127.0.0.1:7900" },
      ]);
      if (path === "/api/platform-sessions/douyin/login" && method === "POST") {
        return json({ detail: { code: "provider_error", message: "采集器启动失败，请稍后重试" } }, 502);
      }
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);
    await screen.findByRole("heading", { name: "归档运行概览" });
    fireEvent.click(screen.getByRole("button", { name: "平台登录" }));
    fireEvent.click(await screen.findByRole("button", { name: "开始登录 抖音" }));

    const alert = await screen.findByRole("alert", { name: "抖音 登录未完成" });
    expect(alert).toHaveTextContent("采集器启动失败，请稍后重试");
    expect(screen.getByRole("button", { name: "重新获取二维码" })).toBeEnabled();
  });

  it("moves focus into content details, closes on Escape, and restores the trigger", async () => {
    const content = {
      id: 7,
      account_id: 1,
      platform: "douyin",
      remote_id: "post-7",
      title: "雨后的街角",
      author: "城市漫游指南",
      content_type: "video",
      source_url: "https://example.com/post-7",
      published_at: "2026-07-15T08:00:00Z",
      collected_at: "2026-07-15T08:10:00Z",
      summary: "一条归档内容",
      media_count: 0,
      status: "complete",
      error: null,
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts" || path === "/api/runs") return json([]);
      if (path === "/api/contents") return json([content]);
      if (path === "/api/contents/7") return json({ ...content, markdown: "", metadata: { text: content.summary, media: [] } });
      if (path === "/api/summary") return json({ accounts: 0, healthy_accounts: 0, contents: 1, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);
    await screen.findByRole("heading", { name: "归档运行概览" });
    fireEvent.click(screen.getByRole("button", { name: "内容归档" }));
    const trigger = await screen.findByRole("button", { name: "查看归档：雨后的街角" });
    trigger.focus();
    fireEvent.click(trigger);

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "关闭详情" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("surfaces a durable monitoring-gap warning on the affected account", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts") return json([{ ...account, completeness_status: "gap_detected", gap_detected_at: "2026-07-15T08:00:00Z" }]);
      if (path === "/api/contents" || path === "/api/runs") return json([]);
      if (path === "/api/summary") return json({ accounts: 1, healthy_accounts: 1, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);
    await screen.findByRole("heading", { name: "归档运行概览" });
    fireEvent.click(screen.getByRole("button", { name: "监控账号" }));

    expect(await screen.findByText("监控窗口发现断层")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("不会把监控连续性误报为完整");
  });

  it("explains that incomplete media remains in the durable retry queue", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path === "/api/auth/me") return json({ username: "admin", csrf_token: "csrf" });
      if (path === "/api/accounts") return json([{ ...account, completeness_status: "pending_retry" }]);
      if (path === "/api/contents" || path === "/api/runs") return json([]);
      if (path === "/api/summary") return json({ accounts: 1, healthy_accounts: 0, contents: 0, failed_runs: 1 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);
    await screen.findByRole("heading", { name: "归档运行概览" });
    fireEvent.click(screen.getByRole("button", { name: "监控账号" }));

    expect(await screen.findByText("缺失媒体待重试")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("即使离开发现窗口也会继续重试");
  });

  it("logs in with only the WebUI token and explains how the token is updated", async () => {
    let submitted: Record<string, string> | null = null;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/auth/me") return json({ detail: "未登录" }, 401);
      if (path === "/api/auth/login") {
        submitted = JSON.parse(String(init?.body));
        return json({ username: "admin", csrf_token: "csrf" });
      }
      if (path === "/api/accounts" || path === "/api/contents" || path === "/api/runs") return json([]);
      if (path === "/api/summary") return json({ accounts: 0, healthy_accounts: 0, contents: 0, failed_runs: 0 });
      if (path === "/api/storage") return json(storage);
      return json({ detail: "not found" }, 404);
    }));

    render(<App />);
    const tokenInput = await screen.findByLabelText("WebUI 登录 Token");
    expect(tokenInput).toHaveAttribute("type", "password");
    expect(screen.queryByLabelText("用户名")).not.toBeInTheDocument();
    expect(screen.getByText("data/state/webui-login-token.txt")).toBeInTheDocument();
    fireEvent.change(tokenInput, { target: { value: "configured-webui-token" } });
    fireEvent.click(screen.getByRole("button", { name: "登录控制台" }));

    expect(await screen.findByRole("heading", { name: "归档运行概览" })).toBeInTheDocument();
    expect(submitted).toEqual({ token: "configured-webui-token" });
    expect(screen.queryByRole("button", { name: "修改密码" })).not.toBeInTheDocument();
    expect(screen.getByText("Token 认证 · 配置后重启生效")).toBeInTheDocument();
  });
});
