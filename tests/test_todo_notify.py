"""参数验证待办汇总（tools/todo_notify.py）纯函数自检：不依赖数据库/网络。"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("todo_notify", ROOT / "tools" / "todo_notify.py")
todo = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(todo)

failures = 0


def check(condition, name, detail=""):
    global failures
    if condition:
        print(f"  [ OK ] {name}")
    else:
        failures += 1
        print(f"  [FAIL] {name} - {detail}")


def make_item(key, *, waiting=1, state="pending", total=None, passed=0, rejected=0,
              region="独联体地区", vehicle="E5-001", date="2026-09-29", name=None):
    return {
        "key": key,
        "name": name or key.rsplit("/", 1)[-1],
        "data_root": key,
        "region": region,
        "vehicle": vehicle,
        "date": date,
        "updated_at": "2026-10-23 09:00:00",
        "state": state,
        "total": total if total is not None else waiting + passed + rejected,
        "passed": passed,
        "rejected": rejected,
        "waiting": waiting,
    }


def test_find_new():
    state = {"seen": {"a": {"waiting": 2}, "b": {"waiting": 3}}}
    pending = [
        make_item("a", waiting=2),   # 没有变化
        make_item("b", waiting=4),   # 待确认变多
        make_item("c", waiting=1),   # 新数据集
    ]
    got = {item["key"] for item in todo.find_new(pending, state)}
    check(got == {"b", "c"}, "新增待办=新数据集或待确认变多", str(got))
    check(todo.find_new([], {"seen": {"a": {"waiting": 1}}}) == [], "空待办没有新增")


def test_build_summary():
    pending = [
        make_item("x/137254_E5-001_yanzhen_20260929", waiting=15, total=15),
        make_item("x/137657_E5-001_yanzhen_20261017", waiting=5, total=15, passed=10, state="partial"),
        make_item("y/137777_E5-001_yanzhen_20261018", waiting=3, total=15),
    ]
    by_region = {
        "独联体地区": {
            "pending": 3, "done": 12, "vehicles": ["E5-001"], "mileage_days": 18,
            "mileage_new": 3, "mileage_existing": 15,
        },
        "欧洲地区": {
            "pending": 0, "done": 3, "vehicles": ["LYNK01-001"], "mileage_days": 2,
            "mileage_new": 0, "mileage_existing": 2,
        },
    }
    new_items = pending[:2]
    title, body, template = todo.build_summary(
        pending, new_items, by_region, url="http://example:8901/mileage.html",
        generated_at="2026-10-23 10:00:00",
    )
    check("新增 2 个" in title and "共 3 个待确认" in title, "标题含新增/合计数量", title)
    check(template == "orange", "有新增用橙色卡片", template)
    check("各项目待办" in body, "正文有各项目待办段")
    check("独联体地区" in body and "待确认 3 天" in body, "正文按项目汇总参数验证", body[:500])
    check(
        "参数验证：待确认 3 天 · 已完成 12 天 · 累计 15 天" in body,
        "参数验证行：待确认/已完成/累计天数",
        body[:600],
    )
    check(
        "有效里程：新增 3 天 · 已有 15 天 · 累计 18 天" in body,
        "有效里程行：新增/已有/累计天数",
        body[:600],
    )
    check("**参数验证合计**：18 天（待确认 3 · 已完成 15）" in body, "正文有参数验证合计", body[:600])
    check("**有效里程合计**：累计 20 天（新增 3 天）" in body, "正文有有效里程合计", body[:600])
    check("http://example:8901/mileage.html" in body, "正文带平台链接")
    check("2026-10-23 10:00:00" in body, "正文带统计时间")
    check(
        "137254_E5-001_yanzhen_20260929" not in body,
        "不推具体数据集明细（明细在平台上看）",
        body[:500],
    )

    _, _, template2 = todo.build_summary([], [], {}, generated_at="2026-10-23 10:00:00")
    check(template2 == "grey", "无待办用灰色卡片", template2)

    title3, body3, template3 = todo.build_summary(pending, [], by_region, generated_at="2026-10-23 10:00:00")
    check(template3 == "blue", "无新增有待办用蓝色卡片", template3)
    check("剩余 3 个待确认" in title3, "无新增时标题用「剩余 N 个待确认」", title3)
    check("137254_E5-001_yanzhen_20260929" not in body3, "无新增也不列明细")

    # 没传 mileage_new/existing 时：全部算已有
    fallback = {"独联体地区": {"pending": 0, "done": 1, "vehicles": [], "mileage_days": 4}}
    _, body4, _ = todo.build_summary([], [], fallback, generated_at="2026-10-23 10:00:00")
    check("有效里程：新增 0 天 · 已有 4 天 · 累计 4 天" in body4, "缺省里程进度按已有处理", body4[:400])


def test_state_roundtrip():
    pending = [make_item("a", waiting=2), make_item("b", waiting=5)]
    state = todo.build_state(pending, seen_at="2026-10-23 10:00:00")
    check(state["seen"]["a"]["waiting"] == 2 and state["last_pending"] == 2, "状态记录待确认张数")
    check(state["last_sent_at"] == "2026-10-23 10:00:00", "发送后记录 last_sent_at")
    skipped = todo.build_state(
        pending, seen_at="2026-10-23 12:00:00", last_sent_at="2026-10-23 10:00:00"
    )
    check(skipped["last_sent_at"] == "2026-10-23 10:00:00", "跳过发送时上次发送时间不前移")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        todo.save_state(path, state)
        loaded = todo.load_state(path)
        check(loaded == json.loads(json.dumps(state)), "状态文件可保存/读回")
        path.write_text("{bad json", encoding="utf-8")
        check(todo.load_state(path) == {}, "坏状态文件容错为空")


def test_attach_mileage_progress():
    original = todo.mileage_store.region_mileage_days_since
    try:
        todo.mileage_store.region_mileage_days_since = lambda since: {"独联体地区": 3}
        by_region = {"独联体地区": {"pending": 1, "done": 0, "vehicles": [], "mileage_days": 10}}
        todo.attach_mileage_progress(by_region, since="2026-10-23 00:00:00")
        info = by_region["独联体地区"]
        check(
            info["mileage_new"] == 3 and info["mileage_existing"] == 7,
            "按上次发送时间算新增/已有里程天数",
            str(info),
        )
        first = {"独联体地区": {"pending": 1, "done": 0, "vehicles": [], "mileage_days": 10}}
        todo.attach_mileage_progress(first, since="")
        check(
            first["独联体地区"]["mileage_new"] == 10 and first["独联体地区"]["mileage_existing"] == 0,
            "没有上次发送记录时全部算新增",
            str(first["独联体地区"]),
        )
    finally:
        todo.mileage_store.region_mileage_days_since = original


def test_send_rules():
    check(todo.in_active_hours(8, 8, 22) and todo.in_active_hours(22, 8, 22), "08:00~22:00 内允许推送")
    check(not todo.in_active_hours(7, 8, 22) and not todo.in_active_hours(23, 8, 22), "07:00/23:00 和夜间不推送")
    pending = [{"name": "a", "waiting": 3}]
    send, reason = todo.should_send(pending, [])
    check(send is True, "有待办就推送（即使没有新增）", reason)
    send, reason = todo.should_send([], [])
    check(send is False, "没有待办不推送", reason)
    send, reason = todo.should_send(pending, [], only_new=True)
    check(send is False, "--only-new 时没有新增不推送", reason)
    send, reason = todo.should_send(pending, pending, only_new=True)
    check(send is True, "--only-new 时有新增就推送", reason)
    send, reason = todo.should_send([], [], force=True)
    check(send is True, "--force 即使没有待办也推送", reason)
    ok, _ = todo.viz._feishu_payload_ok('{"code":0,"StatusCode":0,"msg":"success"}')
    check(ok is True, "飞书 code=0 视为成功")
    ok, detail = todo.viz._feishu_payload_ok('{"code":11232,"msg":"frequency limited"}')
    check(ok is False and "11232" in detail, "飞书业务错误码不算成功（频率限制会重试）", detail)


def main():
    test_find_new()
    test_build_summary()
    test_state_roundtrip()
    test_attach_mileage_progress()
    test_send_rules()
    if failures:
        print(f"结果：{failures} 项失败")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
