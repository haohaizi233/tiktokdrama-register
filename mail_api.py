# -*- coding: utf-8 -*-
"""acg-mail-tool 取件API封装：查询邮箱最新邮件并提取验证码

逆向自前端 /detail 页面: POST /api/v1/tool/mail/search
服务端使用账号的 client_id + 令牌访问邮箱, 请求体 password 传空。
响应: {"code": 200, "data": [{"from", "title", "time_stamp", "content"}], "msg": ...}
content 为 HTML。
"""
import html as html_mod
import json
import re
import time
import urllib.error
import urllib.request

API_URL = "http://acg-mail-tool.htopserver.cn/api/v1/tool/mail/search"
MAIL_BOXES = ("INBOX", "JUNK")
RETRIES = 3          # 5xx/网络错误重试次数
RETRY_DELAY_S = 2.0

class MailApiError(Exception):
    pass

def search_mail(email: str, mail_box: str = "INBOX", limit: int = 10,
                begin_at: int = 0, end_at: int = 0) -> list[dict]:
    payload = {
        "email": email.strip(),
        "password": "",
        "mail_box": mail_box,
        "begin_at": begin_at,
        "end_at": end_at,
        "from": "",
        "title": "",
        "content": "",
        "limit": limit,
    }
    last_err = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(
            API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("code") != 200:
                raise MailApiError(f"API错误 code={body.get('code')} msg={body.get('msg')}")
            data = body.get("data")
            return data if isinstance(data, list) else []
        except urllib.error.HTTPError as e:
            last_err = e
            if 500 <= e.code < 600 and attempt < RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
                continue
            raise
        except urllib.error.URLError as e:
            last_err = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
                continue
            raise
    raise last_err

def to_ms(ts) -> int:
    """time_stamp 单位归一化为毫秒（秒/毫秒自适应）"""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return 0
    return ts * 1000 if ts < 10**12 else ts

def html_to_text(content: str) -> str:
    content = re.sub(r"(?is)<(script|style).*?</\1>", " ", content or "")
    content = re.sub(r"(?s)<[^>]+>", " ", content)
    text = html_mod.unescape(content)
    return re.sub(r"\s+", " ", text).strip()

_CODE_PATTERNS = [
    re.compile(r"(?:验证码|校验码|verification\s*code|one[-\s]?time\s*code|code\s+is)[^0-9]{0,30}(\d{4,8})", re.I),
    re.compile(r"\b(\d{6})\b"),
]

def extract_code(content: str) -> str | None:
    text = html_to_text(content)
    for pat in _CODE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None

def is_sender_match(mail: dict, keywords: tuple[str, ...]) -> bool:
    hay = f"{mail.get('from', '')} {mail.get('title', '')}".lower()
    return any(k.lower() in hay for k in keywords)

def wait_for_code(email: str, since_ms: int, timeout_s: int = 180,
                  poll_interval_s: float = 5.0,
                  sender_keywords: tuple[str, ...] = ("tiktok", "短剧", "drama"),
                  stop_event=None, log=None) -> tuple[str, dict] | None:
    """轮询收件箱+垃圾箱, 返回 (验证码, 命中邮件); 超时或被停止返回 None

    since_ms: 发送验证码前的时间戳(毫秒), 早于它的邮件忽略(留60s时钟偏差余量)
    stop_event: threading.Event, 置位后立即返回 None
    log: 单参数回调, 接收调试信息
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            _log("收到停止请求, 结束验证码等待")
            return None
        for box in MAIL_BOXES:
            try:
                mails = search_mail(email, box, limit=10)
            except MailApiError as e:
                _log(f"{box} 查询失败: {e}")
                continue
            except Exception as e:  # 网络抖动等
                _log(f"{box} 请求异常: {e}")
                continue
            candidates = [m for m in mails if to_ms(m.get("time_stamp")) >= since_ms - 60_000]
            candidates.sort(key=lambda m: to_ms(m.get("time_stamp")), reverse=True)
            for m in candidates:
                code = extract_code(m.get("content", ""))
                hit_kw = is_sender_match(m, sender_keywords)
                _log(f"{box} 新邮件: from={m.get('from')!r} title={m.get('title')!r} "
                     f"ts={m.get('time_stamp')} 关键词命中={hit_kw} 提取码={code}")
                if code and (hit_kw or not sender_keywords):
                    return code, m
        remaining = deadline - time.time()
        if remaining > 0:
            time.sleep(min(poll_interval_s, remaining))
    return None
