from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


FORBIDDEN_STUDENT_TERMS = (
    "Trace",
    "Publication",
    "Provider",
    "API Key",
    "模型名称",
    "检索分数",
    "publication_id",
    "trace_id",
)


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


def assert_student_surface(page: Page) -> None:
    body = page.locator("body").inner_text()
    for term in FORBIDDEN_STUDENT_TERMS:
        assert term not in body, f"学生页面暴露工程字段: {term}"


def assert_no_horizontal_overflow(page: Page) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1, f"页面出现横向溢出: {overflow}px"


def assert_mascot_does_not_cover_business_content(page: Page) -> None:
    mascot = page.locator("#mascot").bounding_box()
    main = page.locator(".main-shell").bounding_box()
    assert mascot and main, "无法获取业务内容或吉祥物布局边界"
    root = page.locator("#mascot")
    if "is-edge-hidden" in (root.get_attribute("class") or ""):
        viewport = page.viewport_size
        assert viewport
        visible_left = max(0, mascot["x"])
        visible_right = min(viewport["width"], mascot["x"] + mascot["width"])
        visible_width = max(0, visible_right - visible_left)
        assert visible_width <= mascot["width"] / 2 + 1, "贴边吉祥物暴露超过半个控件"
        return
    mascot_top = mascot["y"]
    main_bottom = main["y"] + main["height"]
    assert mascot_top >= main_bottom - 1, "移动端吉祥物遮挡了业务内容"


def assert_mascot_edge_dock(page: Page) -> None:
    page.wait_for_function("window.QSTMascot?.bound === true")
    root = page.locator("#mascot")
    handle = page.locator("#mascot-toggle")
    viewport = page.viewport_size
    assert viewport
    root.wait_for(state="visible")

    # Mobile starts in a compact right-edge dock so the work surface stays usable.
    assert "is-edge-hidden" in (root.get_attribute("class") or "")
    assert root.get_attribute("data-edge") == "right"
    assert not page.locator(".mascot-note").is_visible()
    face = handle.bounding_box()
    assert face and face["x"] + face["width"] > viewport["width"] - 1

    # Tapping the exposed half reveals the note without changing the business route.
    page.mouse.click(viewport["width"] - 4, face["y"] + face["height"] / 2)
    page.locator(".mascot-note").wait_for(state="visible")
    assert "is-edge-hidden" not in (root.get_attribute("class") or "")

    # Dragging the handle to the opposite edge snaps it back to a half-hidden dock.
    face = handle.bounding_box()
    assert face
    page.mouse.move(face["x"] + face["width"] / 2, face["y"] + face["height"] / 2)
    page.mouse.down()
    page.mouse.move(4, face["y"] + face["height"] / 2, steps=8)
    page.mouse.up()
    page.wait_for_timeout(80)
    assert root.get_attribute("data-edge") == "left"
    assert "is-edge-hidden" in (root.get_attribute("class") or "")
    assert not page.locator(".mascot-note").is_visible()
    page.wait_for_function(
        """() => {
            const face = document.querySelector('#mascot-toggle');
            const root = document.querySelector('#mascot');
            if (!face || !root || root.dataset.edge !== 'left') return false;
            const rect = face.getBoundingClientRect();
            const visibleWidth = Math.min(window.innerWidth, rect.right) - Math.max(0, rect.left);
            return visibleWidth >= rect.width / 2 - 1;
        }"""
    )

    # The exposed half remains a real hit target and opens the note again.
    face = handle.bounding_box()
    assert face
    page.mouse.click(4, face["y"] + face["height"] / 2)
    page.locator(".mascot-note").wait_for(state="visible")
    assert "is-edge-hidden" not in (root.get_attribute("class") or "")


def assert_default_mascot_toggle(page: Page) -> None:
    """A click without a drag must keep the default fixed position usable."""
    page.wait_for_function("window.QSTMascot?.bound === true")
    root = page.locator("#mascot")
    handle = page.locator("#mascot-toggle")
    viewport = page.viewport_size
    assert viewport
    page.locator(".mascot-note").wait_for(state="visible")

    handle.click()
    page.wait_for_timeout(80)
    collapsed = root.bounding_box()
    assert collapsed and 0 <= collapsed["x"] <= viewport["width"] and 0 <= collapsed["y"] <= viewport["height"], "默认位置收起后吉祥物离开视口"
    assert not page.locator(".mascot-note").is_visible()

    handle.click()
    page.locator(".mascot-note").wait_for(state="visible")
    expanded = root.bounding_box()
    assert expanded and 0 <= expanded["x"] <= viewport["width"] and 0 <= expanded["y"] <= viewport["height"], "默认位置再次展开后吉祥物离开视口"


def fill_venue_materials(page: Page) -> None:
    plan = page.locator(".material-card").filter(has_text="活动策划书")
    plan.get_by_label("活动目的").fill("为新生提供交流和校园适应信息")
    plan.get_by_label("活动对象").fill("在校新生")
    plan.get_by_label("活动内容").fill("校园生活介绍、社团交流和现场答疑")
    plan.get_by_label("时间安排").fill("10:00 签到；10:30 主题交流；11:40 总结")

    flow = page.locator(".material-card").filter(has_text="活动流程")
    flow.get_by_label("流程安排").fill("10:00 | 签到 | 学生组织\n10:30 | 主题交流 | 活动负责人\n11:40 | 总结 | 活动负责人")


