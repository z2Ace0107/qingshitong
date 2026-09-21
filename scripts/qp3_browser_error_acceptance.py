"""Exercise the R0 student stream error and cancellation matrix with local fixtures."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("trace_id", "publication_id", "Provider", "API Key", "event_id")


def browser_executable() -> str | None:
    candidates = (
        os.environ.get("QST_BROWSER_EXECUTABLE"),
        shutil.which("chrome.exe"),
        shutil.which("msedge.exe"),
    )
    return next((candidate for candidate in candidates if candidate and Path(candidate).exists()), None)


def stream_event(event_type: str, sequence: int, terminal: bool, payload: object, run_id: str = "run-browser-fixture") -> str:
    envelope = {
        "schema_version": "qst.stream.v1",
        "event_id": f"event-{sequence}",
        "run_id": run_id,
        "sequence": sequence,
        "type": event_type,
        "terminal": terminal,
        "payload": payload,
    }
    return f"event: {event_type}\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n"


def wait_ready(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.locator("body").wait_for(state="visible")
    body = page.locator("body").inner_text()
    assert not any(term in body for term in FORBIDDEN)


def assert_error_case(page: Page, base_url: str, body: str, expected_message: str) -> None:
    def handler(route):
        route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body)

    page.route("**/api/query/stream", handler)
    page.goto(f"{base_url}/student")
    wait_ready(page)
    page.get_by_role("textbox", name="描述你要办理的校园事务").fill("我想在示例活动广场办迎新活动，应该怎么办？")
    page.get_by_role("button", name="查事项").click()
    page.locator(".notice.warning").get_by_text(expected_message, exact=True).wait_for(state="visible")
    assert expected_message in page.locator(".notice.warning").inner_text()
    assert not any(term in page.locator("body").inner_text() for term in FORBIDDEN)
    page.unroute("**/api/query/stream", handler)


def assert_cancel_case(page: Page, base_url: str) -> None:
    page.add_init_script(
        """
        (() => {
          const originalFetch = window.fetch.bind(window);
          const started = "event: run.started\\ndata: " + JSON.stringify({schema_version:"qst.stream.v1",event_id:"event-1",run_id:"run-browser-cancel",sequence:1,type:"run.started",terminal:false,payload:{message:"正在准备查询"}}) + "\\n\\n";
          window.fetch = async (input, init = {}) => {
            const url = typeof input === "string" ? input : input.url;
            if (!url.includes("/api/query/stream")) return originalFetch(input, init);
            const encoder = new TextEncoder();
            const body = new ReadableStream({
              start(controller) {
                controller.enqueue(encoder.encode(started));
                init.signal?.addEventListener("abort", () => controller.error(new DOMException("Aborted", "AbortError")), {once: true});
              },
            });
            return new Response(body, {status: 200, headers: {"Content-Type": "text/event-stream"}});
          };
        })();
        """
    )
    page.route(
        "**/api/runs/run-browser-cancel/cancel",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({"run_id": "run-browser-cancel", "status": "cancel_requested", "terminal": False})),
    )
    page.goto(f"{base_url}/student")
    wait_ready(page)
    page.get_by_role("textbox", name="描述你要办理的校园事务").fill("我想在示例活动广场办迎新活动，应该怎么办？")
    page.get_by_role("button", name="查事项").click()
    page.get_by_role("button", name="取消查询").wait_for(state="visible")
    page.get_by_role("button", name="取消查询").click()
    page.get_by_text("本次查询已结束，可以重新提交", exact=True).wait_for(state="visible")
    assert page.get_by_role("button", name="重新查询").is_visible()
    assert not any(term in page.locator("body").inner_text() for term in FORBIDDEN)


def run(base_url: str, evidence_dir: Path) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    cases = {
        "failed": stream_event("run.started", 1, False, {"message": "正在准备查询"})
        + stream_event("run.failed", 2, True, {"message": "暂时无法完成查询", "recovery_action": "retry_later"}),
        "truncated": stream_event("run.started", 1, False, {"message": "正在准备查询"})
        + stream_event("progress", 2, False, {"message": "处理中"})
        + 'data: {"partial":true}\n',
        "oversized_event": stream_event("run.started", 1, False, {"message": "正在准备查询"})
        + stream_event("progress", 2, False, {"message": "x" * (64 * 1024)}),
    }
    results: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=browser_executable())
        for viewport in ((1440, 900), (390, 844)):
            for name, body in cases.items():
                page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
                assert_error_case(
                    page,
                    base_url,
                    body,
                    {
                        "failed": "暂时无法完成查询，请稍后重试。",
                        "truncated": "查询连接中断，请重新提交。",
                        "oversized_event": "查询结果过大，请稍后重试。",
                    }[name],
                )
                results.append({"case": name, "viewport": {"width": viewport[0], "height": viewport[1]}, "status": "passed"})
                page.close()
            page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
            assert_cancel_case(page, base_url)
            results.append({"case": "cancel", "viewport": {"width": viewport[0], "height": viewport[1]}, "status": "passed"})
            page.close()
        browser.close()
    (evidence_dir / "qp3-error-acceptance.json").write_text(
        json.dumps({"status": "passed", "base_url": base_url, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"QP3 browser error acceptance passed; evidence: {evidence_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the local QP3 stream error and cancellation matrix.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--evidence-dir", default="docs/evidence/browser/r0-final-verification-20260921/qp3-errors")
    args = parser.parse_args()
    run(args.base_url.rstrip("/"), ROOT / args.evidence_dir)
