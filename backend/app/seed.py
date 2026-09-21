from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .db import connect, json_dumps
from .knowledge import build_keyword_index, build_selected_dense_index_if_available


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seed_demo_data() -> None:
    now = now_iso()
    publication_id = "pub-demo-2026-09-15"
    sources = [
        {
            "id": "src-venue-v1",
            "source_key": "virtual-campus-venue-guide",
            "title": "校园活动场地申请指南（演示版）",
            "publisher": "校园服务场景（虚拟责任方）",
            "authority_type": "virtual_design",
            "url": "https://demo.example.invalid/services/venue-application",
            "content": "多功能活动室与示例活动广场的虚拟申请需要填写活动名称、日期、时间段、活动人数、组织者和安全说明。演示规则要求至少提前 3 个工作日提交，开放时段设为工作日 08:00-22:00。出现虚拟档期冲突时，需要选择其他时段并重新确认。",
            "published_at": "2026-09-01T08:00:00+08:00",
            "effective_from": "2026-09-01",
            "effective_to": "2026-12-31",
            "freshness_state": "verified_current",
        },
        {
            "id": "src-course-v1",
            "source_key": "virtual-course-announcement",
            "title": "2026 秋季学期选课安排（演示版）",
            "publisher": "学业服务场景（虚拟责任方）",
            "authority_type": "virtual_design",
            "url": "https://demo.example.invalid/academic/course-selection",
            "content": "选课分为预选窗口和正式选课窗口。预选用于提交意向，正式选课以官方系统显示为准。学生需要登录官方入口查看个人可选课程和课表，公开助手不能读取个人选课结果。",
            "published_at": "2026-08-25T08:00:00+08:00",
            "effective_from": "2026-08-25",
            "effective_to": "2026-10-31",
            "freshness_state": "verified_current",
        },
        {
            "id": "src-workstudy-v1",
            "source_key": "virtual-work-study-guide",
            "title": "勤工助学岗位申请与状态说明（演示版）",
            "publisher": "学生服务场景（虚拟责任方）",
            "authority_type": "virtual_design",
            "url": "https://demo.example.invalid/services/work-study",
            "content": "勤工助学岗位包含岗位时间、工作地点、资格条件、申请截止时间和材料要求。学生可以查看已发布岗位并创建模拟申请；岗位发布方提交岗位后需要经过模拟审核才能展示。",
            "published_at": "2026-09-03T08:00:00+08:00",
            "effective_from": "2026-09-03",
            "effective_to": "2026-12-31",
            "freshness_state": "verified_current",
        },
        {
            "id": "src-leave-v1",
            "source_key": "virtual-undergraduate-leave-guide",
            "title": "本科生请假与销假办理说明（演示版）",
            "publisher": "学业服务场景（虚拟责任方）",
            "authority_type": "virtual_design",
            "url": "https://demo.example.invalid/academic/undergraduate-leave",
            "content": "学生请假分为事假、病假和公假三类虚拟情形。不同情形触发不同的证明材料，公假示例包含线下责任方处理步骤；办理后还需要按流程完成虚拟销假。具体规则仅用于演示条件性材料和状态流转，不代表任何学校当前规定。",
            "published_at": "2026-09-15T08:00:00+08:00",
            "effective_from": "2026-09-15",
            "effective_to": "2026-12-31",
            "freshness_state": "verified_current",
        },
    ]
    items = [
        {
            "id": "item-venue",
            "slug": "venue-application",
            "title": "活动场地申请",
            "domain": "校园活动",
            "summary": "从活动目标、场地和时间出发，完成材料预检、档期规则判断和模拟申请。",
            "audience": "校园学生、学生组织（演示对象）",
            "responsible_party": "校园场地服务（虚拟责任方）",
            "entry_label": "打开场地申请入口",
            "entry_url": "https://demo.example.invalid/services/venue-application",
            "icon": "✦",
            "risk_class": "ordinary",
            "source_revision_id": "src-venue-v1",
            "required_fields": [
                {"key": "event_name", "label": "活动名称", "type": "text", "required": True},
                {"key": "date", "label": "活动日期", "type": "date", "required": True},
                {"key": "start_time", "label": "开始时间", "type": "time", "required": True},
                {"key": "end_time", "label": "结束时间", "type": "time", "required": True},
                {"key": "attendees", "label": "预计人数", "type": "number", "required": True},
                {"key": "organizer", "label": "组织者", "type": "text", "required": True},
            ],
            "materials": ["活动方案", "安全说明", "负责人联系方式"],
            "steps": ["确认场地与时段", "准备活动方案和安全说明", "提交模拟申请", "等待虚拟审核或补正"],
            "time_windows": ["至少提前 3 个工作日", "工作日 08:00—22:00"],
            "aliases": ["示例活动广场", "多功能活动室", "场地", "办活动", "活动室"],
        },
        {
            "id": "item-course",
            "slug": "course-selection",
            "title": "选课公告与课表入口",
            "domain": "学业服务",
            "summary": "解释预选、正式选课和时间窗口，并把个人结果交回官方教务系统核验。",
            "audience": "校园学生（演示对象）",
            "responsible_party": "学业服务（虚拟责任方）",
            "entry_label": "打开教务选课入口",
            "entry_url": "https://demo.example.invalid/academic/course-selection",
            "icon": "◒",
            "risk_class": "time_sensitive",
            "source_revision_id": "src-course-v1",
            "required_fields": [],
            "materials": ["本人账号（仅在官方教务系统使用）"],
            "steps": ["查看当前公告窗口", "进入官方教务入口", "登录后以个人课表和系统提示为准"],
            "time_windows": ["以已发布公告和官方系统当前状态为准"],
            "aliases": ["选课", "预选", "正式选课", "课表", "教务系统"],
        },
        {
            "id": "item-workstudy",
            "slug": "work-study",
            "title": "勤工助学岗位与申请",
            "domain": "学生事务",
            "summary": "按时间和资格查看岗位，进行材料预检并跟踪虚拟申请状态。",
            "audience": "校园学生；岗位发布方（演示角色）",
            "responsible_party": "学生服务（虚拟责任方）",
            "entry_label": "查看勤工助学岗位",
            "entry_url": "https://demo.example.invalid/services/work-study",
            "icon": "✳",
            "risk_class": "ordinary",
            "source_revision_id": "src-workstudy-v1",
            "required_fields": [
                {"key": "job_id", "label": "岗位", "type": "select", "required": True},
                {"key": "availability", "label": "可工作时间", "type": "text", "required": True},
                {"key": "qualification_confirmed", "label": "已阅读资格条件", "type": "checkbox", "required": True},
            ],
            "materials": ["个人基本材料（在官方系统提交）", "可工作时间说明"],
            "steps": ["筛选岗位", "阅读资格条件", "完成材料预检", "提交模拟申请并跟踪状态"],
            "time_windows": ["以岗位页面显示的截止时间为准"],
            "aliases": ["勤工", "勤工助学", "岗位", "兼职", "学生助理"],
        },
        {
            "id": "item-leave",
            "slug": "leave-application",
            "title": "本科生请假与销假",
            "domain": "学业服务",
            "summary": "按请假类型检查对应证明材料，模拟校园责任方处理和返校销假流程。",
            "audience": "校园学生（演示对象）",
            "responsible_party": "学业服务（虚拟责任方）",
            "entry_label": "查看本科生请假入口",
            "entry_url": "https://demo.example.invalid/academic/undergraduate-leave",
            "icon": "◇",
            "risk_class": "time_sensitive",
            "source_revision_id": "src-leave-v1",
            "required_fields": [
                {"key": "leave_type", "label": "请假类型", "type": "select", "required": True, "options": ["personal", "medical", "official"], "provenance": "virtual_design"},
                {"key": "start_date", "label": "开始日期", "type": "date", "required": True, "provenance": "virtual_design"},
                {"key": "end_date", "label": "结束日期", "type": "date", "required": True, "provenance": "virtual_design"},
                {"key": "reason", "label": "请假事由", "type": "text", "required": True, "provenance": "virtual_design"},
            ],
            "materials": ["按请假类型提供对应证明材料"],
            "steps": ["选择请假类型并填写申请", "准备对应证明材料", "等待虚拟责任方处理", "返校后完成虚拟销假"],
            "time_windows": ["事假一般不超过两周（演示提示）", "办理时间以当前官方页面为准"],
            "aliases": ["请假", "病假", "事假", "公假", "销假"],
        },
    ]
    scenarios = [
        {
            "id": "scenario-venue-v1",
            "item_id": "item-venue",
            "version": "v1",
            "name": "活动场地申请模拟",
            "description": "模拟学生从目标、时段、材料到申请状态的完整场地办理链路。",
            "fields": items[0]["required_fields"],
            "rules": [
                {"id": "venue-lead-time", "label": "至少提前 3 个工作日", "kind": "lead_time"},
                {"id": "venue-opening-hours", "label": "工作日 08:00—22:00", "kind": "opening_hours"},
                {"id": "venue-capacity", "label": "预计人数需为正数", "kind": "positive_number", "field": "attendees"},
            ],
            "materials": ["活动方案", "安全说明", "负责人联系方式"],
            "steps": ["字段收集", "材料预检", "预览与确认", "模拟审核与补正"],
        },
        {
            "id": "scenario-course-v1",
            "item_id": "item-course",
            "version": "v1",
            "name": "选课公告变化模拟",
            "description": "模拟公告版本变化、时间窗口提示和个人系统登录边界。",
            "fields": [{"key": "question_scope", "label": "关注窗口", "type": "select", "required": False}],
            "rules": [{"id": "course-official-state", "label": "个人结果必须回官方系统核验", "kind": "official_only"}],
            "materials": ["官方教务系统账号（不提交给庆事通）"],
            "steps": ["读取公告版本", "解释窗口", "跳转官方核验"],
        },
        {
            "id": "scenario-workstudy-v1",
            "item_id": "item-workstudy",
            "version": "v1",
            "name": "勤工助学岗位生命周期模拟",
            "description": "模拟岗位发布、学生申请、补正和完成状态，区分学生与发布方角色。",
            "fields": items[2]["required_fields"],
            "rules": [{"id": "workstudy-qualification", "label": "必须先阅读资格条件", "kind": "checkbox_required", "field": "qualification_confirmed"}],
            "materials": ["个人基本材料（官方系统）", "可工作时间说明"],
            "steps": ["岗位筛选", "资格预检", "模拟申请", "模拟审核/补正"],
        },
        {
            "id": "scenario-leave-v1",
            "item_id": "item-leave",
            "version": "v1",
            "name": "本科生请假与销假模拟",
            "description": "按请假类型触发对应证明材料规则，模拟请假审批和返校销假。",
            "fields": items[3]["required_fields"] + [
                {"key": "evidence_confirmed", "label": "已准备对应证明材料", "type": "checkbox", "required": False, "provenance": "virtual_design"},
            ],
            "rules": [
                {
                    "id": "leave-proof",
                    "label": "请假类型对应证明材料",
                    "kind": "conditional_material",
                    "field": "leave_type",
                    "material_field": "evidence_confirmed",
                    "branches": {
                        "medical": {"name": "医疗或授权证明材料", "detail": "病假示例需要相应证明材料"},
                        "personal": {"name": "事假相关证明材料", "detail": "事假需要提供相应证明材料"},
                        "official": {"name": "公假相关证明材料", "detail": "公假示例由责任方线下处理并需要相应证明材料"},
                    },
                },
            ],
            "materials": [],
            "steps": ["请假类型和时间澄清", "条件性材料预检", "虚拟申请预览", "模拟审批与返校销假"],
        },
    ]
    with connect() as db:
        db.execute(
            """INSERT OR IGNORE INTO publications
            (id, label, app_version, retrieval_version, answer_contract_version, status, created_at, previous_id,
             source_bindings, knowledge_model_version, source_gate_result, quality_gate_result,
             responsibility_gate_result, published_at, rollback_target)
            VALUES (?, ?, ?, ?, ?, 'published', ?, NULL, ?, ?, 'pass', 'pass', 'pass', ?, NULL)""",
            (
                publication_id,
                "庆事通演示发布组合 2026-09-15",
                "0.1.0-demo",
                "retrieval-hybrid-v1",
                "answer-card-v1",
                now,
                json_dumps({item["id"]: item["source_revision_id"] for item in items}),
                "knowledge-model-v1",
                now,
            ),
        )
        for source in sources:
            db.execute(
                """INSERT OR IGNORE INTO source_revisions
                (id, source_key, title, publisher, authority_type, url, content, content_hash, published_at,
                 retrieved_at, effective_from, effective_to, status, freshness_state, supersedes_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, NULL, ?)""",
                (
                    source["id"], source["source_key"], source["title"], source["publisher"], source["authority_type"],
                    source["url"], source["content"], _hash(source["content"]), source["published_at"], now,
                    source["effective_from"], source["effective_to"], source["freshness_state"], now,
                ),
            )
        for item in items:
            db.execute(
                """INSERT OR IGNORE INTO service_items
                (id, slug, title, domain, summary, audience, responsible_party, entry_label, entry_url, icon,
                 risk_class, source_revision_id, required_fields, materials, steps, time_windows, aliases, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'published')""",
                (
                    item["id"], item["slug"], item["title"], item["domain"], item["summary"], item["audience"],
                    item["responsible_party"], item["entry_label"], item["entry_url"], item["icon"], item["risk_class"],
                    item["source_revision_id"], json_dumps(item["required_fields"]), json_dumps(item["materials"]),
                    json_dumps(item["steps"]), json_dumps(item["time_windows"]), json_dumps(item["aliases"]),
                ),
            )
        for scenario in scenarios:
            db.execute(
                """INSERT OR IGNORE INTO scenarios
                (id, item_id, version, name, description, fields, rules, materials, steps, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'published')""",
                (
                    scenario["id"], scenario["item_id"], scenario["version"], scenario["name"], scenario["description"],
                    json_dumps(scenario["fields"]), json_dumps(scenario["rules"]), json_dumps(scenario["materials"]),
                    json_dumps(scenario["steps"]),
                ),
            )
        for item in items:
            db.execute(
                "INSERT OR IGNORE INTO publication_bindings (publication_id, item_id, revision_id) VALUES (?, ?, ?)",
                (publication_id, item["id"], item["source_revision_id"]),
            )
        db.execute(
            """INSERT OR IGNORE INTO workstudy_jobs
            (id, title, department, location, schedule, stipend, qualification, deadline, status,
             created_by, internal_note, version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, ?, 1, ?, ?)""",
            (
                "job-library",
                "资料整理助理（演示）",
                "图书服务（演示）",
                "示例图书空间",
                "周一至周五 17:00-20:00",
                "25 元/小时（演示）",
                "在校本科生，能够稳定安排晚间时间",
                "2026-10-31",
                "demo_provider",
                "种子岗位，仅用于演示学生侧申请；不代表真实招聘信息",
                now,
                now,
            ),
        )
        _seed_bad_case(db, now)

    build_keyword_index(publication_id)
    build_selected_dense_index_if_available(publication_id)


def _seed_bad_case(db, now: str) -> None:
    db.execute(
        """INSERT OR IGNORE INTO bad_cases
        (id, origin, title, input_text, trace_id, category, severity, expected, actual, status, repair_note, created_at, updated_at)
        VALUES (?, 'seeded_red_team', ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?)""",
        (
            "bc-seed-venue-confusion",
            "场地诉求被错误识别为其他服务",
            "我想在示例活动广场办一场迎新活动，应该怎么办？",
            "intent",
            "P1",
            json_dumps({"service_item": "venue-application", "must_show": ["材料", "时间窗口", "模拟边界"]}),
            json_dumps({"historical_failure": "返回了校园卡入口"}),
            "open",
            now,
            now,
        ),
    )
