# -*- coding: utf-8 -*-
"""机构名库: 认证表单「机构名称」字段的唯一分配队列

- institutions.json(项目根) 持久化:
    [{"name": "...", "status": "unused|assigned|submitted|failed",
      "email": null, "at": null, "note": ""}, ...]
- 状态流转: unused(待分配) → assigned(分配中) → submitted(提交成功, 终态)
            failed = 已点提交但结果未确认(保持绑定, 核实未生效后可释放)
- 导入: txt/粘贴, 每行一条(容忍"1."/"1、"/"1\t"序号前缀), 重复跳过, 追加队尾
- 分配: allocate(email) 取队首第一个 unused(队列动态后移), 绑定邮箱
- 复用: release() 仅回收未提交的; submitted 一律拒绝——一个机构名只能用一次
"""
import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INST_FILE = ROOT / "institutions.json"

_IDX_PREFIX = re.compile(r"^\d{1,4}\s*[.．、)]\s*|^\d{1,4}\t+\s*")


def _load() -> list[dict]:
    try:
        data = json.loads(INST_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for it in data:
        if isinstance(it, str):  # 容忍纯字符串格式
            it = {"name": it}
        if isinstance(it, dict) and it.get("name"):
            it.setdefault("status", "unused")
            out.append(it)
    return out


def _save(items: list[dict]) -> None:
    INST_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                         encoding="utf-8")


def parse_lines(text: str) -> list[str]:
    """每行一条机构名: 剥离序号前缀, 压缩内部空白"""
    names = []
    for raw in text.splitlines():
        line = _IDX_PREFIX.sub("", raw.strip())
        line = re.sub(r"\s+", " ", line)
        if line:
            names.append(line)
    return names


def import_text(text: str, source: str = "粘贴内容") -> tuple[int, int]:
    """导入机构名(重复跳过, 追加队尾). 返回 (新增, 重复)"""
    items = _load()
    existing = {it["name"] for it in items}
    names = parse_lines(text)
    new = 0
    for name in names:
        if name in existing:
            continue
        items.append({"name": name, "status": "unused",
                      "email": None, "at": None, "note": ""})
        existing.add(name)
        new += 1
    if new:
        _save(items)
    return new, len(names) - new


def all_names() -> list[dict]:
    return _load()


def summary() -> tuple[int, int]:
    """(可用数, 已用数=assigned+submitted+failed)"""
    items = _load()
    unused = sum(1 for it in items if it.get("status") == "unused")
    return unused, len(items) - unused


def allocate(email: str) -> dict | None:
    """队首分配: 第一个 unused → assigned 绑定邮箱(队列动态后移)"""
    items = _load()
    for it in items:
        if it.get("status") == "unused":
            it.update(status="assigned", email=email,
                      at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            _save(items)
            return dict(it)
    return None


def find(name: str) -> dict | None:
    for it in _load():
        if it["name"] == name:
            return it
    return None


def mark(name: str, email: str, status: str, note: str = "") -> None:
    items = _load()
    for it in items:
        if it["name"] == name:
            it.update(status=status, email=email,
                      at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), note=note)
    _save(items)


def release(name: str) -> bool:
    """回收复用(仅未提交的 assigned/failed); submitted=已成功使用,
    一名一用, 释放会导致一名两用, 一律拒绝"""
    items = _load()
    for it in items:
        if it["name"] == name:
            if it.get("status") == "submitted":
                return False
            it.update(status="unused", email=None, at=None, note="")
            _save(items)
            return True
    return False
