# -*- coding: utf-8 -*-
"""设置密码引擎: 已登录page → 右上角账户菜单 → 设置密码弹窗 → 填写提交 → 结果判定

- set_password():          纯页面流程(定位/填写/提交/判定), 不碰号池
- set_account_password():  密码取号池password字段 + 号池记账 + 成功后重存auth
- PasswordSetter:          批量为已注册账号设置密码(恢复会话, 过期自动验证码重登)
- after_register_tasks():  注册成功后的任务链(企业认证→设置密码, 相互独立)

页面DOM(2026-09-07用户提供MutationObserver实测, 不猜DOM):
- 头像触发器: [aria-haspopup]元素(header内), 点击后 aria-expanded="true"
- 账户菜单: Floating UI Portal挂载(**非avatar子节点**):
    [data-floating-ui-portal] [role="dialog"], 含 设置密码/退出登录 等菜单项
- 密码弹窗: 二次Portal: [data-testid="minidrama-dialog"][role="dialog"]
    #password + #confirmPassword (type=password, label=新密码/确认密码)
    确认按钮: button[data-type="primary"] 文本"确认"
- 严禁依赖: React动态ID(:r5d:)、构建hash class(avatar-xxx/itemLabel-xxx)

流程约定:
- 提交"确认"只点击一次(有副作用操作); 超时先复核响应记录, 绝不盲目重试
- 密码值永不写入日志(只记长度); 截图中输入框为掩码, 无明文泄露
- 全程Locator即时查询, 不缓存ElementHandle(Portal动态挂载/销毁)
- 历史Console 404(/passport/account/info)不参与成功判定, 只看本次提交后的响应
"""
import time
from datetime import datetime
from threading import Event

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import certifier
import hubstudio
import pool
import registrar

HOME_URL = "https://www.tiktokdramacenter.com/home"

AVATAR_NOT_FOUND = "AVATAR_NOT_FOUND"
ACCOUNT_MENU_NOT_OPENED = "ACCOUNT_MENU_NOT_OPENED"
SET_PASSWORD_ITEM_NOT_FOUND = "SET_PASSWORD_ITEM_NOT_FOUND"
PASSWORD_DIALOG_NOT_OPENED = "PASSWORD_DIALOG_NOT_OPENED"
PASSWORD_INPUT_NOT_FOUND = "PASSWORD_INPUT_NOT_FOUND"
CONFIRM_PASSWORD_INPUT_NOT_FOUND = "CONFIRM_PASSWORD_INPUT_NOT_FOUND"
PASSWORD_VALIDATION_FAILED = "PASSWORD_VALIDATION_FAILED"
SUBMIT_BUTTON_NOT_FOUND = "SUBMIT_BUTTON_NOT_FOUND"
SUBMIT_TIMEOUT = "SUBMIT_TIMEOUT"
SERVER_REJECTED = "SERVER_REJECTED"
ALREADY_SET = "ALREADY_SET"
REQUIRES_VERIFICATION = "REQUIRES_VERIFICATION"
SESSION_INVALID = "SESSION_INVALID"
NAV_FAILED = "NAV_FAILED"
NO_PASSWORD_DATA = "NO_PASSWORD_DATA"
SUCCESS = "SUCCESS"

_MENU_SEL = "[data-floating-ui-portal] [role='dialog']"
_ERR_KEYWORDS = ("失败", "错误", "无效", "过于频繁", "不匹配", "不一致",
                 "error", "failed", "invalid", "too many")


class PasswordError(RuntimeError):
    """设置密码失败; code见模块常量, detail为可读明细(不含密码明文)"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"[{code}] {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _default_log(msg, level="info"):
    print(msg, flush=True)


def _check_stop(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise PasswordError("STOPPED", "已手动停止")


def _collapse_panels(page) -> None:
    """收起残留的日期/下拉悬浮面板(Escape对已关闭组件是无害空操作)。
    先关「开始使用」欢迎弹窗: 全屏遮罩会拦截头像/菜单点击(2026-09-07实测)"""
    registrar.dismiss_welcome_modal(page)
    for _ in range(2):
        try:
            if page.locator(
                    ".semi-datepicker:visible, .semi-select-option-list:visible"
            ).count() == 0:
                return
        except Exception:
            return
        page.keyboard.press("Escape")
        time.sleep(0.3)


def _account_menu(page):
    """账户菜单定位。优先"退出登录"文本过滤: 已设置过密码的账号菜单里
    是"重置密码"而非"设置密码"(2026-09-09实测DOM), 用退出登录兜底才能
    匹配到菜单本体。"""
    for kw in ("退出登录", "设置密码", "重置密码", "修改密码"):
        loc = page.locator(_MENU_SEL).filter(has_text=kw)
        if loc.count():
            return loc
    return page.locator(_MENU_SEL).filter(has_text="退出登录")


def _avatar_candidates(page) -> list:
    """全部可见 aria-haspopup 触发器(header内/avatar样式优先), 逐一探测。
    不无脑.first: 页面可能存在多个 haspopup 触发器。"""
    try:
        info = page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('[aria-haspopup]'));
            return els.map((el, i) => {
                const r = el.getBoundingClientRect();
                const cs = getComputedStyle(el);
                const visible = r.width > 0 && r.height > 0
                    && cs.visibility !== 'hidden' && cs.display !== 'none';
                return {i, visible, inHeader: !!el.closest('header'),
                        isAvatar: /avatar/i.test(String(el.className))};
            }).filter(x => x.visible);
        }""")
    except Exception:
        info = []
    info.sort(key=lambda x: (not x.get("inHeader"), not x.get("isAvatar")))
    return [page.locator("[aria-haspopup]").nth(x["i"]) for x in info]


