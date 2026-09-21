"""Exercise focused R0 keyboard, reduced-motion, and recovery checks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def browser_executable() -> str | None:
    candidates = (
        os.environ.get("QST_BROWSER_EXECUTABLE"),
        shutil.which("chrome.exe"),
        shutil.which("msedge.exe"),
    )
    return next((candidate for candidate in candidates if candidate and Path(candidate).exists()), None)


def wait_ready(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.locator("body").wait_for(state="visible")


def assert_no_horizontal_overflow(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def assert_keyboard_and_reduced_motion(page: Page, base_url: str) -> None:
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{base_url}/student")
    wait_ready(page)
    query = page.get_by_role("textbox", name="描述你要办理的校园事务")
    query.focus()
    assert page.evaluate("document.activeElement === document.querySelector('#query-input')")
    query.fill("我想在示例活动广场办迎新活动，应该怎么办？")
    query.press("Enter")
    page.locator(".answer-card").wait_for(state="visible", timeout=10_000)
    assert page.locator(".answer-card").get_by_role("heading", name="活动场地申请").is_visible()
    animation_duration = page.locator(".mascot-asset-image").evaluate("el => getComputedStyle(el).animationDuration")
    transition_duration = page.locator(".mascot-face").evaluate("el => getComputedStyle(el).transitionDuration")
    assert animation_duration in {"0s", "0.001ms", "1e-06s"}, animation_duration
    assert transition_duration in {"0s", "0.001ms", "1e-06s"}, transition_duration
    assert_no_horizontal_overflow(page)


def start_venue_task(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/student")
    wait_ready(page)
    page.get_by_role("textbox", name="描述你要办理的校园事务").fill("我想在示例活动广场办迎新活动，应该怎么办？")
    page.get_by_role("button", name="查事项").click()
    card = page.locator(".answer-card")
    card.get_by_role("heading", name="活动场地申请").wait_for(state="visible")
    card.get_by_role("button", name="开始办理").click()
    page.wait_for_url("**/student/tasks/*/materials")
    wait_ready(page)


def assert_long_material_and_error_recovery(page: Page, base_url: str) -> None:
    start_venue_task(page, base_url)
    activity_plan = page.locator(".material-card").filter(has_text="活动策划书")
    activity_plan.get_by_label("活动目的").fill("为新生提供交流和校园适应信息")
    activity_plan.get_by_label("活动对象").fill("在校新生")
    activity_plan.get_by_label("活动内容").fill("内容" * 1200)
    activity_plan.get_by_label("时间安排").fill("10:00 签到；10:30 主题交流；11:40 总结")
    assert_no_horizontal_overflow(page)

    page.route(
        "**/api/student/tasks/*/materials",
        lambda route: route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps({"detail": {"code": "version_conflict", "message": "办理内容已更新，请重新读取后再保存。"}}, ensure_ascii=False),
        ),
    )
    page.get_by_role("button", name="保存材料").click()
    page.locator(".save-state", has_text="保存失败").wait_for(state="visible")
    assert activity_plan.get_by_label("活动内容").input_value() == "内容" * 1200
    assert_no_horizontal_overflow(page)


def run(base_url: str, evidence_dir: Path) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=browser_executable())
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        assert_keyboard_and_reduced_motion(page, base_url)
        assert_long_material_and_error_recovery(page, base_url)
        page.screenshot(path=str(evidence_dir / "frontend-accessibility-desktop.png"), full_page=True)
        browser.close()
    (evidence_dir / "frontend-accessibility-acceptance.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "base_url": base_url,
                "checks": [
                    "键盘聚焦输入并提交事项",
                    "prefers-reduced-motion 下动效和过渡受限",
                    "长材料不产生横向溢出",
                    "保存版本冲突保留编辑内容并展示恢复提示",
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Frontend accessibility acceptance passed; evidence: {evidence_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run focused R0 frontend accessibility and recovery checks.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--evidence-dir", default="docs/evidence/browser/r0-final-verification-20260921/accessibility")
    args = parser.parse_args()
    run(args.base_url.rstrip("/"), ROOT / args.evidence_dir)
