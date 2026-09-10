# -*- coding: utf-8 -*-
"""一次性清洗: 机构名库中超长(>30字符)的名字改为合格名。

规则(保留品牌词+类型词, 去掉中间修饰词):
  "Aurora Scientific Research Institute"(36) → "Aurora Research Institute"(24)
撞名时逐级降级: 去掉更多词 / 类型词缩写, 保证全库唯一。
先备份原库到 institutions.json.bak_{日期}, 幂等(重跑无超长名则不动)。
"""
import json
import shutil
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent
FILE = ROOT / "institutions.json"
MAX_LEN = 30

ABBR = {"Institute": "Inst", "Centre": "Ctr", "Center": "Ctr",
        "Academy": "Acad", "Laboratory": "Lab", "Council": "Cncl"}


def shorten(name: str, taken: set[str]) -> str | None:
    words = name.split()
    first, last = words[0], words[-1]
    last2 = " ".join(words[-2:]) if len(words) >= 2 else last
    first2 = " ".join(words[:2]) if len(words) >= 2 else first

    cands = [
        f"{first} {last2}",           # Aurora Research Institute
        f"{first} {last}",            # Aurora Institute
        f"{first2} {last}",           # Aurora Scientific Institute
        f"{first} {ABBR.get(last, last)}",   # Aurora Inst
        f"{first[:26].rstrip()} {ABBR.get(last, last[:4])}",
    ]
    for c in cands:
        if len(c) <= MAX_LEN and c not in taken:
            return c
    return None


def main():
    items = json.loads(FILE.read_text(encoding="utf-8"))
    overlong_idx = [i for i, it in enumerate(items) if len(it["name"]) > MAX_LEN]
    if not overlong_idx:
        print("库中无超长名, 无需清洗")
        return
    skipped = [(i, items[i]) for i in overlong_idx
               if items[i].get("status") in ("submitted",)]
    targets = [i for i in overlong_idx if i not in {j for j, _ in skipped}]
    if skipped:
        print(f"跳过 submitted 状态的超长名 {len(skipped)} 个(不动已提交绑定)")

    taken = {it["name"] for i, it in enumerate(items) if i not in set(targets)}
    backup = FILE.with_name(f"institutions.json.bak_{date.today():%Y%m%d}")
    if not backup.exists():
        shutil.copy2(FILE, backup)
        print(f"原库已备份: {backup.name}")

    mapping = []
    failed = []
    for i in targets:
        old = items[i]["name"]
        new = shorten(old, taken)
        if new is None:
            failed.append(old)
            continue
        items[i]["name"] = new
        items[i]["note"] = f"原名[{old}]超长已改名"
        taken.add(new)
        mapping.append((old, new))

    FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"已改名 {len(mapping)} 个, 失败 {len(failed)} 个")
    for old, new in mapping:
        print(f"  {len(old):2d}→{len(new):2d}  {old}  →  {new}")
    if failed:
        print("以下名字无法自动缩短, 请手工处理:")
        for n in failed:
            print(f"  {n}")


if __name__ == "__main__":
    main()
