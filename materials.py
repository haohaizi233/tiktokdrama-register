# -*- coding: utf-8 -*-
"""企业资料解析: 资料/ 文件夹 + 汇总xlsx → 公司档案, 供企业认证自动化使用

数据源(同格式资料可重复使用):
- 资料/*.xlsx        汇总表, 每行一家公司, 固定12列布局(列序不变):
    A公司名 B注册编号 C注册州 D注册城市 E注册地址
    F代表人姓名 G出生日期 H证件号 I证件有效期
    J居住州 K居住城市 L居住地址
  允许缺值(2026-09-08业务变更): B~E(企业注册字段)以PDF识别回填为准, 留空即可;
  J~L(居住地址)表单无对应字段, 本就未用。必填=A公司名+F~I代表人4项
  (证件照片识别不准, 必须表格录入)
- 资料/{公司名}/     该公司文件: 州注册文件(AOG/注册回执/Correspondence)、CP575、证件照片
- 资料/overrides.json 可选, 人工指定文件角色: {"公司名": {"id_front": "文件名", ...}}
- 资料/usage.json    自动维护, 槽位制: {公司: [{email,status,at,note}...]}, 默认每企业5号(可改)

文件角色自动识别规则:
- 州注册文件(企业营业证明): 文件名含 "-AOG-" > "Business Entity Filing Acknowledgment" > "Correspondence"
- CP575(EIN税号信, 用作代表人证明): 文件名匹配 cp575
- 证件照: front.jpg/back.jpg/selfie* 命名, 或数字命名 1=正面 2=背面 3.*=手持/自拍
"""
import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path

from paths import APP_ROOT as ROOT
MATERIALS_DIR = ROOT / "资料"
OVERRIDES_FILE = MATERIALS_DIR / "overrides.json"

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

COLUMNS = ("company", "reg_no", "reg_state", "reg_city", "reg_addr",
           "rep_name", "rep_birth", "id_no", "id_exp",
           "rep_state", "rep_city", "rep_addr")


# ---------- 基础工具 ----------

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _norm_date(v) -> str:
    """xlsx单元格/字符串 → YYYY-MM-DD"""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    s = str(v or "").strip()
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return s


def _norm_text(v) -> str:
    return re.sub(r"\s+", " ", str(v or "").strip())


# ---------- xlsx 汇总表 ----------

