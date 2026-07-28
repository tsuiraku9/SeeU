import os
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


base_url = os.getenv("E2E_BASE_URL", "http://127.0.0.1:8099")
screenshot_path = os.getenv("E2E_SCREENSHOT_PATH", ".e2e/browser/e2e-ui.png")
login_token = os.getenv("E2E_WEBUI_LOGIN_TOKEN") or os.getenv(
    "WEBUI_LOGIN_TOKEN", "test-webui-login-token-long-enough"
)
errors: list[str] = []
api_paths: list[str] = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" and "401" not in message.text else None)
    page.on("response", lambda response: api_paths.append(urlparse(response.url).path) if urlparse(response.url).path.startswith("/api/") else None)
    page.goto(base_url, wait_until="networkidle")
    assert page.url.rstrip("/") == base_url.rstrip("/")
    assert page.title() == "我会一直看着你"
    token_input = page.get_by_label("WebUI 登录 Token", exact=True)
    assert token_input.count() == 1
    assert page.get_by_role("heading", name="欢迎回来", exact=True).count() == 1
    token_input.fill(login_token)
    page.locator("form").get_by_role("button").click()
    page.get_by_role("heading", name="归档运行概览").wait_for()
    page.wait_for_load_state("networkidle")
    assert page.locator(".stats-grid").is_visible()
    assert "/api/contents" not in api_paths
    assert "/api/runs" not in api_paths
    page.get_by_role("button", name="内容归档", exact=True).click()
    page.get_by_role("heading", name="统一信息流").wait_for()
    page.get_by_role("button", name="监控账号", exact=True).click()
    page.get_by_role("heading", name="监控账号").wait_for()
    page.get_by_role("button", name="任务记录", exact=True).click()
    page.get_by_role("heading", name="任务记录").wait_for()
    page.get_by_role("button", name="系统设置", exact=True).click()
    page.get_by_role("heading", name="系统设置").wait_for()
    page.get_by_text("SQLite 日志模式", exact=True).wait_for()
    refresh = page.get_by_label("自动刷新")
    refresh.select_option("120")
    assert refresh.input_value() == "120"
    assert page.evaluate("JSON.parse(localStorage.getItem('seeu-ui-preferences-v1')).refreshSeconds") == 120
    if screenshot_path:
        page.screenshot(path=screenshot_path, full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator(".mobile-top").wait_for()
    assert page.get_by_role("navigation", name="移动端主导航").is_visible()
    assert page.get_by_role("button", name="退出", exact=True).is_visible()
    assert page.get_by_role("button", name="改密").count() == 0
    browser.close()

assert not errors, f"Browser console errors: {errors}"
print("E2E UI lazy loading, settings, desktop layout, and mobile navigation passed")
