from __future__ import annotations

import argparse
import json
import os
import sqlite3
import shutil
from pathlib import Path

from playwright.sync_api import APIRequestContext, Page, sync_playwright


def browser_executable() -> str | None:
    candidates = (
        os.environ.get("QST_BROWSER_EXECUTABLE"),
        shutil.which("chrome.exe"),
        shutil.which("msedge.exe"),
    )
    return next((candidate for candidate in candidates if candidate and Path(candidate).exists()), None)


def response_json(response, expected: int = 200) -> dict:
    assert response.status == expected, f"请求失败 {response.status}: {response.text()}"
    return response.json()


def sign_in(api: APIRequestContext, base_url: str, token: str) -> str:
    payload = response_json(api.post(f"{base_url}/api/governance/session", headers={"X-QST-Governance-Token": token}))
    return payload["session"]["csrf_token"]


def isolate_seed_bad_case_for_release_gate() -> None:
    """Prepare the non-replayable seed case before exercising the close API."""
    database_path = os.environ.get("QST_DB_PATH")
    assert database_path, "浏览器验收必须使用显式临时数据库"
    assert Path(database_path).resolve().parent.name.startswith("qst-browser-"), "浏览器验收拒绝修改非临时数据库"
    with sqlite3.connect(database_path) as database:
        database.execute(
            "UPDATE bad_cases SET status = 'verified', updated_at = datetime('now') "
            "WHERE id = 'bc-seed-venue-confusion' AND status = 'open'"
        )


def prepare_impacted_task(api: APIRequestContext, base_url: str) -> str:
    task = response_json(api.post(f"{base_url}/api/student/tasks", data={"scenario_id": "scenario-venue-v1"}))
    task_id = task["task_ref"]["task_id"]

    evaluation_token = os.environ["QST_BROWSER_EVALUATION_TOKEN"]
    knowledge_token = os.environ["QST_BROWSER_KNOWLEDGE_TOKEN"]
    release_token = os.environ["QST_BROWSER_RELEASE_TOKEN"]

    evaluation_csrf = sign_in(api, base_url, evaluation_token)
    changed = response_json(
        api.post(
            f"{base_url}/api/demo/announcement-change",
            headers={"X-CSRF-Token": evaluation_csrf},
            data={"source_id": "src-venue-v1", "idempotency_key": "browser-reconfirm-change"},
        )
    )

    knowledge_csrf = sign_in(api, base_url, knowledge_token)
    revision_id = changed["candidate_revision_id"]
    activated = response_json(
        api.post(
            f"{base_url}/api/source-revisions/{revision_id}/review",
            headers={"X-CSRF-Token": knowledge_csrf, "Idempotency-Key": f"browser-source-review-{revision_id}"},
            data={"decision": "approve", "review_note": "浏览器验收夹具已核对", "expected_version": 1},
        )
    )
    publication_id = activated["publication_id"]
    reviewed = response_json(
        api.post(
            f"{base_url}/api/publications/{publication_id}/business-review",
            headers={"X-CSRF-Token": knowledge_csrf, "Idempotency-Key": f"browser-business-review-{publication_id}"},
            data={"decision": "approve", "review_note": "浏览器验收夹具已核对", "expected_version": activated["version"]},
        )
    )

    evaluation_csrf = sign_in(api, base_url, evaluation_token)
    isolate_seed_bad_case_for_release_gate()
    response_json(
        api.post(
            f"{base_url}/api/bad-cases/bc-seed-venue-confusion/close",
            headers={"X-CSRF-Token": evaluation_csrf},
            data={"close_note": "浏览器验收夹具已隔离种子案例"},
        )
    )
    response_json(
        api.post(
            f"{base_url}/api/evaluations/run",
            headers={"X-CSRF-Token": evaluation_csrf},
            data={"publication_id": publication_id, "run_mode": "release"},
        )
    )

    release_csrf = sign_in(api, base_url, release_token)
    response_json(
        api.post(
            f"{base_url}/api/publications/{publication_id}/activate",
            headers={"X-CSRF-Token": release_csrf, "Idempotency-Key": f"browser-publication-activate-{publication_id}"},
            data={"release_note": "浏览器验收切换依据", "expected_version": reviewed["version"]},
        )
    )
    return task_id


def wait_ready(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.locator("body").wait_for(state="visible")


def assert_page(page: Page) -> None:
    text = page.locator("body").inner_text()
    assert "办理依据发生变化" in text, f"页面未渲染依据变化内容：{text[:1200]}"
    assert "原办理依据" in text, f"页面未渲染原办理依据：{text[:1200]}"
    assert "当前办理依据" in text, f"页面未渲染当前办理依据：{text[:1200]}"
    for forbidden in ("impact_event_id", "publication_id", "source_revision_id", "Trace"):
        assert forbidden not in text
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 1


def run(base_url: str, evidence_dir: Path) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []
    request_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=browser_executable())
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: console_errors.append(str(error)))
        page.on("requestfailed", lambda request: request_errors.append(f"{request.method} {request.url}: {request.failure}"))
        task_id = prepare_impacted_task(context.request, base_url)
        page.goto(f"{base_url}/student/tasks/{task_id}/status")
        wait_ready(page)
        page.get_by_role("link", name="重新检查办理依据").click()
        page.wait_for_url("**/reconfirmation")
        wait_ready(page)
        page.locator("h2", has_text="办理依据发生变化").wait_for(state="visible")
        assert_page(page)
        page.screenshot(path=str(evidence_dir / "reconfirmation-desktop.png"), full_page=True)

        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile.goto(f"{base_url}/student/tasks/{task_id}/reconfirmation")
        wait_ready(mobile)
        mobile.locator("h2", has_text="办理依据发生变化").wait_for(state="visible")
        assert_page(mobile)
        mobile.screenshot(path=str(evidence_dir / "reconfirmation-mobile.png"), full_page=True)
        mobile.get_by_role("checkbox", name="我已阅读当前办理依据，并确认继续检查材料。").check()
        mobile.get_by_role("button", name="重新确认并继续").click()
        mobile.wait_for_url("**/materials")
        assert "材料填写" in mobile.locator("body").inner_text()
        browser.close()

    assert not console_errors, f"浏览器控制台错误: {console_errors}"
    assert not request_errors, f"浏览器请求失败: {request_errors}"
    (evidence_dir / "reconfirmation-acceptance.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "base_url": base_url,
                "viewports": [{"width": 1440, "height": 900}, {"width": 390, "height": 844}],
                "checks": [
                    "依据变化后的旧/新依据业务投影",
                    "学生端内部字段隔离",
                    "桌面和移动端无横向溢出",
                    "明确确认后回到材料工作区",
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Reconfirmation browser acceptance passed; evidence: {evidence_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the R0 student reconfirmation browser acceptance flow.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8002")
    parser.add_argument("--evidence-dir", default="docs/evidence/browser/r0-20260919-reconfirmation")
    args = parser.parse_args()
    run(args.base_url.rstrip("/"), Path(args.evidence_dir))
