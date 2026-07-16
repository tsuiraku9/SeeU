from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def load_local_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def resolve_login_token(env: dict[str, str]) -> str:
    configured_token = env.get("WEBUI_LOGIN_TOKEN", "").strip()
    if configured_token:
        return configured_token
    generated_token_path = ROOT / "data" / "state" / "webui-login-token.txt"
    try:
        generated_token = generated_token_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(
            "WebUI login token is unavailable; configure WEBUI_LOGIN_TOKEN or start the service first"
        ) from error
    if not generated_token:
        raise RuntimeError(
            "WebUI login token is unavailable; configure WEBUI_LOGIN_TOKEN or start the service first"
        )
    return generated_token


def resolve_webui_url(env: dict[str, str]) -> str:
    raw_port = (env.get("WEBUI_PORT") or env.get("APP_PORT") or "8080").strip()
    try:
        port = int(raw_port)
    except ValueError as error:
        raise RuntimeError("WEBUI_PORT must be a valid TCP port") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("WEBUI_PORT must be a valid TCP port")
    host = (env.get("APP_BIND_ADDRESS") or "127.0.0.1").strip()
    url_host = f"[{host}]" if ":" in host else host
    return f"http://{url_host}:{port}"


def response_payload(response) -> object:  # type: ignore[no-untyped-def]
    try:
        return response.json()
    except Exception:
        return {"text": response.text()[:500]}


def main() -> None:
    env = load_local_env()
    results: list[dict[str, object]] = []
    browser_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.goto(resolve_webui_url(env), wait_until="networkidle")

        login_token = page.get_by_label("WebUI 登录 Token", exact=True)
        if login_token.is_visible():
            login_token.fill(resolve_login_token(env))
            page.locator("form.login-form").get_by_role("button").click()
            page.locator(".app-shell").wait_for(state="visible")

        page.get_by_role("button", name=re.compile("监控账号")).click()
        page.locator(".account-list").wait_for(state="visible")
        page.locator(".account-card").first.wait_for(state="visible")

        cards = page.locator(".account-card")
        for index in range(cards.count()):
            card = cards.nth(index)
            title = card.locator("h2").inner_text()
            source_url = card.locator("a").inner_text()
            try:
                with page.expect_response(
                    lambda response: response.request.method == "POST"
                    and re.search(r"/api/accounts/\d+/test$", response.url) is not None,
                    timeout=210_000,
                ) as response_info:
                    card.get_by_role("button", name="测试", exact=True).click()
                response = response_info.value
                payload = response_payload(response)
                card.locator(".account-test-feedback").wait_for(state="visible", timeout=10_000)
                results.append(
                    {
                        "title": title,
                        "source_url": source_url,
                        "status": response.status,
                        "payload": payload,
                        "feedback": card.locator(".account-test-feedback").inner_text(),
                    }
                )
            except PlaywrightTimeoutError:
                results.append({"title": title, "source_url": source_url, "status": 0, "payload": {"error": "timeout"}})
            page.wait_for_timeout(500)

        alerts = page.locator('[role="alert"]').all_text_contents()
        page.screenshot(path=str(ROOT / "data" / "account-tests-ui-verification.png"), full_page=True)
        browser.close()

    print(
        json.dumps(
            {"results": results, "alerts": alerts, "browser_errors": browser_errors},
            ensure_ascii=False,
        )
    )
    successful = all(
        item.get("status") == 200
        and isinstance(item.get("payload"), dict)
        and int(item["payload"].get("found", 0)) > 0  # type: ignore[union-attr]
        and "测试成功" in str(item.get("feedback", ""))
        for item in results
    )
    if len(results) < 1 or not successful or browser_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