def _open_account_menu(page, log, stop_event):
    """点击头像打开账户菜单(无副作用, 有限重试+多候选探测)"""
    menu = _account_menu(page)
    if menu.count() and menu.first.is_visible():
        return menu
    cands = _avatar_candidates(page)
    if not cands:
        raise PasswordError(AVATAR_NOT_FOUND, "页面无可见的[aria-haspopup]头像触发器")
    last = ""
    for avatar in cands[:4]:
        for attempt in (1, 2):
            _check_stop(stop_event)
            try:
                if attempt == 2:
                    avatar.hover(timeout=3_000)
                    time.sleep(0.3)
                avatar.click(timeout=4_000)
            except Exception as e:
                last = f"点击失败:{str(e)[:60]}"
                _collapse_panels(page)
                time.sleep(0.4)
                continue
            deadline = time.time() + 2.5
            while time.time() < deadline:
                menu = _account_menu(page)
                if menu.count() and menu.first.is_visible():
                    log("  账户菜单已打开", "dim")
                    return menu
                time.sleep(0.3)
            last = "点击后账户菜单未出现"
            _collapse_panels(page)  # 误开的面板收起, 换下一个候选
    raise PasswordError(ACCOUNT_MENU_NOT_OPENED,
                        f"{last}(已探测{min(len(cands), 4)}个候选)")


def _click_set_password_item(page, menu, log, stop_event) -> None:
    """点击菜单项"设置密码"; 菜单项为"重置密码"说明该账号已有密码。

    已有密码的账号菜单显示"重置密码"(2026-09-09实测DOM: menuItem内
    span.itemLabel文本=重置密码), 直接判ALREADY_SET不再打开重置弹窗
    (重置流程非本工具目标, 误入会把已有密码当新密码改写)。"""
    for kw in ("重置密码", "修改密码"):  # 修改密码=旧猜测, 保留兜底
        if menu.get_by_text(kw, exact=True).count():
            log(f"  菜单项为[{kw}], 该账号之前已设置过密码, 跳过", "dim")
            raise PasswordError(ALREADY_SET,
                                f"菜单项为[{kw}], 该账号已设置过密码")
    item = menu.get_by_text("设置密码", exact=True)
    deadline = time.time() + 5
    while time.time() < deadline and item.count() == 0:
        _check_stop(stop_event)
        time.sleep(0.3)
    if item.count() == 0:
        raise PasswordError(SET_PASSWORD_ITEM_NOT_FOUND, "账户菜单中无[设置密码]项")
    try:
        item.first.click(timeout=5_000)
    except PWTimeout:
        _collapse_panels(page)
        item.first.dispatch_event("click")
    log("  已点击[设置密码]", "dim")


def _password_dialog_loc(page):
    """密码弹窗定位: 主定位 data-testid+文本过滤;
    fallback=含#confirmPassword的dialog(账户菜单无密码输入框, 天然区分)"""
    primary = page.locator("[data-testid='minidrama-dialog']").filter(
        has_text="设置密码")
    if primary.count() and primary.first.is_visible():
        return primary.first
    fb = page.locator("[role='dialog']").filter(
        has=page.locator("#confirmPassword"))
    if fb.count() and fb.first.is_visible():
        return fb.first
    return None


def _wait_password_dialog(page, log, stop_event, timeout_s: int = 12):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _check_stop(stop_event)
        loc = _password_dialog_loc(page)
        if loc is not None:
            return loc
        time.sleep(0.4)
    raise PasswordError(PASSWORD_DIALOG_NOT_OPENED, "设置密码弹窗未出现")


