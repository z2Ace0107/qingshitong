from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT = ROOT / "docs" / "evidence" / "browser" / "agent-config.png"


def browser_executable() -> str | None:
    return next(
        (
            candidate
            for candidate in (
                os.environ.get("QST_BROWSER_EXECUTABLE"),
                shutil.which("chrome.exe"),
                shutil.which("msedge.exe"),
            )
            if candidate and Path(candidate).exists()
        ),
        None,
    )


def main() -> None:
    requests: list[dict] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=browser_executable())
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        def intercept_query(route):
            requests.append(json.loads(route.request.post_data or "{}"))
            route.continue_()

        page.route("**/api/query/stream", intercept_query)
        page.goto("http://127.0.0.1:8000/student", wait_until="networkidle")
        page.get_by_label("描述你要办理的校园事务").wait_for()

        # R0 intentionally removed the old browser-side Provider configuration.
        # This acceptance keeps the negative boundary executable after frontend migration.
        for selector in ("#agent-settings", "#agent-api-key", "#agent-base-url", "#agent-model", "#agent-apply"):
            assert page.locator(selector).count() == 0, f"学生端重新暴露了服务端配置入口: {selector}"

        page.get_by_label("描述你要办理的校园事务").fill("我想查勤工助学岗位")
        page.get_by_role("button", name="查事项").click()
        page.locator(".answer-card").wait_for()

        assert requests
        forbidden = {"api_key", "provider", "model", "base_url", "api_format", "retrieval_strategy", "actor_type"}
        assert all(forbidden.isdisjoint(item) for item in requests)

        page.screenshot(path=str(SCREENSHOT), full_page=True)
        browser.close()

    print(json.dumps({"status": "passed", "checked": "student_provider_boundary", "requests": requests, "screenshot": str(SCREENSHOT)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
