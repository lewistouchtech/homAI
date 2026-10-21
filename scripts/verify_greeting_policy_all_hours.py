#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 test_greeting_policy 在一天各个时刻都能通过。

背景：test_03/04/07 原先用「N 分钟前」造数据，而 today_count 按**本地日历日**
计数，凌晨跑时 120/180/240 分钟前会落到昨天 → 计数对不上而失败。
2026-10-04 01:25 实测三个用例失败，同一代码 10:30 全绿。

做法：在子进程里把 time 模块的 time/localtime/mktime 冻结到当天各时刻，
再跑测试类。greeting_policy 内部只通过 time.xxx 取当前时间，
所以 patch time 模块对所有 import 者都生效。
必须用子进程：patch time.time 会污染当前进程里其他库的行为。

已知局限：用 SQLite CURRENT_TIMESTAMP 写 created_at 的用例（test_21）
在冻结时钟下会假失败——那个值由 SQLite 的 C 库读真实系统时钟，
Python 层影响不到。这类用例需在真实时钟下验证，故默认跳过并单独说明。

用法：./venv/bin/python scripts/verify_greeting_policy_all_hours.py
"""
import subprocess
import sys
import time
from pathlib import Path

_SERVER = Path(__file__).resolve().parent.parent
_TEST_REL = "tests/unit/test_greeting_policy.py"
# 用 SQLite CURRENT_TIMESTAMP 写时间戳的用例，冻结时钟下无法验证（见模块 docstring）
SKIP_UNDER_FREEZE = "test_21"

# 关键时刻：00:01（刚过零点，可用跨度最小）、00:30、01:25（当初失败的
# 时刻）、05:00、12:00、23:59（当天末尾）
CHECK_TIMES = ["00:01", "00:30", "01:25", "05:00", "12:00", "23:59"]

_CHILD = '''
import importlib.util, sys, time, unittest

root, target, test_rel, skip_id = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]
sys.path.insert(0, root)

# 偏移式假时钟：fake_now() = 目标时刻 + 真实流逝时间。
# 不能用常量假时钟——那会让 time.sleep(...) 的 elapsed 恒为 0，
# 依赖「窗口过期」的真实时间用例（test_16 用 cooccur_window_seconds=0.05
# 配 sleep(0.08)）永远不满足。
_orig_time, _orig_localtime, _orig_mktime = time.time, time.localtime, time.mktime
_real_start = _orig_time()


def fake_now():
    return target + (_orig_time() - _real_start)


time.time = fake_now
time.localtime = lambda t=None: _orig_localtime(fake_now() if t is None else t)
time.mktime = lambda t: _orig_mktime(
    (t[0], t[1], t[2], t[3], t[4], t[5], t[6], t[7], -1))


def _case_ids(suite):
    """展开 suite，产出每个叶子用例的完整 id（支持嵌套 suite）。"""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _case_ids(item)
        else:
            yield str(item)


def _filter(suite, skip_id):
    out = unittest.TestSuite()
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            sub = _filter(item, skip_id)
            if sub.countTestCases():
                out.addTest(sub)
        elif skip_id not in str(item):
            out.addTest(item)
    return out


spec = importlib.util.spec_from_file_location("tg", root + "/" + test_rel)
tg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg)

suite = unittest.defaultTestLoader.loadTestsFromModule(tg)
suite = _filter(suite, skip_id)
result = unittest.TextTestRunner(verbosity=0).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
'''


def run_at(target: float, skip_id: str = SKIP_UNDER_FREEZE) -> bool:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(_SERVER), str(target), _TEST_REL, skip_id],
        capture_output=True, text=True, cwd=str(_SERVER))
    if proc.returncode != 0:
        print("    stdout:", proc.stdout.strip()[-1200:])
        print("    stderr:", proc.stderr.strip()[-600:])
    return proc.returncode == 0


def main() -> None:
    today = time.strftime("%Y-%m-%d")
    print(f"验证 {_TEST_REL} 在 {len(CHECK_TIMES)} 个时刻是否稳定通过")
    print(f"（已跳过 {SKIP_UNDER_FREEZE}：它用 SQLite CURRENT_TIMESTAMP，"
          "冻结 Python 时钟会造出假失败，需真实时钟单独验证）\n")
    all_ok = True
    for hhmm in CHECK_TIMES:
        target = time.mktime(
            time.strptime(f"{today} {hhmm}:00", "%Y-%m-%d %H:%M:%S"))
        ok = run_at(target)
        all_ok &= ok
        print(f"  {today} {hhmm}  {'通过' if ok else '失败 ← 仍依赖运行时刻'}")
    print("\n" + ("全部时刻通过，已与运行时刻解耦" if all_ok else "仍有时刻失败"))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()