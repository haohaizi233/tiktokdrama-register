# -*- coding: utf-8 -*-
"""号池管理: pool_data/ 文件夹内多个json自动合并为统一号池

- 每次导入txt/粘贴文本 → 生成 pool_data/pool_时间戳.json (只保存新增邮箱, 自动全局续编号)
- 号池视图 = 文件夹内全部json按修改时间合并, 同邮箱后导入的覆盖旧的
- 状态更新会写回所有包含该邮箱的json, 保持一致
"""
import json
import re
from datetime import datetime
from pathlib import Path

from paths import APP_ROOT as ROOT
POOL_DIR = ROOT / "pool_data"
AUTH_DIR = ROOT / "auth"
LOG_DIR = ROOT / "logs"
LEGACY_ACCOUNTS = ROOT / "accounts.json"
SEP = "----"
# 严格邮箱正则: 只允许常见邮箱字符, 排除引号/逗号/分号等标点
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# 导入时需要剥离的包裹字符(ASCII/中文/弯引号、逗号/分号/空格等)
_STRIP_CHARS = "\"' \t\r\n,;，；“”‘’＂＇「」『』"


def norm_email(value) -> str:
    """剥离首尾包裹标点后的邮箱。号池/用量/机构名比对都走这一份。"""
    if value is None:
        return ""
    return str(value).strip(_STRIP_CHARS)


def emails_match(a, b) -> bool:
    return bool(norm_email(a)) and norm_email(a) == norm_email(b)

def ensure_dirs() -> None:
    for d in (POOL_DIR, AUTH_DIR, LOG_DIR):
        d.mkdir(exist_ok=True)

def _new_account(index: int, email: str, password: str, client_id: str, token: str) -> dict:
    return {
        "index": index,
        "email": email,
        "password": password,
        "client_id": client_id,
        "token": token,
        "status": "unused",
        "registered_at": None,
        "last_error": None,
    }

def parse_lines(text: str) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """解析 `序号\t邮箱----密码----client_id----令牌` 文本 → (账号元组列表, 异常行列表)

    自动剥离每段首尾的引号/逗号/分号等包裹标点, 并用严格正则校验邮箱格式。
    """
    accounts, bad = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s+(.*)$", line)
        if m and SEP in m.group(2):
            line = m.group(2)
        parts = [p.strip(_STRIP_CHARS) for p in line.split(SEP)]
        if len(parts) != 4 or not EMAIL_RE.match(parts[0]):
            bad.append(raw.strip(_STRIP_CHARS)[:60])
            continue
        accounts.append((parts[0], parts[1], parts[2], parts[3]))
    return accounts, bad

def _pool_files() -> list[Path]:
    ensure_dirs()
    return sorted(POOL_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)

def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def all_accounts() -> list[dict]:
    """合并号池文件夹内全部json, 同邮箱后者覆盖, 按序号排序

    自动剥离邮箱首尾包裹标点(引号等), 跳过校验不通过的脏数据。
    """
    merged: dict[str, dict] = {}
    for f in _pool_files():
        data = _read_json(f)
        if not isinstance(data, dict):
            continue
        for a in data.get("accounts", []):
            if isinstance(a, dict) and a.get("email"):
                email = norm_email(a["email"])
                if not EMAIL_RE.match(email):
                    continue  # 脏数据(含引号等)直接跳过
                a = dict(a)
                a["email"] = email
                if not a.get("status"):
                    a["status"] = "unused"
                merged[email] = a
    return sorted(merged.values(), key=lambda a: a.get("index", 0))


def scan_dirty() -> tuple[list[str], list[str]]:
    """预览号池脏邮箱. 返回 (可修正原值列表, 应删除原值列表)."""
    fixable, droppable = [], []
    seen = set()
    for f in _pool_files():
        data = _read_json(f)
        if not isinstance(data, dict):
            continue
        for a in data.get("accounts", []):
            if not isinstance(a, dict) or not a.get("email"):
                continue
            raw = a["email"]
            if raw in seen:
                continue
            seen.add(raw)
            clean = norm_email(raw)
            if EMAIL_RE.match(clean):
                if clean != raw:
                    fixable.append(raw)
            else:
                droppable.append(raw)
    return fixable, droppable


def clean_pool() -> tuple[int, int]:
    """清洗号池中所有脏邮箱(含引号/逗号等包裹标点).

    返回 (修正数, 删除数)。修正 = 剥离标点后格式合法; 删除 = 剥离后仍不合法。
    """
    fixed, dropped = 0, 0
    for f in _pool_files():
        data = _read_json(f)
        if not isinstance(data, dict):
            continue
        changed = False
        kept = []
        for a in data.get("accounts", []):
            if not isinstance(a, dict) or not a.get("email"):
                continue
            raw_email = a["email"]
            clean = norm_email(raw_email)
            if not EMAIL_RE.match(clean):
                dropped += 1
                changed = True
                continue
            if clean != raw_email:
                a["email"] = clean
                fixed += 1
                changed = True
            kept.append(a)
        if changed:
            data["accounts"] = kept
            _write_json(f, data)
    _relink_bound_emails()
    return fixed, dropped


