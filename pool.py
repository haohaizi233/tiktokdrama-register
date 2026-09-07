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

ROOT = Path(__file__).resolve().parent
POOL_DIR = ROOT / "pool_data"
AUTH_DIR = ROOT / "auth"
LOG_DIR = ROOT / "logs"
LEGACY_ACCOUNTS = ROOT / "accounts.json"
SEP = "----"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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
    """解析 `序号\t邮箱----密码----client_id----令牌` 文本 → (账号元组列表, 异常行列表)"""
    accounts, bad = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s+(.*)$", line)
        if m and SEP in m.group(2):
            line = m.group(2)
        parts = [p.strip() for p in line.split(SEP)]
        if len(parts) != 4 or not EMAIL_RE.match(parts[0]):
            bad.append(raw.strip()[:60])
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
    """合并号池文件夹内全部json, 同邮箱后者覆盖, 按序号排序"""
    merged: dict[str, dict] = {}
    for f in _pool_files():
        data = _read_json(f)
        if not data:
            continue
        for a in data.get("accounts", []):
            if isinstance(a, dict) and a.get("email"):
                merged[a["email"]] = a
    return sorted(merged.values(), key=lambda a: a.get("index", 0))

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
    """把字段更新写入所有包含该邮箱的json"""
    for f in _pool_files():
        data = _read_json(f)
        if not data:
            continue
        changed = False
        for a in data.get("accounts", []):
            if a.get("email") == email:
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