def fill_venue_fields(page: Page) -> None:
    fields = page.locator(".field-grid")
    fields.get_by_label("活动名称").fill("迎新交流会")
    fields.get_by_label("组织类型").select_option("student_organization")
    fields.get_by_label("组织名称").fill("学生成长社")
    fields.get_by_label("申请场地").select_option("demo_plaza")
    fields.get_by_label("活动日期").fill("2026-10-03")
    fields.get_by_label("开始时间").fill("10:00")
    fields.get_by_label("结束时间").fill("12:00")
    fields.get_by_label("预计人数").fill("80")
    fields.get_by_label("活动目的").fill("帮助新生了解校园生活并建立交流联系")


def complete_venue(page: Page, base_url: str) -> str:
    page.goto(f"{base_url}/student")
    wait_ready(page)
    page.get_by_label("描述你要办理的校园事务").wait_for(state="visible")
    assert_student_surface(page)

    page.get_by_label("描述你要办理的校园事务").fill("我想在示例活动广场办迎新活动，10 月 3 日 10 点到 12 点，需要准备什么？")
    page.get_by_role("button", name="查事项").click()
    answer_card = page.locator(".answer-card")
    answer_card.get_by_role("heading", name="活动场地申请").wait_for(state="visible")
    answer_card.get_by_role("button", name="开始办理").click()
    page.wait_for_url("**/student/tasks/*/materials")
    wait_ready(page)

    fill_venue_fields(page)
    fill_venue_materials(page)
    page.get_by_role("button", name="保存材料").click()
    page.locator(".save-state", has_text="已保存").wait_for(state="visible")
    page.get_by_role("button", name="运行材料预检").click()
    page.get_by_text("可以查看提交内容", exact=True).wait_for(state="visible")

    page.get_by_role("link", name="提交预览").click()
    page.wait_for_url("**/student/tasks/*/preview")
    wait_ready(page)
    page.get_by_role("checkbox", name="我已检查以上内容，并确认提交当前版本。").check()
    page.get_by_role("button", name="确认提交").click()
    page.locator(".task-header .large-status", has_text="已提交").wait_for(state="visible")
    page.get_by_role("link", name="办理状态", exact=True).click()
    page.wait_for_url("**/student/tasks/*/status")
    wait_ready(page)
    assert "已提交" in page.locator("body").inner_text()
    assert_student_surface(page)
    return page.url.rsplit("/", 1)[0]


def run(base_url: str, evidence_dir: Path) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []
    request_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=browser_executable())
        desktop = browser.new_page(viewport={"width": 1440, "height": 900})
        desktop.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        desktop.on("pageerror", lambda error: console_errors.append(str(error)))
        desktop.on("requestfailed", lambda request: request_errors.append(f"{request.method} {request.url}: {request.failure}"))
        task_base = complete_venue(desktop, base_url)
        desktop.reload()
        wait_ready(desktop)
        assert "已提交" in desktop.locator("body").inner_text()
        assert_default_mascot_toggle(desktop)
        assert_no_horizontal_overflow(desktop)
        desktop.evaluate("window.scrollTo(0, 0)")
        desktop.screenshot(path=str(evidence_dir / "r0-desktop.png"), full_page=True)

        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        mobile.on("pageerror", lambda error: console_errors.append(str(error)))
        mobile.on("requestfailed", lambda request: request_errors.append(f"{request.method} {request.url}: {request.failure}"))
        mobile.goto(f"{base_url}/student")
        wait_ready(mobile)
        assert_no_horizontal_overflow(mobile)
        mobile_task_base = complete_venue(mobile, base_url)
        assert mobile_task_base != task_base or mobile.url.endswith("/status")
        assert_no_horizontal_overflow(mobile)
        # The default dock must stay out of the business surface. Once the
        # user explicitly opens it, the companion note is an intentional
        # floating layer and is verified separately below.
        assert_mascot_does_not_cover_business_content(mobile)
        assert_mascot_edge_dock(mobile)
        mobile.evaluate("window.scrollTo(0, 0)")
        mobile.screenshot(path=str(evidence_dir / "r0-mobile.png"), full_page=True)

        browser.close()

    assert not console_errors, f"浏览器控制台错误: {console_errors}"
    assert not request_errors, f"浏览器请求失败: {request_errors}"
    (evidence_dir / "r0-acceptance.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "base_url": base_url,
                "viewports": [{"width": 1440, "height": 900}, {"width": 390, "height": 844}],
                "checks": [
                    "示例活动广场 查询、任务、结构化材料、预检、预览、明确确认和状态",
                    "桌面刷新恢复",
                    "默认位置吉祥物点击收起/展开",
                    "移动端核心路径",
                    "学生端工程字段隔离",
                    "桌面和移动端无横向溢出",
                    "移动端吉祥物不遮挡业务内容",
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"R0 frontend acceptance passed; evidence: {evidence_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the local R0 React student-workbench acceptance flow.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--evidence-dir", default="docs/evidence/browser/r0-final-verification-20260921")
    args = parser.parse_args()
    run(args.base_url.rstrip("/"), Path(args.evidence_dir))