def _relink_bound_emails() -> None:
    """清洗号池后, 把 usage.json / institutions.json 里带引号的邮箱一并改成干净地址。"""
    try:
        import materials
        usage = materials.load_usage()
        changed = False
        for slots in usage.values():
            for s in slots:
                raw = s.get("email") or ""
                clean = norm_email(raw)
                if clean and clean != raw:
                    s["email"] = clean
                    changed = True
        if changed:
            materials.save_usage(usage)
    except Exception:
        pass
    try:
        import institutions
        items = institutions._load()
        changed = False
        for it in items:
            raw = it.get("email") or ""
            clean = norm_email(raw)
            if clean and clean != raw:
                it["email"] = clean
                changed = True
        if changed:
            institutions._save(items)
    except Exception:
        pass

def import_text(text: str, source_name: str = "粘贴内容") -> tuple[int, int, list[str]]:
    """导入账号文本 → 新增部分保存为新json. 返回 (新增数, 重复跳过数, 异常行列表)"""
    ensure_dirs()
    current = all_accounts()
    existing = {a["email"] for a in current}
    next_index = max((a.get("index", 0) for a in current), default=0) + 1
    parsed, bad = parse_lines(text)
    new = []
    for email, password, client_id, token in parsed:
        if email in existing:
            continue
        new.append(_new_account(next_index, email, password, client_id, token))
        next_index += 1
    if new:
        fname = POOL_DIR / f"pool_{datetime.now():%Y%m%d_%H%M%S}.json"
        _write_json(fname, {
            "source": source_name,
            "imported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": new,
        })
    return len(new), len(parsed) - len(new), bad

def update_account(email: str, **fields) -> None:
    """把字段更新写入所有包含该邮箱的json(按剥离引号后的地址匹配, 并写回干净邮箱)。"""
    email = norm_email(email)
    if not email:
        return
    for f in _pool_files():
        data = _read_json(f)
        if not data:
            continue
        changed = False
        for a in data.get("accounts", []):
            if emails_match(a.get("email"), email):
                a["email"] = email
                a.update(fields)
                changed = True
        if changed:
            _write_json(f, data)

def reset_failed() -> int:
    """failed → unused, 清除错误信息. 返回重置数量"""
    n = 0
    for f in _pool_files():
        data = _read_json(f)
        if not data:
            continue
        changed = False
        for a in data.get("accounts", []):
            if a.get("status") == "failed":
                a["status"] = "unused"
                a["last_error"] = None
                n += 1
                changed = True
        if changed:
            _write_json(f, data)
    return n

def migrate_legacy() -> None:
    """旧版数据迁移: 根目录accounts.json → pool_data/; 裸storage_state的auth → 结构化"""
    ensure_dirs()
    if LEGACY_ACCOUNTS.exists() and not any(POOL_DIR.glob("*.json")):
        data = _read_json(LEGACY_ACCOUNTS)
        accs = [a for a in (data or {}).get("accounts", []) if isinstance(a, dict) and a.get("email")]
        if accs:
            _write_json(POOL_DIR / "pool_legacy.json", {
                "source": "accounts.json(旧版自动迁移)",
                "imported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "accounts": accs,
            })
    for f in AUTH_DIR.glob("*.json"):
        data = _read_json(f)
        if not isinstance(data, dict) or "cookies" not in data or "email" in data:
            continue
        _write_json(f, {
            "email": f.stem,
            "platform": "tiktokdramacenter.com",
            "status": "registered",
            "registered_at": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "cookies": data.get("cookies", []),
            "origins": data.get("origins", []),
        })


def reclaim_stale_assignments() -> dict:
    """启动时回收崩溃残留的 assigned 占用(从未点提交)。failed 不回收。

    返回 {"slots": [...], "institutions": [...]}。
    """
    import institutions
    import materials
    return {
        "slots": materials.reclaim_assigned(),
        "institutions": institutions.reclaim_assigned(),
    }


def select_certifiable(targets: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    """按库存挑选本次可认证账号: 未终态绑定优先续上, 新号才消耗空槽/空机构名。

    续用公司资料不全时跳过该号、不改绑(避免一号占两槽)。
    返回 (可跑列表, [(email, 跳过原因)...])。
    """
    import institutions
    import materials
    try:
        free_slots, _, _ = materials.summary()
    except Exception:
        free_slots = 0
    free_inst, _ = institutions.summary()
    picked: list[dict] = []
    skipped: list[tuple[str, str]] = []
    for a in targets:
        email = a.get("email") or ""
        slot = materials.find_open_slot(email)
        inst = institutions.find_open(email)
        if slot:
            company, _ = slot
            profile = materials.profile_by_name(company)
            if not profile:
                skipped.append((email, f"续用公司[{company}]已不在汇总表, 保持原绑定不改绑"))
                continue
            if not profile.get("complete"):
                issues = "; ".join(profile.get("issues") or [])
                skipped.append((
                    email,
                    f"续用公司[{company}]资料不全({issues}), 保持原绑定不改绑",
                ))
                continue
        need_slot = slot is None
        need_inst = inst is None
        if need_slot and free_slots <= 0:
            skipped.append((email, "无可用公司槽位"))
            continue
        if need_inst and free_inst <= 0:
            skipped.append((email, "无可用机构名"))
            continue
        if need_slot:
            free_slots -= 1
        if need_inst:
            free_inst -= 1
        picked.append(a)
    return picked, skipped
