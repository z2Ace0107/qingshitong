from __future__ import annotations

from pathlib import Path
import os
import shutil

from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "browser" / "r0-final-verification-20260921" / "qp3"
BASE_URL = "http://127.0.0.1:8000/"


def assert_student_surface(page: Page) -> None:
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")
    page.locator("h1").wait_for()
    assert "今天想把哪件事办清楚" in page.locator("h1").inner_text()
    assert page.get_by_role("textbox", name="描述你要办理的校园事务").is_visible()
    assert page.locator("body").evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    body_text = page.locator("body").inner_text().lower()
    for forbidden in ("trace_id", "publication_id", "api key", "provider", "event_id"):
        assert forbidden not in body_text


def assert_stream_query(page: Page) -> None:
    query = page.get_by_role("textbox", name="描述你要办理的校园事务")
    query.fill("我想在示例活动广场办迎新活动，应该怎么办？")
    page.get_by_role("button", name="查事项").click()
    page.locator(".answer-card").wait_for(timeout=10_000)
    answer_text = page.locator(".answer-card").inner_text()
    assert "活动场地申请" in answer_text
    assert "服务边界" in answer_text
    assert "事项结论" in answer_text
    assert page.locator("body").evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def assert_mobile_surface(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="病假材料").click()
    page.locator(".answer-card").wait_for(timeout=10_000)
    assert "本科生请假与销假" in page.locator(".answer-card").inner_text()
    assert page.locator("body").evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(EVIDENCE / "qp3-mobile-20260919.png"), full_page=True)


def main() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        executable = next(
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
        browser = playwright.chromium.launch(headless=True, executable_path=executable)
        desktop = browser.new_page(viewport={"width": 1440, "height": 900})
        assert_student_surface(desktop)
        assert_stream_query(desktop)
        desktop.screenshot(path=str(EVIDENCE / "qp3-desktop-20260919.png"), full_page=True)
        assert_mobile_surface(desktop)
        browser.close()
    print(f"QP3 browser acceptance passed; screenshots: {EVIDENCE}")


if __name__ == "__main__":
    main()
