import os

from playwright.sync_api import sync_playwright


base_url = os.getenv("E2E_BASE_URL", "http://127.0.0.1:8099")
screenshot_path = os.getenv("E2E_SCREENSHOT_PATH", "data/browser/e2e-ui.png")
login_token = os.getenv("E2E_WEBUI_LOGIN_TOKEN") or os.getenv(
    "WEBUI_LOGIN_TOKEN", "test-webui-login-token-long-enough"
)
errors: list[str] = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" and "401" not in message.text else None)
    page.goto(base_url, wait_until="networkidle")
    assert page.url.rstrip("/") == base_url.rstrip("/")
    assert page.title() == "我会一直看着你"
    token_input = page.get_by_label("WebUI 登录 Token", exact=True)
    assert token_input.count() == 1
    assert page.get_by_role("heading", name="欢迎回来", exact=True).count() == 1
    token_input.fill(login_token)
    page.locator("form").get_by_role("button").click()
    page.get_by_role("heading", name="归档运行概览").wait_for()
    assert page.locator(".stats-grid").is_visible()
    page.get_by_role("button", name="内容归档", exact=True).click()
    page.get_by_role("heading", name="统一信息流").wait_for()
    page.get_by_role("button", name="监控账号", exact=True).click()
    page.get_by_role("heading", name="监控账号").wait_for()
    page.get_by_role("button", name="任务记录", exact=True).click()
    page.get_by_role("heading", name="任务记录").wait_for()
    if screenshot_path:
        page.screenshot(path=screenshot_path, full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator(".mobile-top").wait_for()
    assert page.get_by_role("navigation", name="移动端主导航").is_visible()
    assert page.get_by_role("button", name="退出", exact=True).is_visible()
    assert page.get_by_role("button", name="改密").count() == 0
    browser.close()

assert not errors, f"Browser console errors: {errors}"
print("E2E UI token login, authenticated navigation, desktop layout, and mobile navigation passed")
