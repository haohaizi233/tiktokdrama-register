# -*- coding: utf-8 -*-
"""HubStudio 指纹浏览器接入(2026-09-08)

自动化不再裸起Chromium, 改为接管HubStudio客户端的指纹环境:
    hubstudio.connect(playwright) → Local API打开环境 → connect_over_cdp接管

- Local API: http://127.0.0.1:6873 (需客户端在运行; API权限需VIP及以上套餐,
  免费版所有业务接口返回 code 26)
- 指纹(UA/时区/Canvas噪音/WebRTC隐藏)与SOCKS5代理全部由环境承载:
  引擎侧不传UA/timezone/viewport(会覆盖环境指纹), 不注入webdriver覆写
  (ChroBrowser自带反检测, start参数cdpHide=true屏蔽CDP痕迹)
- locale保留zh-CN: 站点选择器大量依赖简体中文文本(如 text=企业认证),
  环境语言随HK IP匹配可能渲染繁体/英文导致选择器全挂; HK出口+zh-CN
  Accept-Language属常见正常指纹, 取舍后保留
- 额度: 每次browser/start计一次"环境打开次数"(VIP每日500~2000次)。
  代理对每个新context自动分配独立出口IP(2026-09-09实测: 同一次环境打开内
  连续4个context实测4个不同IP)——每账号新IP零额度天然成立, 引擎每账号
  new_context即可, 一次环境打开串行跑完整批, 结束调close_env;
  rotate(强制重开环境)仅作兜底, 引擎不再常规调用。
  崩溃恢复优先复用仍在运行的环境(端口存hubstudio_state.json, 不重开)

API文档: https://support-orig.hubstudio.cn/0379/7beb/fbb0/6964
浏览器状态: 0已开启 1开启中 2关闭中 3已关闭
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "http://127.0.0.1:6873"
# 共享VIP团队PH出口环境(已实测站点login页正常渲染): Socks5 iprocket -res-PH
# 上次出口103.210.9.143 Globe Telecom住宅宽带, 2026-09-07批量创建core150
# 备选PH: 1819132568/1818970706/1818905913; 注意US出口到站点(CN CDN)不通
DEFAULT_CONTAINER = "1819191426"
from paths import APP_ROOT
STATE_FILE = APP_ROOT / "hubstudio_state.json"

# start可安全重试的瞬时错误(环境未成功打开); -10013环境正在运行需先stop
_TRANSIENT = {5, 21, 24, -10015}


class HubStudioError(RuntimeError):
    pass


def _default_log(msg, level="info"):
    print(msg)


def _cc(container_code):
    """containerCode统一转int(实测env/list用字符串过滤不命中, start报数据获取失败)"""
    try:
        return int(container_code)
    except (TypeError, ValueError):
        return container_code


def _post(path: str, body: dict, timeout: float = 30) -> dict:
    req = urllib.request.Request(
        API_BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Connection": "close"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise HubStudioError(f"Local API HTTP {e.code}: {e.read()[:200]!r}")
    except Exception as e:
        # 区分两种不可用(2026-09-09实测): 端口拒绝=客户端没开;
        # 超时=端口在监听但API无响应(客户端卡死/残留进程), 须重启客户端
        if "timed out" in str(e) or isinstance(e, TimeoutError):
            raise HubStudioError(
                f"Local API {path} 请求超时({timeout}s): 端口在监听但客户端无响应, "
                "HubStudio客户端可能卡死(残留进程), 请重启HubStudio客户端") from e
        raise HubStudioError(
            f"Local API {path} 请求失败: {e} (HubStudio客户端是否在运行?)") from e


def _status(container_code: str) -> int | None:
    try:
        d = _post("/api/v1/browser/all-browser-status",
                  {"containerCodes": [_cc(container_code)]})
        cs = (d.get("data") or {}).get("containers") or []
        return cs[0].get("status") if cs else None
    except HubStudioError:
        return None


def _cdp_alive(port) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=5) as r:
            return "Browser" in r.read().decode()
    except Exception:
        return False


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(container_code: str, port, ip: str | None = None) -> None:
    try:
        STATE_FILE.write_text(json.dumps(
            {"containerCode": container_code, "port": port, "ip": ip,
             "at": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
    except OSError:
        pass


def last_ip() -> str | None:
    """最近一次环境打开的代理出口IP(rotate防撞车/号池留档用)"""
    return _load_state().get("ip")


def _wait_status(container_code: str, want: int, rounds: int = 15) -> None:
    for _ in range(rounds):
        if _status(container_code) == want:
            return
        time.sleep(2)


def check_ready(timeout: float = 8) -> None:
    """快速自检: 客户端API可达+权限可用+默认环境在当前团队(不消耗打开额度)。

    失败抛HubStudioError(客户端未运行/免费版无API权限/切错团队/API卡死)。
    timeout: 单请求超时, 默认8s——预检专用短超时(2026-09-09实测客户端
    API卡死时30s超时×2请求会让调用方干等60s; 健康客户端响应<100ms)。
    """
    d = _post("/api/v1/browser/all-browser-status",
              {"containerCodes": [_cc(DEFAULT_CONTAINER)]}, timeout=timeout)
    if d.get("code") != 0:
        raise HubStudioError(
            f"Local API返回 code={d.get('code')}: {d.get('msg')}")
    # 存在性校验: all-browser-status对不存在的环境也返回code 0(status=3),
    # start才报错(实测环境不在当前团队时start报code -1数据获取失败)
    e = _post("/api/v1/env/list", {"containerCodes": [_cc(DEFAULT_CONTAINER)]},
              timeout=timeout)
    if not ((e.get("data") or {}).get("list") or []):
        raise HubStudioError(
            f"环境{DEFAULT_CONTAINER}不在当前登录的团队中"
            "(客户端是否切换了账号/团队? 可用env/list确认环境编号)")


def open_env(container_code: str = DEFAULT_CONTAINER, log=None,
             headless: bool = False, force: bool = False) -> int:
    """打开指纹环境, 返回CDP调试端口(崩溃恢复时复用已开环境, 不消耗额度)。

    force=True: 跳过复用分支, 已开环境也强制stop后重新start——
    用于rotate换IP(代理住宅池在重开时重新分配出口IP)。
    """
    log = log or _default_log
    container_code = _cc(container_code)
    st = _status(container_code)
    for _ in range(15):  # 开启中/关闭中为瞬态, 等落定
        if st not in (1, 2):
            break
        time.sleep(2)
        st = _status(container_code)
    if st == 0 and not force:
        port = _load_state().get("port")
        if port and _cdp_alive(port):
            log(f"HubStudio环境{container_code}仍在运行(CDP端口{port}), 直接复用", "dim")
            return port
        # 手动打开或端口记录丢失: API无法接管客户端已开环境, 先关再由API拉起
        log("环境已打开但无可用CDP记录, 先关闭再由API重新拉起", "warn")
        _post("/api/v1/browser/stop", {"containerCode": container_code})
        _wait_status(container_code, 3)
    elif st == 0 and force:
        _post("/api/v1/browser/stop", {"containerCode": container_code})
        _wait_status(container_code, 3)
    body = {"containerCode": container_code, "isHeadless": headless,
            "cdpHide": True}
    for attempt in (1, 2):
        d = _post("/api/v1/browser/start", body, timeout=150)
        if d.get("code") == 0:
            port = (d.get("data") or {}).get("debuggingPort")
            if not port:
                raise HubStudioError(f"start成功但无debuggingPort: {d}")
            ip = (d.get("data") or {}).get("ip") or "?"
            log(f"HubStudio环境已打开: CDP端口{port}, 代理出口IP {ip}", "ok")
            _save_state(container_code, port, ip)
            return port
        code = d.get("code")
        msg = d.get("msg") or (d.get("data") or {}).get("err") or ""
        if code == -10013:  # 环境正在运行(与状态查询竞态), 关掉重开
            log(f"环境正被占用(code -10013), 关闭后重试", "warn")
            _post("/api/v1/browser/stop", {"containerCode": container_code})
            _wait_status(container_code, 3)
            continue
        if code in _TRANSIENT and attempt == 1:
            log(f"环境打开失败(code {code} {msg}), 5秒后重试", "warn")
            time.sleep(5)
            continue
        raise HubStudioError(f"打开HubStudio环境失败 code={code}: {msg}")
    raise HubStudioError("打开HubStudio环境失败(重试用尽)")


def close_env(container_code: str = DEFAULT_CONTAINER, log=None) -> None:
    """关闭指纹环境(尽力而为: 失败只记日志, 滞留环境会被下次运行复用)"""
    log = log or _default_log
    try:
        d = _post("/api/v1/browser/stop",
                  {"containerCode": _cc(container_code)})
        if d.get("code") == 0:
            log(f"HubStudio环境{container_code}已关闭", "dim")
        else:
            log(f"关闭环境返回 code={d.get('code')} {d.get('msg')}", "warn")
    except HubStudioError as e:
        log(f"关闭环境失败(不影响数据, 下次运行会先处理): {e}", "warn")


def _cdp_connect(playwright, port):
    endpoint = f"http://127.0.0.1:{port}"
    last = None
    for _ in (1, 2):
        try:
            return playwright.chromium.connect_over_cdp(endpoint, timeout=60_000)
        except Exception as e:
            last = e
            time.sleep(3)
    raise HubStudioError(f"CDP接管失败({endpoint}): {last}")


def connect(playwright, container_code: str = DEFAULT_CONTAINER, log=None,
            headless: bool = False):
    """打开环境并CDP接管, 返回Playwright browser。

    注意: browser.close()只断开连接不关环境进程, 引擎收尾必须再调close_env()。
    """
    port = open_env(container_code, log=log, headless=headless)
    return _cdp_connect(playwright, port)


def rotate(playwright, container_code: str = DEFAULT_CONTAINER, log=None,
           headless: bool = False):
    """强制重开环境换新出口IP(2026-09-08业务: 每账号新IP)。

    stop → start走代理住宅池重新分配IP; **每次调用消耗1次环境打开额度**。
    极小概率分回同一IP, 撞车自动再重开一次(仍撞则带警告继续, 不让账号失败)。
    返回新CDP连接browser; 旧browser连接需调用方先close。

    2026-09-09实测发现代理对每个新context自动分配独立IP(见new_context),
    三引擎已改为零额度方案, 引擎侧不再调用本函数; 保留作兜底(代理行为
    变化、context不再换IP时启用)。
    """
    log = log or _default_log
    port = None
    for attempt in (1, 2):
        prev_ip = last_ip()
        port = open_env(container_code, log=log, headless=headless, force=True)
        new_ip = last_ip()
        if new_ip and prev_ip and new_ip == prev_ip and attempt == 1:
            log(f"重开后出口IP未变({new_ip}), 代理池撞车, 再重开一次", "warn")
            continue
        if new_ip and prev_ip and new_ip == prev_ip:
            log(f"两次重开仍是同一出口IP {new_ip}, 继续执行(代理池异常)", "warn")
        break
    return _cdp_connect(playwright, port)


def new_context(browser, storage_state=None):
    """HubStudio模式专用context: 指纹由环境承载, 不传UA/时区/视口/反检测脚本;
    仅保留locale=zh-CN(简体中文文本选择器依赖)与会话恢复storage_state。

    实测(2026-09-09): 代理对每个新context独立分配出口IP——同一次环境打开内
    连续4个context实测4个不同IP, 每账号新IP零额度天然成立, 无需重开环境。
    """
    return browser.new_context(locale="zh-CN", storage_state=storage_state)


IP_SITES = ("https://api.ipify.org", "https://ifconfig.me/ip")


def context_ip(page) -> str | None:
    """在给定page上实测当前context的出口IP(失败返回None, 不抛错)。

    注意: 会离开当前页面, 仅在刚建context/流程开始时调用。
    """
    for url in IP_SITES:
        try:
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            txt = (page.inner_text("body") or "").strip()
            if txt and len(txt) <= 45 and any(c.isdigit() for c in txt):
                return txt
        except Exception:
            continue
    return None


def default_page(browser):
    """环境自带主窗口(默认context的页面, 环境打开时为about:blank)。

    2026-09-09用户方案: 批次第1个账号直接用它作自动化窗口, 不再另开新窗
    (原行为每次弹两个窗: 自带about闲置 + 自动化新窗)。
    页面不存在(上次运行已交接关闭/崩溃恢复)返回None, 调用方降级new_context。
    """
    if not browser.contexts:
        return None
    pages = browser.contexts[0].pages
    return pages[0] if pages else None


def use_default_page(browser, storage_state=None):
    """把环境自带窗口改造为第1个账号的自动化窗口, 返回page(无自带窗返回None)。

    默认context=环境持久profile: 先clear_cookies清上次首账号残留(防串号),
    有storage_state时add_cookies注入登录凭证(等效new_context(storage_state);
    origins/localStorage只是i18n文本缓存非登录态, 站点会自动重建, 不注入)。

    语言(2026-09-09实测): 默认context没有locale, 环境语言随出口IP匹配(英文),
    站点按navigator.language客户端检测→渲染英文→中文文本选择器全挂
    (text=企业认证等, ensure_home误判会话失效)。覆写navigator.language即可
    让站点渲染zh-Hans(实测html.lang='zh-Hans'); Accept-Language请求头改不了
    (context已存在), 但站点语言由客户端检测决定, 不受影响。
    cookie操作与navigator覆写在默认context上实测可用(2026-09-09)。
    clear/add失败直接抛(若静默跳过, 残留cookie会导致注册到错误的账号)。
    """
    pg = default_page(browser)
    if pg is None:
        return None
    pg.context.add_init_script(
        "Object.defineProperty(navigator,'language',{get:()=>'zh-CN'});"
        "Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh']});")
    pg.context.clear_cookies()
    if storage_state and storage_state.get("cookies"):
        pg.context.add_cookies(storage_state["cookies"])
    return pg


class WindowChain:
    """Hub模式单窗交接: 任意时刻只保留一个浏览器窗口(2026-09-09用户方案)。

    第1个账号直接用环境自带about窗口(默认context); 第2个账号新建隔离context,
    新窗口就绪后再关上一个——必须先建后关: 零窗口浏览器立即退出且环境自动
    关闭(2026-09-09实测T2)。账号结束不关窗(交给下一账号交接), 批末由
    close_env整体关闭, 全程无零窗口时刻。
    """

    def __init__(self, browser, log=None):
        self.browser = browser
        self.log = log or _default_log
        self.cur = None  # (page, context|None) 当前账号窗口; context=None=自带窗
        # 崩溃恢复: 上次运行遗留的隔离context窗口, 下一窗就绪后一并交接关闭
        # (不在构造时直接关——若自带窗也已被关, 会造成零窗口杀死浏览器)
        self._pending = list(browser.contexts[1:])

    def _close(self, obj) -> None:
        try:
            obj.close()
        except Exception as e:
            self.log(f"关闭旧窗口失败(忽略): {e}", "dim")

    def acquire(self, storage_state=None):
        """取当前账号窗口(自动交接关闭上一个)。返回(page, context);
        context=None表示环境自带窗(默认context, 只能page.close不能关context)。"""
        if self.cur is None:
            pg = use_default_page(self.browser, storage_state)
            if pg is not None:
                self.cur = (pg, None)
                self.log("单窗模式: 第1个账号直接使用环境自带窗口", "dim")
                return pg, None
        ctx = None
        try:
            ctx = new_context(self.browser, storage_state=storage_state)
            pg = ctx.new_page()
        except Exception:
            if ctx is not None:
                self._close(ctx)
            raise
        # 新窗已就绪, 现在交接关闭: 上一账号窗口 + 崩溃恢复遗留窗口
        if self.cur is not None:
            ppage, pctx = self.cur
            self._close(pctx if pctx is not None else ppage)
            self.cur = None
        for leaked in self._pending:
            self._close(leaked)
        self._pending = []
        self.cur = (pg, ctx)
        return pg, ctx
