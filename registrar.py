# -*- coding: utf-8 -*-
"""注册引擎: Playwright驱动 tiktokdramacenter 邮箱验证码注册, 供GUI/CLI共用

- login_with_code(): 邮箱验证码登录/注册(独立函数, certifier会话过期重登也复用)
- Registrar: 批量注册; after_register钩子可在注册成功后立即做企业认证(同页面, 免重登)
- log(msg, level) / progress(done, total) 回调, 便于界面实时展示
- stop_event 置位后: 当前账号保持unused直接退出, 不污染状态
- 登录成功保存结构化auth: auth/{email}.json
"""
import json
import time
from datetime import datetime
from threading import Event

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import hubstudio
import mail_api
import pool

LOGIN_URL = "https://www.tiktokdramacenter.com/login"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _default_log(msg, level="info"):
    print(msg, flush=True)


def save_auth(page, email: str) -> None:
    """保存/刷新 auth/{email}.json 结构化登录凭证(重新登录时保留原注册时间)"""
    raw = page.context.storage_state()
    pool.AUTH_DIR.mkdir(exist_ok=True)
    auth_path = pool.AUTH_DIR / f"{email}.json"
    registered_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if auth_path.exists():  # 重新登录时保留原注册时间
        try:
            prev = json.loads(auth_path.read_text(encoding="utf-8"))
            registered_at = prev.get("registered_at") or registered_at
        except (json.JSONDecodeError, OSError):
            pass
    auth_path.write_text(
        json.dumps({
            "email": email,
            "platform": "tiktokdramacenter.com",
            "status": "registered",
            "registered_at": registered_at,
            "cookies": raw.get("cookies", []),
            "origins": raw.get("origins", []),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _wait_login_ready(page, timeout_s: int = 30) -> None:
    """等待登录页真正可用: 输入框可见且未被禁用(React水合完成)。
    实测偶发: 页面渲染一半, 输入框可见但disabled, 直接click会30s超时(2026-09-07)。"""
    page.wait_for_selector("input#email", state="visible", timeout=timeout_s * 1000)
    page.wait_for_selector("input#email:not([disabled])", timeout=timeout_s * 1000)


def logged_in(page) -> bool:
    """登录态检测: 当前页是否渲染了登录后界面(侧栏菜单标记)。

    实测坑(2026-09-07): ①会话失效时/home渲染公开落地页且URL不变;
    ②locator("text=企业认证")对菜单文本节点结构敏感(拆分/零宽字符时
    count恒为0, 但innerText里明明有), 故用innerText子串判断+双标记词。
    """
    if "/login" in page.url:
        return False
    try:
        txt = page.evaluate("document.body ? document.body.innerText : ''")
    except Exception:
        return False
    return "企业认证" in txt or "短剧管理" in txt


def dismiss_welcome_modal(page, wait_s: float = 0) -> bool:
    """关闭登录/注册成功后偶发的「开始使用」欢迎弹窗, 返回是否发现并已点击。

    全屏semi-modal遮罩会拦截后续一切真实点击(侧栏菜单/头像/表单按钮),
    2026-09-07实测注册后偶发卡死即此弹窗未关(DOM: .semi-modal-footer内
    文本"开始使用"的主按钮)。选择器限定在semi-modal内部, 避免误点落地页
    同名CTA; wait_s内轮询等它出现(弹窗渲染晚于页面落地是常态);
    无弹窗/点击失败静默返回False——调用方为流程入口+点击自愈清障,
    无弹窗才是常态, 不能为此抛错或长等。
    """
    deadline = time.time() + wait_s
    while True:
        try:
            btn = page.locator(".semi-modal-footer button", has_text="开始使用")
            if not btn.count():
                btn = page.locator(".semi-modal button", has_text="开始使用")
            if btn.count():
                btn.first.click(timeout=2_000)
                try:
                    btn.first.wait_for(state="hidden", timeout=3_000)
                except PWTimeout:
                    pass
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def _send_error_toast(page) -> str:
    """点发送后立刻检查 toast, 捕获「区域不支持」等即时拒绝.

    Semi UI toast (.semi-toast-content) 只显示约2-3秒, 点发送后2秒已渲染,
    再轮询3秒覆盖动画延迟。返回错误文本或空串。
    """
    _ERR = ("不支持", "区域", "地区", "失败", "错误", "频繁", "限制",
            "稍后", "无法", "不可用", "disabled", "not available", "region")
    for _ in range(6):
        texts = []
        for sel in (".semi-toast-content", ".semi-toast",
                    "[class*='toast']", "[role='alert']"):
            try:
                texts = [t.strip() for t in
                         page.locator(sel).all_inner_texts() if t.strip()]
            except Exception:
                continue
            if texts:
                break
        for t in texts:
            if any(k in t for k in _ERR):
                return t[:120]
        time.sleep(0.5)
    return ""


def login_with_code(page, email: str, *, log=None, code_timeout: int = 180,
                    stop_event: Event | None = None, shoot=None) -> None:
    """/login页邮箱验证码登录(未注册自动注册), 成功后保存/刷新 auth/{email}.json"""
    log = log or _default_log

    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        _wait_login_ready(page)
    except PWTimeout:
        # 已登录状态访问/login会被重定向回/home, 永远等不到输入框
        # (2026-09-07实测: 会话检测误判失效后重登触发)——先复核登录态
        if logged_in(page):
            log("  复核发现已处于登录状态, 无需验证码登录", "warn")
            save_auth(page, email)
            return
        # 站点偶发对/login也渲染公开落地页或水合一半(输入框disabled), 刷新一次重试
        page.reload(wait_until="domcontentloaded", timeout=60_000)
        try:
            _wait_login_ready(page)
        except PWTimeout:
            if logged_in(page):
                log("  复核发现已处于登录状态, 无需验证码登录", "warn")
                save_auth(page, email)
                return
            if shoot:
                shoot(page, "login_not_ready")
            raise
    log("  页面就绪")

    email_input = page.locator("input#email")
    email_input.click()
    email_input.fill(email)
    time.sleep(0.5)

    send_btn = page.locator("input#email ~ div.semi-input-suffix button", has_text="发送").first
    if not send_btn.count():
        send_btn = page.locator("form button", has_text="发送").first
    before_send_ms = int(time.time() * 1000)
    send_btn.click()
    if shoot:
        shoot(page, "after_send")
    time.sleep(2)
    toast = _send_error_toast(page)
    if toast:
        if shoot:
            shoot(page, "send_toast")
        raise RuntimeError(f"发送验证码被拒: {toast}")
    log("  已点击发送, 等待验证码邮件…")

    result = mail_api.wait_for_code(
        email, before_send_ms, timeout_s=code_timeout,
        stop_event=stop_event,
        log=lambda m: log(f"  [mail] {m}", "dim"),
    )
    if not result:
        raise RuntimeError("已停止" if (stop_event and stop_event.is_set()) else "超时未取到验证码")
    code, mail = result
    log(f"  验证码: {code} (来自 {mail.get('from')})", "ok")

    code_input = page.locator("input#code")
    code_input.wait_for(state="visible", timeout=10_000)
    code_input.click()
    code_input.fill(code)
    time.sleep(0.5)
    if shoot:
        shoot(page, "code_filled")

    agreement = page.locator("span#agreement")
    if agreement.count():
        cls = agreement.get_attribute("class") or ""
        if "semi-checkbox-checked" not in cls:
            box = page.locator("span#agreement .semi-checkbox-inner")
            (box if box.count() else agreement).first.click()
            time.sleep(0.5)
            log("  已补勾协议")

    submit = page.locator(
        "form button.semi-button-primary.semi-button-solid", has_text="登录"
    ).first
    if not submit.count():
        submit = page.locator("form button.semi-button-solid").first
    submit.click()

    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=20_000)
        log(f"  URL已跳转: {page.url}")
    except PWTimeout:
        log(f"  URL未跳转(仍为 {page.url}), 继续检测", "warn")
    time.sleep(3)
    if shoot:
        shoot(page, "after_submit")

    left_login = "/login" not in page.url
    keys = page.evaluate(
        "() => Object.keys(localStorage).filter(k => /token|user|auth|session/i.test(k))"
    )
    if not left_login and not keys:
        raise RuntimeError(f"提交后疑似未登录成功, url={page.url}")

    # 首次登录/注册落地后偶发「开始使用」欢迎弹窗, 遮罩会拦截
    # 后续认证/设置密码流程的一切点击, 落地即关(wait 2s覆盖渲染延迟)
    if dismiss_welcome_modal(page, wait_s=2):
        log("  已关闭[开始使用]欢迎弹窗")
    save_auth(page, email)
    log(f"  登录凭证已保存: auth/{email}.json", "dim")


class Registrar:
    def __init__(self, accounts: list[dict], headless: bool = False,
                 log=None, progress=None, code_timeout: int = 180,
                 stop_event: Event | None = None, after_register=None,
                 use_hubstudio: bool = False, rotate_ip: bool = False,
                 on_account=None):
        """
        after_register(page, email, shoot): 注册成功后立即回调(同一已登录page),
        用于自动企业认证等; shoot(page, name)用于失败留档截图; 异常只记日志, 不影响注册结果。
        on_account(email): 每个账号处理完后回调(刷新号池用)。
        """
        self.accounts = accounts
        self.headless = headless
        self.log = log or _default_log
        self.progress = progress or (lambda done, total: None)
        self.code_timeout = code_timeout
        self.stop_event = stop_event or Event()
        self.after_register = after_register
        self.on_account = on_account
        self.use_hub = use_hubstudio
        self.rotate_ip = rotate_ip and use_hubstudio

    def _stopped(self) -> bool:
        return self.stop_event.is_set()

    def _shoot(self, page, name: str) -> None:
        try:
            pool.LOG_DIR.mkdir(exist_ok=True)
            path = pool.LOG_DIR / f"{datetime.now():%H%M%S}_{name}.png"
            page.screenshot(path=str(path), full_page=True)
            self.log(f"  截图: {path.name}", "dim")
        except Exception as e:
            self.log(f"  截图失败({name}): {e}", "dim")

    def run(self) -> tuple[int, int]:
        """串行注册所有账号, 返回 (成功数, 失败数)"""
        total = len(self.accounts)
        ok = fail = 0
        self.log(f"开始处理 {total} 个账号")
        if self.rotate_ip:
            self.log("每账号新IP: HubStudio代理对每个窗口自动分配独立出口IP(零额度), "
                     "本次实测并记录每账号IP", "dim")
        with sync_playwright() as p:
            browser = None
            chain = None  # Hub模式单窗交接链
            try:
                for i, acc in enumerate(self.accounts, 1):
                    if self._stopped():
                        self.log(f"收到停止请求, 剩余 {total - i + 1} 个账号不再处理", "warn")
                        break
                    self.progress(i - 1, total)
                    email = acc["email"]
                    self.log(f"[{i}/{total}] === {email} ===")
                    # 心跳: 每10个账号记一次, 崩溃时可定位最后存活点
                    if i % 10 == 0:
                        self.log(f"[heartbeat] 已处理 {i}/{total}, 成功 {ok} / 失败 {fail}", "dim")
                    page = context = None
                    try:
                        if self.use_hub:
                            if browser is None:
                                browser = hubstudio.connect(
                                    p, log=self.log, headless=self.headless)
                                chain = hubstudio.WindowChain(browser, log=self.log)
                            # 单窗交接: 第1账号直接用环境自带窗, 之后每账号新窗+关上一窗
                            # (每账号新context=新出口IP, cookie隔离照旧)
                            page, context = chain.acquire()
                        else:
                            if browser is None:
                                browser = p.chromium.launch(
                                    headless=self.headless,
                                    args=["--disable-blink-features=AutomationControlled"],
                                )
                            context = browser.new_context(
                                locale="zh-CN",
                                timezone_id="Asia/Shanghai",
                                viewport={"width": 1380, "height": 900},
                                user_agent=UA,
                            )
                            context.add_init_script(
                                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                            )
                            page = context.new_page()
                        ip = (hubstudio.context_ip(page)
                              if self.use_hub and self.rotate_ip else None)
                        if ip:
                            self.log(f"  出口IP: {ip}", "dim")
                        self.register_one(page, acc)
                        pool.update_account(
                            email,
                            status="registered",
                            registered_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            last_ip=ip,
                            last_error=None,
                        )
                        ok += 1
                        self.log(f"[{i}/{total}] >>> {email} 注册成功", "ok")
                        if self.after_register:
                            try:
                                self.after_register(page, email, self._shoot)
                            except Exception as e:
                                self.log(f"[{i}/{total}] 注册后附加任务失败: {e}", "warn")
                    except Exception as e:
                        if self._stopped():
                            self.log(f"[{i}/{total}] 已手动停止, {email} 保持未使用状态", "warn")
                        else:
                            pool.update_account(email, status="failed", last_error=str(e)[:300])
                            fail += 1
                            self.log(f"[{i}/{total}] >>> {email} 失败: {e}", "err")
                            if page:
                                self._shoot(page, "error")
                    finally:
                        if context and not self.use_hub:
                            context.close()
                        if self.on_account:
                            try:
                                self.on_account(email)
                            except Exception:
                                pass
            finally:
                if browser:
                    browser.close()
                if self.use_hub:  # close只断CDP连接, 环境进程需API关闭
                    hubstudio.close_env(log=self.log)
        self.progress(total, total)
        return ok, fail

    def register_one(self, page, acc: dict) -> None:
        login_with_code(page, acc["email"], log=self.log,
                        code_timeout=self.code_timeout,
                        stop_event=self.stop_event, shoot=self._shoot)