def load_xlsx_rows() -> list[dict]:
    """读取 资料/ 下最新的xlsx汇总表 → 原始行列表(仅含公司名列非空的行)。

    固定12列按位置映射(见COLUMNS), B~E/J~L允许留空。Excel打开时的~$锁文件跳过。
    """
    import openpyxl
    files = sorted((f for f in MATERIALS_DIR.glob("*.xlsx")
                    if not f.name.startswith("~$")),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        try:
            wb = openpyxl.load_workbook(f, data_only=True, read_only=True)
        except Exception:
            continue
        rows = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                if not row or not _norm_text(row[0]):
                    continue
                rec = {}
                for i, key in enumerate(COLUMNS):
                    rec[key] = row[i] if i < len(row) else None
                rows.append(rec)
        wb.close()
        if rows:
            return rows
    return []


# ---------- 文件角色识别 ----------

def _match_biz_proof(files: list[Path]) -> Path | None:
    for f in files:
        if "-AOG-" in f.name.upper():
            return f
    for f in files:
        if "Business Entity Filing".lower() in f.name.lower():
            return f
    for f in files:
        if "correspondence" in f.name.lower():
            return f
    for f in files:
        if "英文版" in f.name and f.suffix.lower() == ".pdf":
            return f
    return None


def _match_cp575(files: list[Path]) -> Path | None:
    for f in files:
        if re.search(r"cp.?575", f.name, re.I):
            return f
    return None


def _match_en_license_pdf(files: list[Path], preferred: str = "") -> Path | None:
    """选用英译 PDF, 兼容旧版资料.

    新版 cn_meta.license_pdf 对得上 → 用它。
    读档案时不删文件(GUI 刷新会扫全部资料夹, 删文件会卡住界面)。
    多余失败稿由执照库翻译成功/补搬时清掉。
    记名对不上 / 旧版没有记名 → 回落到仅有的一份或最新一份,
    保证上一版已经译好的资料仍能认证.
    """
    pdfs = [f for f in files
            if f.is_file() and "英文版" in f.name and f.suffix.lower() == ".pdf"]
    if not pdfs:
        return None
    keep = None
    if preferred:
        for f in pdfs:
            if f.name == preferred:
                keep = f
                break
    if keep is None:
        if len(pdfs) == 1:
            keep = pdfs[0]
        else:
            try:
                keep = max(pdfs, key=lambda p: p.stat().st_mtime)
            except OSError:
                keep = pdfs[0]
    return keep


def _pick_named_id(files: list[Path], side: str) -> Path | None:
    if not files:
        return None
    canon = [f for f in files if f.stem in (f"id-{side}", f"id_{side}")]
    if canon:
        return canon[0]
    return files[0]


def _match_id_images(images: list[Path]) -> dict:
    """证件照识别: id-正面/id-反面 优先, 其次 *-正面/*-反面, front/back, 或 1/2/3 数字命名"""
    out = {"id_front": None, "id_back": None, "id_selfie": None}
    fronts: list[Path] = []
    backs: list[Path] = []
    rest = []
    for f in images:
        stem = f.stem
        low = f.name.lower()
        if stem.endswith("-正面") or stem.endswith("_正面"):
            fronts.append(f)
        elif stem.endswith("-反面") or stem.endswith("_反面"):
            backs.append(f)
        elif low.startswith("front"):
            out["id_front"] = out["id_front"] or f
        elif low.startswith("back"):
            out["id_back"] = out["id_back"] or f
        elif low.startswith("selfie"):
            out["id_selfie"] = out["id_selfie"] or f
        else:
            rest.append(f)
    out["id_front"] = out["id_front"] or _pick_named_id(fronts, "正面")
    out["id_back"] = out["id_back"] or _pick_named_id(backs, "反面")
    if not out["id_front"]:
        for f in rest:
            if re.match(r"^1\.(jpg|jpeg|png|webp)$", f.name, re.I):
                out["id_front"] = f
    if not out["id_back"]:
        for f in rest:
            if re.match(r"^2\.(jpg|jpeg|png|webp)$", f.name, re.I):
                out["id_back"] = f
    if not out["id_selfie"]:
        for f in rest:
            if re.match(r"^3(\(\d+\))?\.(jpg|jpeg|png|webp)$", f.name, re.I):
                out["id_selfie"] = f
                break
    return out


def find_folder(company: str) -> Path | None:
    if not (MATERIALS_DIR.is_dir()):
        return None
    exact = MATERIALS_DIR / company
    if exact.is_dir():
        return exact
    low = company.strip().lower()
    for d in MATERIALS_DIR.iterdir():
        if d.is_dir() and d.name.strip().lower() == low:
            return d
    return None


def _apply_overrides(company: str, files: dict) -> dict:
    ov = _read_json(OVERRIDES_FILE, {})
    conf = ov.get(company) or {}
    folder = find_folder(company)
    for role, name in conf.items():
        if role in files and name:
            p = folder / name if folder else None
            if p and p.exists():
                files[role] = p
    return files


def _cjk_name(name: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", name or ""))


def has_id_preset(profile: dict) -> bool:
    """xlsx F~I 提供了代表人姓名+证件号 → 认证时先填再上传; 否则上传后靠站点识别。"""
    return bool(str(profile.get("rep_name") or "").strip()
                and str(profile.get("id_no") or "").strip())


def _load_cn_meta(folder: Path) -> dict:
    path = folder / "cn_meta.json"
    data = _read_json(path, {})
    return data if isinstance(data, dict) else {}


def _profile_from_folder(folder: Path, row: dict | None = None, *, workflow: str = "us") -> dict:
    row = row or {}
    company = _norm_text(row.get("company") or folder.name)
    p = {
        "name": company,
        "workflow": workflow,
        "name_zh": "",
        "name_en": "",
        "reg_no": _norm_text(row.get("reg_no")),
        "reg_state": _norm_text(row.get("reg_state")).upper(),
        "reg_city": _norm_text(row.get("reg_city")).title(),
        "reg_addr": _norm_text(row.get("reg_addr")),
        "rep_name": _norm_text(row.get("rep_name")).title(),
        "rep_birth": _norm_date(row.get("rep_birth")),
        "id_no": _norm_text(row.get("id_no")),
        "id_exp": _norm_date(row.get("id_exp")),
        "id_long_term": False,
        "rep_state": _norm_text(row.get("rep_state")).upper(),
        "rep_city": _norm_text(row.get("rep_city")).title(),
        "rep_addr": _norm_text(row.get("rep_addr")),
        "folder": str(folder),
        "biz_proof": None, "cp575": None,
        "id_front": None, "id_back": None, "id_selfie": None,
        "issues": [],
    }
    files = [f for f in folder.iterdir() if f.is_file()]
    images = [f for f in files if f.suffix.lower() in IMG_EXTS]
    p["biz_proof"] = _match_biz_proof(files)
    p["cp575"] = _match_cp575(files)
    p.update(_match_id_images(images))
    p = _apply_overrides(company, p)
    if workflow == "cn":
        meta = _load_cn_meta(folder)
        p["name_zh"] = meta.get("name_zh") or ""
        p["name_en"] = meta.get("name_en") or ""
        p["reg_no"] = p["reg_no"] or meta.get("credit_code") or ""
        p["reg_state"] = p["reg_state"] or _norm_text(meta.get("reg_state"))
        p["reg_city"] = p["reg_city"] or _norm_text(meta.get("reg_city"))
        p["reg_addr"] = p["reg_addr"] or meta.get("address_en") or meta.get("address_zh") or ""
        # 身份证字段只采用表格(xlsx F~I); 本地 OCR / 法定代表人名不写入填表槽。
        # 没有表格值时认证引擎直接上传, 交给站点识别。
        en_pdf = _match_en_license_pdf(files, preferred=meta.get("license_pdf") or "")
        if en_pdf:
            p["biz_proof"] = en_pdf
            p["cp575"] = en_pdf
        elif p["biz_proof"] and "英文版" in Path(p["biz_proof"]).name:
            # 旧版没有 license_pdf 字段, 或记名文件改过名: 沿用目录里已有的英文版
            p["cp575"] = p["cp575"] or p["biz_proof"]
        elif p["biz_proof"] and not p["cp575"]:
            p["cp575"] = p["biz_proof"]
        for k in ("biz_proof", "id_front", "id_back"):
            if not p[k]:
                p["issues"].append(f"缺少文件:{k}")
        today = date.today().isoformat()
        meta_exp = _norm_date(meta.get("id_exp"))
        meta_long = bool(meta.get("id_long_term"))
        if p["rep_birth"] and p["rep_birth"] >= today:
            p["issues"].append("出生日期异常")
        if (p["id_exp"] or meta_exp) and not (p["id_long_term"] or meta_long):
            exp = p["id_exp"] or meta_exp
            if exp <= today:
                p["issues"].append(f"证件有效期已过期({exp})")
    else:
        today = date.today().isoformat()
        for k in ("rep_name", "rep_birth", "id_no", "id_exp"):
            if not p[k]:
                p["issues"].append(f"缺少数据:{k}")
        if p["rep_birth"] and p["rep_birth"] >= today:
            p["issues"].append("出生日期异常")
        if p["id_exp"] and p["id_exp"] <= today:
            p["issues"].append(f"证件有效期已过期({p['id_exp']})")
        for k in ("biz_proof", "cp575", "id_front", "id_back"):
            if not p[k]:
                p["issues"].append(f"缺少文件:{k}")
    p["complete"] = not p["issues"]
    return p


def cn_folder_profiles() -> list[dict]:
    """Chinese-named folders in 资料/ that came from 中国执照库 (英文版 PDF)."""
    if not MATERIALS_DIR.is_dir():
        return []
    skip = {"资料不全"}
    result = []
    for d in sorted(MATERIALS_DIR.iterdir(), key=lambda p: p.name):
        if not d.is_dir() or d.name in skip or not _cjk_name(d.name):
            continue
        result.append(_profile_from_folder(d, {"company": d.name}, workflow="cn"))
    return result


# ---------- 公司档案 ----------

def profiles() -> list[dict]:
    """全部公司档案(含文件与数据校验结果), 按xlsx行序, 再追加中文文件夹。"""
    result = []
    for row in load_xlsx_rows():
        company = _norm_text(row.get("company"))
        if not company:
            continue
        folder = find_folder(company)
        if not folder:
            p = {
                "name": company,
                "workflow": "us",
                "name_zh": "",
                "name_en": "",
                "reg_no": _norm_text(row.get("reg_no")),
                "reg_state": _norm_text(row.get("reg_state")).upper(),
                "reg_city": _norm_text(row.get("reg_city")).title(),
                "reg_addr": _norm_text(row.get("reg_addr")),
                "rep_name": _norm_text(row.get("rep_name")).title(),
                "rep_birth": _norm_date(row.get("rep_birth")),
                "id_no": _norm_text(row.get("id_no")),
                "id_exp": _norm_date(row.get("id_exp")),
                "id_long_term": False,
                "rep_state": _norm_text(row.get("rep_state")).upper(),
                "rep_city": _norm_text(row.get("rep_city")).title(),
                "rep_addr": _norm_text(row.get("rep_addr")),
                "folder": None,
                "biz_proof": None, "cp575": None,
                "id_front": None, "id_back": None, "id_selfie": None,
                "issues": ["资料文件夹不存在"],
                "complete": False,
            }
            result.append(p)
            continue
        workflow = "cn" if _cjk_name(company) else "us"
        result.append(_profile_from_folder(folder, row, workflow=workflow))
    names = {p["name"] for p in result}
    for p in cn_folder_profiles():
        if p["name"] not in names:
            result.append(p)
    dedup = {}
    for p in result:
        dedup[p["name"]] = p
    return list(dedup.values())


# ---------- 用量跟踪(槽位制: 一司最多 COMPANY_SLOTS 号) ----------

DEFAULT_COMPANY_SLOTS = 5
MIN_COMPANY_SLOTS = 1
MAX_COMPANY_SLOTS = 20
# 可变: 平台政策可能调整。改小不会删已占用槽, 但该公司不再分新号。
COMPANY_SLOTS = DEFAULT_COMPANY_SLOTS
_OPEN_SLOT = ("assigned", "failed")  # 未终态: 崩溃残留 / 提交未确认, 重试必须续上


class SlotFullError(RuntimeError):
    """写入新槽位时该公司已满(同邮箱更新不走这条)"""


def _clean_email(email) -> str:
    from pool import norm_email
    return norm_email(email)


def _email_eq(stored, email) -> bool:
    from pool import emails_match
    return emails_match(stored, email)


def _slots_file() -> Path:
    return ROOT / "company_slots.json"


def _clamp_slots(n) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return DEFAULT_COMPANY_SLOTS
    return max(MIN_COMPANY_SLOTS, min(MAX_COMPANY_SLOTS, v))


def get_company_slots() -> int:
    return _clamp_slots(COMPANY_SLOTS)


def max_slot_occupancy(usage: dict | None = None) -> tuple[int, str]:
    """Highest per-company slot count in usage.json. Returns (count, company)."""
    usage = load_usage() if usage is None else usage
    best, who = 0, ""
    for company, slots in (usage or {}).items():
        n = len(slots or [])
        if n > best:
            best, who = n, company
    return best, who


def set_company_slots(n: int) -> int:
    """Persist the per-company cap and update the live module value.

    Shrinking does not drop existing usage rows; companies already over the
    new cap simply get no new assignments.
    """
    global COMPANY_SLOTS
    COMPANY_SLOTS = _clamp_slots(n)
    _write_json(_slots_file(), {
        "slots": COMPANY_SLOTS,
        "default": DEFAULT_COMPANY_SLOTS,
    })
    return COMPANY_SLOTS


def reload_company_slots() -> int:
    global COMPANY_SLOTS
    data = _read_json(_slots_file(), {})
    raw = data.get("slots") if isinstance(data, dict) else None
    COMPANY_SLOTS = _clamp_slots(raw if raw is not None else DEFAULT_COMPANY_SLOTS)
    return COMPANY_SLOTS


reload_company_slots()


def _norm_usage(raw) -> dict:
    """归一化为 {公司: [槽位dict...]}; 旧格式(公司→单条dict)惰性迁移为1条槽位"""
    out = {}
    for company, v in (raw or {}).items():
        if isinstance(v, dict):
            v = [v]
        if isinstance(v, list):
            slots = [dict(s) for s in v
                     if isinstance(s, dict) and s.get("email")]
            if slots:
                out[company] = slots
    return out


def _usage_file() -> Path:
    """运行时从 MATERIALS_DIR 派生。import 时固定的模块常量会让测试
    (monkeypatch MATERIALS_DIR)把假数据写进真实 usage.json——
    2026-09-10 实测踩坑, 测试污染了真实槽位记录。"""
    return MATERIALS_DIR / "usage.json"


def load_usage() -> dict:
    return _norm_usage(_read_json(_usage_file(), {}))


def save_usage(usage: dict) -> None:
    _write_json(_usage_file(), _norm_usage(usage))


def available_slots(company: str, usage: dict | None = None) -> int:
    usage = load_usage() if usage is None else usage
    return max(0, COMPANY_SLOTS - len(usage.get(company) or []))


# ---------- 服务端拉黑(营业执照认证账号达服务端上限等) ----------
# 独立于usage.json: 本地槽位只统计本工具的用量, 服务端上限含历史/外部
# 认证, 计数可能低估; 上传被服务端以"上限"类message拒绝时拉黑该公司,
# 后续账号自动换下一家, 不再白试。删除 资料/blocked.json 对应条目可恢复。

def _blocked_file() -> Path:
    """运行时派生, 同 _usage_file()(2026-09-10测试踩坑: import时固定
    常量导致monkeypatch MATERIALS_DIR不生效, 假拉黑记录写进真实文件)"""
    return MATERIALS_DIR / "blocked.json"


def load_blocked() -> dict:
    return _read_json(_blocked_file(), {})


def block_company(company: str, reason: str) -> None:
    blocked = load_blocked()
    if company in blocked:
        return
    blocked[company] = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
    }
    MATERIALS_DIR.mkdir(exist_ok=True)
    _write_json(_blocked_file(), blocked)


def unblock_company(company: str) -> bool:
    blocked = load_blocked()
    if company not in blocked:
        return False
    blocked.pop(company)
    _write_json(_blocked_file(), blocked)
    return True


def mark_usage(company: str, email: str, status: str, note: str = "") -> int:
    """记账该公司该邮箱的槽位(同公司同邮箱更新, 否则追加新槽位)。

    追加时若已满 COMPANY_SLOTS 则抛 SlotFullError(同邮箱更新不受限)。
    返回记账后该公司剩余可用槽位数。
    """
    usage = load_usage()
    slots = usage.setdefault(company, [])
    email = _clean_email(email)
    for s in slots:
        if _email_eq(s.get("email"), email):
            s["email"] = email
            s.update(status=status,
                     at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     note=note or s.get("note", ""))
            break
    else:
        if len(slots) >= COMPANY_SLOTS:
            raise SlotFullError(
                f"{company} 槽位已满({COMPANY_SLOTS}), 拒绝为 {email} 新增占用")
        slots.append({
            "email": email,
            "status": status,  # assigned / submitted / failed
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": note,
        })
    save_usage(usage)
    return max(0, COMPANY_SLOTS - len(slots))


def release_usage(company: str, email: str) -> bool:
    """释放该公司该邮箱的槽位回收复用。submitted 槽位=已成功认证,
    释放会导致同槽重复使用, 一律拒绝(返回False)。
    """
    usage = load_usage()
    slots = usage.get(company) or []
    email = _clean_email(email)
    for s in slots:
        if _email_eq(s.get("email"), email) and s.get("status") != "submitted":
            slots.remove(s)
            if not slots:
                usage.pop(company, None)
            save_usage(usage)
            return True
    return False


def pick_company() -> dict | None:
    """挑一家还有空槽且资料完整的公司(按xlsx行序, 先填满一家再换下一家)。

    服务端拉黑的公司(blocked.json, 如营业执照认证账号已达服务端上限)
    不再选取——本地槽位计数可能低估服务端侧历史用量(2026-09-09)。
    """
    blocked = load_blocked()
    usage = load_usage()
    for p in profiles():
        if (p["complete"] and p["name"] not in blocked
                and available_slots(p["name"], usage) > 0):
            return p
    return None


def find_open_slot(email: str, usage: dict | None = None) -> tuple[str, dict] | None:
    """该邮箱的未终态槽位(assigned/failed)。多条时取最近一条, submitted 不计。"""
    usage = load_usage() if usage is None else usage
    email = _clean_email(email)
    found: list[tuple[str, dict]] = []
    for company, slots in usage.items():
        for s in slots:
            if _email_eq(s.get("email"), email) and s.get("status") in _OPEN_SLOT:
                found.append((company, s))
    if not found:
        return None
    found.sort(key=lambda x: x[1].get("at") or "", reverse=True)
    company, slot = found[0]
    return company, dict(slot)


def profile_by_name(company: str) -> dict | None:
    for p in profiles():
        if p["name"] == company:
            return p
    return None


def pick_company_for(email: str) -> dict | None:
    """优先续上该邮箱未终态绑定; 没有则按行序挑空槽公司。

    有未终态绑定但资料已不完整/不在表中时, 仍返回该公司档案(complete=False),
    调用方必须报错, 不得改绑新公司(否则一号占两槽)。
    例外: 绑定的公司已被服务端拉黑(执照达认证上限)——服务端已明确拒绝
    该执照, 认证不可能通过它完成, 释放未终态槽位后改选下一家(2026-09-09)。
    """
    binding = find_open_slot(email)
    if not binding:
        return pick_company()
    company, _ = binding
    if company in load_blocked():
        release_usage(company, email)
        return pick_company()
    p = profile_by_name(company)
    if p:
        return p
    return {
        "name": company,
        "complete": False,
        "issues": ["续用上次绑定但该公司已不在汇总表"],
    }


def drop_extra_open_slots(email: str, keep_company: str) -> list[str]:
    """同一邮箱只保留 keep_company 上的未终态槽, 其它公司上的 assigned/failed 释放。

    submitted 不动。返回被回收的公司名列表。
    """
    usage = load_usage()
    dropped: list[str] = []
    changed = False
    email = _clean_email(email)
    for company, slots in list(usage.items()):
        if company == keep_company:
            continue
        kept = []
        for s in slots:
            if _email_eq(s.get("email"), email) and s.get("status") in _OPEN_SLOT:
                dropped.append(company)
                changed = True
            else:
                kept.append(s)
        if kept:
            usage[company] = kept
        else:
            usage.pop(company, None)
    if changed:
        save_usage(usage)
    return dropped


def reclaim_assigned() -> list[dict]:
    """回收崩溃残留: 只释放 status=assigned 的槽(从未点提交)。

    failed=已点提交未确认, 可能平台已受理, 不回收, 由重试续上。
    submitted 终态不回收。返回 [{company, email}, ...]。
    """
    usage = load_usage()
    reclaimed: list[dict] = []
    changed = False
    for company, slots in list(usage.items()):
        kept = []
        for s in slots:
            if s.get("status") == "assigned":
                reclaimed.append({"company": company, "email": s.get("email")})
                changed = True
            else:
                kept.append(s)
        if kept:
            usage[company] = kept
        else:
            usage.pop(company, None)
    if changed:
        save_usage(usage)
    return reclaimed


def count_assigned() -> int:
    return sum(1 for slots in load_usage().values()
               for s in slots if s.get("status") == "assigned")


def summary() -> tuple[int, int, list[str]]:
    """(可用槽位总数, 已占用槽位数, 问题公司列表)

    槽位=公司×COMPANY_SLOTS; 与待认证账号数直接可比(GUI预检/截断依据)。
    服务端拉黑的公司(blocked.json)可用槽位计0(选不到即不可用)。
    """
    usage = load_usage()
    blocked = load_blocked()
    allp = profiles()
    ok = used = 0
    for p in allp:
        if not p["complete"]:
            continue
        n = len(usage.get(p["name"]) or [])
        used += n  # 含改小上限后超出 cap 的已绑定, 不截断
        if p["name"] in blocked:
            continue
        ok += max(0, COMPANY_SLOTS - n)
    problems = [f"{p['name']}: {'; '.join(p['issues'])}" for p in allp if not p["complete"]]
    return ok, used, problems


def company_counts() -> tuple[int, int, int, int]:
    """公司数统计: (总家数, 已占用家数, 空闲家数, 资料不全家数)

    已占用=有槽位记录的公司(不论槽位是否用满); 空闲=资料完整且无任何槽位记录。
    """
    usage = load_usage()
    allp = profiles()
    total = len(allp)
    used = sum(1 for p in allp if usage.get(p["name"]))
    incomplete = sum(1 for p in allp if not p["complete"])
    free = sum(1 for p in allp if p["complete"] and not usage.get(p["name"]))
    return total, used, free, incomplete


# ---------- 资料不全归档 ----------

def incomplete_companies() -> tuple[list[dict], list[dict]]:
    """(可归档的资料不全公司, 因已绑定而保留的资料不全公司)"""
    usage = load_usage()
    allp = profiles()
    unbound = [p for p in allp if not p["complete"] and p["name"] not in usage]
    bound = [p for p in allp if not p["complete"] and p["name"] in usage]
    return unbound, bound


def _drop_xlsx_rows(names: set[str], backup: Path) -> Path | None:
    """从当前生效的xlsx汇总表删除指定公司的行, 删除前把原表复制到backup路径。

    生效表与load_xlsx_rows同语义: 按mtime取最新的"含数据"表。
    返回被修改的xlsx路径; 表被占用/无法保存返回None。
    """
    import openpyxl
    for f in sorted(MATERIALS_DIR.glob("*.xlsx"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            wb = openpyxl.load_workbook(f)
        except Exception:
            continue
        drops = {}
        has_row = False
        for ws in wb.worksheets:
            idx = []
            for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
                if not row:
                    continue
                name = _norm_text(row[0])
                if name:
                    has_row = True
                if name in names:
                    idx.append(i)
            if idx:
                drops[ws] = idx
        if not has_row:
            wb.close()
            continue
        if drops:
            for ws, idx in drops.items():
                for i in reversed(idx):
                    ws.delete_rows(i)
            try:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, backup)
                wb.save(f)
            except OSError:
                wb.close()
                return None
        wb.close()
        return f
    return None


def archive_incomplete() -> dict:
    """把资料不全且未绑定的公司归档到 资料/资料不全/:
    ①xlsx汇总表先备份到归档目录, 再删除这些公司的行(删除失败整体中止, 不动任何文件夹)
    ②公司文件夹整体移入归档目录(没有文件夹的仅删表行; 归档目录已有同名文件夹则跳过移动)
    ③overrides.json清理这些公司的条目
    已绑定(usage.json)的公司一律不归档(保留绑定历史)。
    返回 {archived:[{name,issues,folder,error}], backup, xlsx, errors}
    """
    targets, _ = incomplete_companies()
    report = {"archived": [], "backup": None, "xlsx": None, "errors": []}
    if not targets:
        return report
    names = {p["name"] for p in targets}
    archive_dir = MATERIALS_DIR / "资料不全"

    # ① 先处理xlsx: 失败(可能被Excel占用)则整体中止, 避免出现半归档状态
    backup = archive_dir / f"汇总表备份_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    xlsx = _drop_xlsx_rows(names, backup)
    if xlsx is None:
        report["errors"].append(
            "xlsx汇总表删除失败(可能正被Excel打开占用), 未做任何移动, 未归档")
        return report
    report["xlsx"] = str(xlsx)
    report["backup"] = str(backup)

    # ② 文件夹移入归档目录
    archive_dir.mkdir(parents=True, exist_ok=True)
    for p in targets:
        entry = {"name": p["name"], "issues": p["issues"],
                 "folder": None, "error": None}
        folder = find_folder(p["name"])
        if folder:
            dest = archive_dir / folder.name
            if dest.exists():
                entry["error"] = "归档目录已有同名文件夹, 未移动(表行已删)"
            else:
                try:
                    shutil.move(str(folder), str(dest))
                    entry["folder"] = str(dest)
                except OSError as e:
                    entry["error"] = f"文件夹移动失败(表行已删): {e}"
        report["archived"].append(entry)

    # ③ 清理overrides.json中这些公司的条目
    ov = _read_json(OVERRIDES_FILE, {})
    if any(n in ov for n in names):
        for n in names:
            ov.pop(n, None)
        _write_json(OVERRIDES_FILE, ov)
    return report


def full_slot_companies() -> list[dict]:
    """槽位已满(占用数 ≥ COMPANY_SLOTS)的公司, 无法再认证新号."""
    usage = load_usage()
    cap = get_company_slots()
    out = []
    for p in profiles():
        n = len(usage.get(p["name"]) or [])
        if n >= cap:
            out.append({
                "name": p["name"],
                "slots": n,
                "cap": cap,
                "folder": p.get("folder"),
            })
    return out


def delete_full_slot_companies(names: list[str] | None = None) -> dict:
    """删除满槽公司: 资料文件夹 + usage/blocked/overrides + xlsx 对应行.

    二次确认由 GUI 负责. 返回 {deleted:[{name,error}], errors, xlsx, backup}.
    """
    targets = full_slot_companies()
    if names is not None:
        want = set(names)
        targets = [t for t in targets if t["name"] in want]
    report = {"deleted": [], "errors": [], "xlsx": None, "backup": None}
    if not targets:
        return report
    name_set = {t["name"] for t in targets}

    backup_dir = MATERIALS_DIR / "已满槽"
    backup = backup_dir / f"汇总表备份_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    xlsx = _drop_xlsx_rows(name_set, backup)
    if xlsx:
        report["xlsx"] = str(xlsx)
        report["backup"] = str(backup)

    for t in targets:
        entry = {"name": t["name"], "error": None}
        folder = find_folder(t["name"])
        if folder:
            try:
                shutil.rmtree(folder)
            except OSError as e:
                entry["error"] = f"文件夹删除失败: {e}"
        report["deleted"].append(entry)
        if entry["error"]:
            report["errors"].append(f"{t['name']}: {entry['error']}")

    usage = load_usage()
    for name in name_set:
        usage.pop(name, None)
    save_usage(usage)

    blocked = load_blocked()
    if any(n in blocked for n in name_set):
        for n in name_set:
            blocked.pop(n, None)
        _write_json(_blocked_file(), blocked)

    ov = _read_json(OVERRIDES_FILE, {})
    if any(n in ov for n in name_set):
        for n in name_set:
            ov.pop(n, None)
        _write_json(OVERRIDES_FILE, ov)
    return report
