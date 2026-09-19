# -*- coding: utf-8 -*-
"""机构名库: 认证表单「机构名称」字段的唯一分配队列

- institutions.json(项目根) 持久化:
    [{"name": "...", "status": "unused|assigned|submitted|failed|discarded",
      "email": null, "at": null, "note": ""}, ...]
- 状态流转: unused(待分配) → assigned(分配中) → submitted(提交成功, 终态)
            failed = 已点提交但结果未确认(保持绑定, 核实未生效后可释放)
            discarded = 服务端提示「机构名称已存在」(终态, 永不可复用)
- 导入: txt/粘贴, 每行一条(容忍"1."/"1、"/"1\t"序号前缀), 重复跳过, 追加队尾
- 分配: allocate(email) 优先续上该邮箱未终态绑定(assigned/failed), 否则取队首 unused
- 复用: release() 仅回收未提交的; submitted/discarded 一律拒绝——submitted一名一用,
        discarded服务端已占用, 释放复用必然再撞名
- 崩溃残留: reclaim_assigned() 只回收 assigned(从未点提交); failed 由重试续上
"""
import json
import re
from datetime import datetime
from pathlib import Path

from paths import APP_ROOT as ROOT
INST_FILE = ROOT / "institutions.json"

_IDX_PREFIX = re.compile(r"^\d{1,4}\s*[.．、)\t]\s*")
_OPEN = ("assigned", "failed")

# 站点认证表单「机构名称」输入框前端限制 maxLength=30(2026-09-09 从
# cooperation/enterprise/create 页面 chunk 源码实证: Form.Input maxLength:30),
# 超长名会被浏览器截断 → 填写校验必然失败。库中超过30字符的机构名不可用。
MAX_LEN = 30


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


def import_text(text: str, source: str = "粘贴内容") -> tuple[int, int, int]:
    """导入机构名(重复跳过, 超长跳过, 追加队尾). 返回 (新增, 重复, 超长跳过)

    超过MAX_LEN(30)字符的机构名会被表单输入框截断, 导入阶段直接拒收
    (进库也是废数据, 白占一名)。
    """
    items = _load()
    existing = {it["name"] for it in items}
    names = parse_lines(text)
    new = overlong = 0
    for name in names:
        if name in existing:
            continue
        if len(name) > MAX_LEN:
            overlong += 1
            continue
        items.append({"name": name, "status": "unused",
                      "email": None, "at": None, "note": ""})
        existing.add(name)
        new += 1
    if new:
        _save(items)
    return new, len(names) - new - overlong, overlong


def all_names() -> list[dict]:
    return _load()


def summary() -> tuple[int, int]:
    """(可用数, 已用数=assigned+submitted+failed); 可用数不含超长名"""
    items = _load()
    unused = sum(1 for it in items
                 if it.get("status") == "unused" and len(it["name"]) <= MAX_LEN)
    return unused, len(items) - unused


def count_overlong() -> int:
    """超过MAX_LEN字符的名字总数(不可用, 建议清洗或改名)"""
    return sum(1 for it in _load() if len(it["name"]) > MAX_LEN)


def find_open(email: str) -> dict | None:
    """该邮箱的未终态机构名(assigned/failed)。多条时取最近一条。"""
    found = [it for it in _load()
             if it.get("email") == email and it.get("status") in _OPEN]
    if not found:
        return None
    found.sort(key=lambda it: it.get("at") or "", reverse=True)
    return dict(found[0])


def allocate(email: str) -> dict | None:
    """分配机构名: 已有该邮箱的 assigned/failed 则续上(不新占一名);
    否则取队首 unused → assigned。同一邮箱多余的未终态占用一并收回。

    超过MAX_LEN(30)的名字不可用(表单输入框截断, 必然失败):
    队首遇到时跳过; 该邮箱续上的绑定若超长则回收换可用名
    (超长名从未走到过点提交, 回收无一名两用风险)。
    """
    items = _load()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    open_idx = [i for i, it in enumerate(items)
                if it.get("email") == email and it.get("status") in _OPEN]
    if open_idx:
        open_idx.sort(key=lambda i: items[i].get("at") or "", reverse=True)
        keep = open_idx[0]
        if len(items[keep]["name"]) <= MAX_LEN:
            items[keep].update(status="assigned", email=email, at=now)
            for i in open_idx:
                if i != keep:
                    items[i].update(status="unused", email=None, at=None,
                                    note="同一邮箱重复占用, 已回收")
            _save(items)
            return dict(items[keep])
        # 续上的绑定超长: 回收(必然再失败, 不能死循环), 落到下面取队首
        for i in open_idx:
            items[i].update(status="unused", email=None, at=None,
                            note=f"超长(>{MAX_LEN}字符)不可用, 已跳过")
    for it in items:
        if it.get("status") == "unused" and len(it["name"]) <= MAX_LEN:
            it.update(status="assigned", email=email, at=now)
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
    discarded=服务端已占用(机构名称已存在), 释放复用必然再撞名, 一律拒绝"""
    items = _load()
    for it in items:
        if it["name"] == name:
            if it.get("status") in ("submitted", "discarded"):
                return False
            it.update(status="unused", email=None, at=None, note="")
            _save(items)
            return True
    return False


def discard(name: str, note: str = "") -> None:
    """废弃机构名(终态, 2026-09-10实测场景): 提交后服务端toast
    「机构名称已存在，请重新命名」——该名在平台侧已被占用, 释放复用
    必然再撞同名toast, 永久标记不可用。submitted/discarded 不改变。"""
    items = _load()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed = False
    for it in items:
        if it["name"] == name and it.get("status") not in ("submitted", "discarded"):
            it.update(status="discarded", email=None, at=now,
                      note=note or "服务端提示机构名称已存在")
            changed = True
    if changed:
        _save(items)


def reclaim_assigned() -> list[dict]:
    """回收崩溃残留: 只释放 assigned(从未点提交)。failed/submitted 不动。

    返回 [{name, email}, ...]。
    """
    items = _load()
    reclaimed: list[dict] = []
    for it in items:
        if it.get("status") == "assigned":
            reclaimed.append({"name": it["name"], "email": it.get("email")})
            it.update(status="unused", email=None, at=None, note="")
    if reclaimed:
        _save(items)
    return reclaimed


def count_assigned() -> int:
    return sum(1 for it in _load() if it.get("status") == "assigned")
