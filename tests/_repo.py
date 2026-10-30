"""测试/自检脚本的公共引导（tests/ 下所有 Python 脚本都先 import 它）。

这些脚本只用于开发验证，不参与部署，因此都放在 tests/ 里。放在子目录后有两件事要处理：

1. **导入仓库模块**：Python 只把"脚本所在目录"（tests/）放进 sys.path，看不到仓库根目录，
   而 `ap18_visualizer` / `parse_ap18_pcap` 在根目录。这里把根目录补进 sys.path。
2. **默认数据目录**：脚本里的默认值原本是相对当前目录的 "AP18"，从 tests/ 里、或者从仓库
   外面调用时都会找不到。统一用 `repo_path()` 从仓库根目录解析。

用法（在仓库任意位置都能跑）：

    python tests/test_parse_api.py --solo
    python tests/bench_pipeline.py --limit 4
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def repo_path(*parts: str) -> Path:
    """仓库根目录下的路径（默认数据目录因此与运行时 cwd 无关）。"""
    return ROOT.joinpath(*parts)


# 默认数据目录：仓库根目录下的 AP18
DEFAULT_DATA_DIR = "AP18"
DEFAULT_DATA_PATH = repo_path(DEFAULT_DATA_DIR)

# 找不到数据时的统一提示：示例数据 AP18 是随时可能被清掉的，要告诉用户怎么指定自己的目录
NO_DATA_HINT = (
    "仓库里的示例数据 AP18 可能已被清掉，请把采集目录作为参数传进来，"
    '例如 python tests/{script} "D:\\\\你的采集目录"'
)


def collect_pcaps(inputs, default=None) -> tuple[list[Path], list[Path]]:
    """把"目录或 pcap 文件"参数展开成 pcap 列表，返回 (找到的, 找不到的)。

    必须把"找不到的路径"单独报出来：否则一个不存在的默认目录会被当成"一个 pcap 文件"
    混进列表，最后表现成一句莫名其妙的解析失败，而不是"数据目录不在了"。
    """
    found: list[Path] = []
    missing: list[Path] = []
    for item in (inputs or [default or DEFAULT_DATA_PATH]):
        path = Path(item)
        if not path.is_absolute():
            candidate = ROOT / path
            path = candidate if candidate.exists() else path
        if path.is_dir():
            found.extend(sorted(path.glob("*.pcap")))
        elif path.is_file():
            found.append(path)
        else:
            missing.append(path)
    return found, missing


def no_data_error(parser, script: str, missing=()) -> None:
    """统一的"没有数据可测"报错（带上该脚本自己的用法示例）。"""
    hint = NO_DATA_HINT.format(script=script)
    if missing:
        parser.error(f"找不到这些路径：{', '.join(str(path) for path in missing)}；{hint}")
    parser.error(f"没有找到 .pcap 文件：{hint}")