def _dialog_alive(page) -> bool:
    d = page.locator("[data-testid='minidrama-dialog']").filter(
        has_text="设置密码")
    if d.count() and d.first.is_visible():
        return True
    d2 = page.locator("[role='dialog']").filter(
        has=page.locator("#confirmPassword"))
    return bool(d2.count() and d2.first.is_visible())


def _visible_texts(scope, sel: str) -> list[str]:
    try:
        return [t.strip() for t in
                scope.locator(sel + ":visible").all_inner_texts() if t.strip()]
    except Exception:
        return []


def _field_error_text(dialog) -> str:
    """弹窗内可见的校验错误文本(error样式/role=alert/semi错误消息)"""
    for sel in ("[class*='error']", "[role='alert']",
                ".semi-form-field-error-message"):
        texts = _visible_texts(dialog, sel)
        if texts:
            return " | ".join(texts)[:200]
    return ""


def _toast_text(page) -> str:
    for sel in ("[role='alert']", ".semi-toast", "[class*='toast']"):
        texts = _visible_texts(page, sel)
        if texts:
            return " | ".join(texts)[:200]
    return ""


def _looks_error(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _ERR_KEYWORDS)


def _wait_submit_result(page, dialog, log, stop_event, seen, timeout_s: int = 30):
    """提交后综合判定(优先级): 弹窗校验错误 > 错误toast > 弹窗关闭(成功/二次验证)
    > 超时复核响应记录。绝不因click()未抛异常就判成功。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _check_stop(stop_event)
        err = _field_error_text(dialog)
        if err:
            return PASSWORD_VALIDATION_FAILED, f"表单校验: {err}"
        toast = _toast_text(page)
        if toast and _looks_error(toast):
            return SERVER_REJECTED, f"toast: {toast}"
        if not _dialog_alive(page):
            time.sleep(2.0)  # 弹窗关闭后toast可能稍后才渲染
            toast = _toast_text(page)
            if toast and _looks_error(toast):
                return SERVER_REJECTED, f"toast: {toast}"
            # 设置密码后要求验证码/邮件验证 → 人工接管, 不绕过
            if (page.locator("input#code:visible").count()
                    or page.locator("[class*='verify']:visible").count()):
                return REQUIRES_VERIFICATION, "提交后出现验证流程, 需人工接管"
            return SUCCESS, f"弹窗已关闭{f', toast: {toast}' if toast else ''}"
        time.sleep(1.0)
    # 超时: 复核本次提交产生的响应记录(历史404不在此列), 不盲目重试
    ok200 = [u for s, u in seen if s == 200]
    if ok200:
        return SUBMIT_TIMEOUT, f"提交请求已返回200但弹窗未关闭, 待复核({ok200[0]})"
    errs = [f"{s} {u}" for s, u in seen]
    if any(s >= 400 for s, _ in seen):
        return SERVER_REJECTED, f"接口拒绝: {'; '.join(errs[:2])}"
    return SUBMIT_TIMEOUT, ("提交后无明确结果"
                            + (f"(响应记录: {'; '.join(errs[:2])})" if errs else ""))


def set_password(page, new_password: str, *, log=None, stop_event=None,
                 state: dict | None = None, shoot=None) -> str:
    """在已登录page上完成设置密码全流程。成功返回判定依据描述, 失败抛PasswordError。

    state["submitted"]: [确认]点击成功后置True, 供调用方区分"未提交"与"结果未确认"。
    """
    log = log or _default_log
    # 会话恢复/重登结束的page已停在已登录/home时直接复用, 免重复goto整页
    # 重载+全文稳定等待(2026-09-07用户反馈home→登录→home→home冗余)。
    # goto超时等Playwright异常须转PasswordError——否则绕过号池记账逃逸
    # (2026-09-07 Ernestine实测: goto PWTimeout导致pwd_status未写回)
    try:
        home_ok = certifier.ensure_home(page, stop_event)
    except Exception as e:
        raise PasswordError(NAV_FAILED, f"进入/home失败: {str(e)[:120]}") from e
    if not home_ok:
        raise PasswordError(SESSION_INVALID, f"登录会话失效: {page.url}")
    # 欢迎弹窗全屏遮罩会拦截头像点击(2026-09-07注册后偶发卡死实测)
    if registrar.dismiss_welcome_modal(page):
        log("  已关闭[开始使用]欢迎弹窗", "dim")
    # 右下角悬浮球会拦截点击(企业认证流程实测同款问题)
    page.add_style_tag(content='[class*="floatinEntry"]{display:none !important;}')

    menu = _open_account_menu(page, log, stop_event)
    _click_set_password_item(page, menu, log, stop_event)
    dialog = _wait_password_dialog(page, log, stop_event)
    log("  密码弹窗已打开")

    pwd = dialog.locator("#password")
    if not pwd.count():
        pwd = dialog.get_by_label("新密码")
    conf = dialog.locator("#confirmPassword")
    if not conf.count():
        conf = dialog.get_by_label("确认密码")
    if not pwd.count():
        raise PasswordError(PASSWORD_INPUT_NOT_FOUND, "未找到新密码输入框(#password)")
    if not conf.count():
        raise PasswordError(CONFIRM_PASSWORD_INPUT_NOT_FOUND,
                            "未找到确认密码输入框(#confirmPassword)")

    pwd.first.fill(new_password)
    conf.first.fill(new_password)
    # 提交前本地预检: 非空+两次一致(值不写日志)
    v1 = pwd.first.input_value()
    v2 = conf.first.input_value()
    if not v1 or not v2:
        raise PasswordError(PASSWORD_VALIDATION_FAILED, "填写后输入框为空")
    if v1 != v2:
        raise PasswordError(PASSWORD_VALIDATION_FAILED, "两次输入不一致(本地预检)")
    log(f"  两次密码已填写且一致(长度{len(v1)})")
    if shoot:
        shoot(page, "pwd_filled")  # type=password掩码, 截图无明文

    btn = dialog.get_by_role("button", name="确认", exact=True)
    if not btn.count():
        btn = dialog.locator("button[data-type='primary']", has_text="确认")
    if not btn.count():
        raise PasswordError(SUBMIT_BUTTON_NOT_FOUND, "未找到[确认]按钮")

    # 只监听本次点击之后产生的密码相关响应(历史Console 404不参与判定)
    seen: list[tuple[int, str]] = []

    def _on_response(resp):
        try:
            url = resp.url.lower()
            if "password" in url or ("passport" in url
                                     and resp.request.method == "POST"):
                seen.append((resp.status, resp.url.split("?")[0]))
        except Exception:
            pass

    page.on("response", _on_response)
    try:
        # 有副作用操作: 点击超时=未点击成功(无请求发出), 清障后仅重试一次;
        # 点击成功后绝不再点(第一次请求可能已生效, 重复提交有副作用)
        clicked = False
        for attempt in (1, 2):
            _check_stop(stop_event)
            try:
                btn.first.click(timeout=8_000)
                clicked = True
                break
            except PWTimeout:
                if attempt == 1:
                    log("  [确认]按钮被遮挡, 清障后重试一次", "warn")
                    _collapse_panels(page)
                    page.add_style_tag(
                        content='[class*="floatinEntry"]{display:none !important;}')
                    time.sleep(0.5)
        if not clicked:
            raise PasswordError(SUBMIT_BUTTON_NOT_FOUND,
                                "[确认]按钮点击未成功(未提交)")
        if state is not None:
            state["submitted"] = True
        log("  已点击[确认], 等待结果…")
        if shoot:
            shoot(page, "pwd_submitted")

        outcome, detail = _wait_submit_result(
            page, dialog, log, stop_event, seen)
        log(f"  结果: {outcome} {detail}")
        if shoot and outcome != SUCCESS:
            shoot(page, "pwd_failed")
        if outcome == SUCCESS:
            if shoot:
                shoot(page, "pwd_success")
            return detail
        raise PasswordError(outcome, detail)
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass


def set_account_password(page, email: str, *, log=None, stop_event=None,
                         shoot=None) -> str:
    """为email设置账户密码: 密码取号池password字段(邮箱密码复用, 不硬编码)。

    号池记账: pwd_status(success/already/failed/unconfirmed)+pwd_at+pwd_error;
    成功后重存auth(服务端可能轮换登录cookie)。
    """
    log = log or _default_log
    acc = next((a for a in pool.all_accounts() if a["email"] == email), {})
    new_password = (acc.get("password") or "").strip()
    if not new_password:
        raise PasswordError(NO_PASSWORD_DATA, "号池中该账号无密码字段")
    log(f"  开始设置账户密码(密码长度{len(new_password)})")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state: dict = {}
    try:
        detail = set_password(page, new_password, log=log,
                              stop_event=stop_event, state=state, shoot=shoot)
    except PasswordError as e:
        if e.code == ALREADY_SET:
            pool.update_account(email, pwd_status="already", pwd_at=now,
                                pwd_error=None)
            raise
        status = ("unconfirmed"
                  if e.code in (SUBMIT_TIMEOUT, REQUIRES_VERIFICATION)
                  else "failed")
        pool.update_account(email, pwd_status=status, pwd_at=now,
                            pwd_error=f"[{e.code}] {e.detail}"[:300])
        raise
    try:
        registrar.save_auth(page, email)
        log("  登录凭证已刷新(auth)", "dim")
    except Exception as e:
        log(f"  重存auth失败(不影响密码结果): {e}", "warn")
    pool.update_account(email, pwd_status="success", pwd_at=now, pwd_error=None)
    return detail


def after_register_tasks(page, email, *, log=None, stop_event=None,
                         shoot=None, do_cert: bool = True,
                         do_pwd: bool = True) -> None:
    """注册成功后的附加任务链: ①企业认证 ②设置账户密码。
    两个任务相互独立, 各自捕获异常只记日志, 不影响注册结果与彼此。"""
    log = log or _default_log
    if do_cert:
        try:
            company = certifier.certify_account(
                page, email, log=log, stop_event=stop_event, shoot=shoot)
            log(f"--- 企业认证提交成功: {company} ---", "ok")
        except Exception as e:
            log(f"--- 企业认证未完成: {e}", "warn")
    if do_pwd:
        try:
            set_account_password(page, email, log=log,
                                 stop_event=stop_event, shoot=shoot)
            log(f"--- 账户密码设置成功: {email} ---", "ok")
        except Exception as e:
            log(f"--- 账户密码设置未完成: {e}", "warn")


class PasswordSetter:
    """批量为已注册账号设置密码(每账号恢复会话, 过期自动验证码重登)"""

    def __init__(self, accounts: list[dict], headless: bool = False,
                 log=None, progress=None, code_timeout: int = 180,
                 stop_event: Event | None = None,
                 use_hubstudio: bool = False, rotate_ip: bool = False):
        self.accounts = accounts
        self.headless = headless
        self.log = log or _default_log
        self.progress = progress or (lambda done, total: None)
        self.code_timeout = code_timeout
        self.stop_event = stop_event or Event()
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

    def run(self) -> tuple[int, int, int]:
        """串行设置密码, 返回 (成功, 失败, 跳过)"""
        total = len(self.accounts)
        ok = fail = skip = 0
        self.log(f"开始设置账户密码 {total} 个账号")
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
                    page = context = None
                    try:
                        if self.use_hub:
                            if browser is None:
                                browser = hubstudio.connect(
                                    p, log=self.log, headless=self.headless)
                                chain = hubstudio.WindowChain(browser, log=self.log)
                        elif browser is None:
                            browser = p.chromium.launch(
                                headless=self.headless,
                                args=["--disable-blink-features=AutomationControlled"],
                            )
                        page, context = certifier.open_session(
                            browser, email, log=self.log,
                            code_timeout=self.code_timeout,
                            stop_event=self.stop_event, shoot=self._shoot,
                            use_hub=self.use_hub, chain=chain)
                        ip = (hubstudio.context_ip(page)
                              if self.use_hub and self.rotate_ip else None)
                        if ip:
                            self.log(f"  出口IP: {ip}", "dim")
                        set_account_password(page, email, log=self.log,
                                             stop_event=self.stop_event,
                                             shoot=self._shoot)
                        ok += 1
                        if ip:
                            pool.update_account(email, last_ip=ip)
                        self.log(f"[{i}/{total}] >>> {email} 密码设置成功", "ok")
                    except PasswordError as e:
                        if self._stopped():
                            self.log(f"[{i}/{total}] 已手动停止", "warn")
                        elif e.code == ALREADY_SET:
                            skip += 1
                            self.log(f"[{i}/{total}] >>> {email} 跳过: {e}", "warn")
                        else:
                            fail += 1
                            self.log(f"[{i}/{total}] >>> {email} 失败: {e}", "err")
                            if page:
                                self._shoot(page, f"pwd_error_{email.split('@')[0]}")
                    except Exception as e:
                        if self._stopped():
                            self.log(f"[{i}/{total}] 已手动停止", "warn")
                        else:
                            fail += 1
                            self.log(f"[{i}/{total}] >>> {email} 失败: {e}", "err")
                            if page:
                                self._shoot(page, f"pwd_error_{email.split('@')[0]}")
                    finally:
                        if context and not self.use_hub:
                            # Hub模式窗口由WindowChain交接(账号末不关: 零窗口会杀死浏览器)
                            context.close()
            finally:
                if browser:
                    browser.close()
                if self.use_hub:  # close只断CDP连接, 环境进程需API关闭
                    hubstudio.close_env(log=self.log)
        self.progress(total, total)
        return ok, fail, skip
