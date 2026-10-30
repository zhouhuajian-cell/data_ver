"""飞书群消息 @人：按地区读取 open_id 配置（不依赖网络）。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ap18_visualizer as viz  # noqa: E402

failures = 0


def check(condition, name, detail=""):
    global failures
    if condition:
        print(f"  [ OK ] {name}")
    else:
        failures += 1
        print(f"  [FAIL] {name} - {detail}")


def main() -> int:
    original = viz.FEISHU_MENTIONS_FILE
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "feishu_mentions.json"
        path.write_text(
            json.dumps(
                {
                    "独联体地区": {"name": "赵国政", "open_id": "ou_test123"},
                    "无 openid 地区": {"name": "张三"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        viz.FEISHU_MENTIONS_FILE = path
        try:
            check(
                viz.feishu_region_mention("独联体地区") == "<at id=ou_test123></at>",
                "按地区生成 @open_id 片段",
                viz.feishu_region_mention("独联体地区"),
            )
            check(viz.feishu_region_mention("欧洲地区") == "", "未配置的地区不 @")
            check(viz.feishu_region_mention("无 openid 地区") == "", "缺 open_id 的配置被忽略")

            path.write_text("{bad json", encoding="utf-8")
            check(viz.feishu_region_mention("独联体地区") == "", "坏配置文件不报错")

            path.write_text("[]", encoding="utf-8")
            check(viz.feishu_region_mention("独联体地区") == "", "配置不是对象时忽略")
        finally:
            viz.FEISHU_MENTIONS_FILE = original

    if failures:
        print(f"结果：{failures} 项失败")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
