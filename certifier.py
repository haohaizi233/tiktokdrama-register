# -*- coding: utf-8 -*-
"""企业认证引擎: 恢复登录会话 → 企业认证表单 → 上传先行(OCR回填) → 补录 → 提交

- certify_account(): 在已登录page上绑定一家公司并完成认证(GUI注册后钩子/独立模式共用)
- certify_page(): 纯表单操作(导航+填表+上传+提交), 不管理公司用量
- Certifier: 批量为已注册账号做认证(独立浏览器+会话恢复/过期重登)
- 公司资料来自 materials.py, 一司一号, usage.json 跟踪绑定
- 机构名称来自 institutions.py 机构名库队列(一名一用, 不再使用企业名)
- 认证结果写回号池: cert_status / cert_company / cert_at / cert_error

表单结构(2026-09-06 实测 /cooperation/enterprise/create, Semi Design控件):
- 机构信息: institutionName(文本)
- 企业信息: enterpriseQualification(上传·单文件=州注册证明)
  enterpriseNameLocal/En, registrationNumber, registrationCountry(下拉=美国)
  registrationProvince/City/Address(文本), isLongTerm(勾选长期→免填expirationDate)
- 代表人: identityDocuments(上传·多文件=证件正反面), legalName, birthDate(日期控件)
  idNumber, idExpirationDate(日期控件)+isIdLongTerm(勾选长期), idContent(单选: 国籍/地址/两者都有→驾照含地址选"地址")
  addressProof(上传=证件正面, 驾照含居住地址可作地址证明), representativeProof(上传=CP575, 含公司名+代表人+地址)

字段ID已2026-09-07在线dump逐一核对(19个字段全吻合, 含residentialMaterialInfo.addressProof)
"""
import json
import time
from datetime import datetime
from pathlib import Path
from threading import Event

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import institutions
import materials
import pool
import registrar

HOME_URL = "https://www.tiktokdramacenter.com/home"
UA = registrar.UA
COUNTRY = "美国"
ID_CONTENT = "地址"  # 证件为美国驾照, 含地址字段


class CertError(RuntimeError):
    """认证失败(可重试)"""


class AlreadyCertified(CertError):
    """该账号已有认证记录, 无需重复提交"""


def _default_log(msg, level="info"):
    print(msg, flush=True)


def _check_stop(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise CertError("已手动停止")


def _css_id(fid: str) -> str:
    return "#" + fid.replace(".", "\\.")


def wait_stable(page, timeout_s: int = 40, stop_event=None) -> bool:
    """等待SPA渲染内容稳定(文本连续两次采样一致); 停止请求即时抛出"""
    deadline = time.time() + timeout_s
    last, same = "", 0
    while time.time() < deadline:
        _check_stop(stop_event)
        try:
            txt = page.evaluate("document.body ? document.body.innerText : ''")
        except Exception:
            txt = ""
        if txt == last and len(txt) > 80:
            same += 1
        else:
            same = 0
        last = txt
        if same >= 2:
            return True
        time.sleep(2)
    return False


def ensure_home(page, stop_event=None, timeout_s: int = 40) -> bool:
    """确保page处于已登录的/home; 返回登录态是否有效(调用方决定报错方式)。

    冗余跳转优化(2026-09-07用户实测反馈home→登录→home→home):
    - 已在/home且登录态时直接复用当前页面——会话恢复/重登结束时page本就
      停在/home, 再次goto会整页重载+全文稳定等待, 每个账号白等十几秒;
    - 需要跳转时(表单页/其他页面过来), 用"侧栏标记+头像出现"的精确条件
      等待替代2s一轮的全文稳定采样; 页面渲染稳定仍无登录标记则提前判
      失效返回False, 不再等满超时才触发重登。

    慢网络防误判(2026-09-07实测: /home因网络迟迟没加载出来被判会话失效→
    切/login重登, 48s后复核才发现其实一直登录着, 白耗一次跳转+等待):
    - "文本稳定"必须叠加 document.readyState=='complete' 才算数——文档
      还在加载时文本暂时不变≠会话失效, 稳定计数直接归零;
    - 一轮超时后若文档仍未加载完, 宽限再观察一轮, 而不是拿没加载出来
      的页面定罪(真失效会话最终会渲染完整落地页, 仍走稳定判失效);
    - goto超时也不判死刑: 导航可能仍在进行, 交给轮询继续观察。
    """
    if "/home" in page.url and registrar.logged_in(page):
        try:  # 登录态已确认, 仅兜底等头像(后续流程实际要点击的元素)
            page.wait_for_selector("[aria-haspopup]", state="visible", timeout=15_000)
        except PWTimeout:
            pass
        return True
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
    except PWTimeout:
        pass  # 慢网络: 导航未完成不判死刑, 交给下方轮询继续观察

    def _loaded() -> bool:
        try:
            return page.evaluate("document.readyState") == "complete"
        except Exception:
            return False

    def _watch(deadline_s: int):
        """轮询登录标记。True=已登录 / False=加载完成+渲染稳定仍无登录态(判失效)
        / None=超时未定(页面一直没稳定, 交给调用方兜底判定)。"""
        deadline = time.time() + deadline_s
        last, same = "", 0
        while time.time() < deadline:
            _check_stop(stop_event)
            logged = registrar.logged_in(page)
            if logged and page.locator("[aria-haspopup]").count():
                return True
            try:
                txt = page.evaluate("document.body ? document.body.innerText : ''")
            except Exception:
                txt = ""
            if txt == last and len(txt) > 80 and _loaded():
                same += 1
            else:
                same = 0
            last = txt
            if same >= 2 and not logged:
                # 宽限复核: /home偶发先渲染公开落地页、鉴权接口返回后才切换登录态
                # (2026-09-07 Ernestine实测误判失效, 靠/login重定向复核兜住);
                # 多等2秒再确认, 避免触发一次多余的重登跳转
                time.sleep(2)
                if registrar.logged_in(page):
                    continue
                return False  # 文档加载完成+渲染稳定仍无登录态 → 会话失效
            time.sleep(1)
        return None

    verdict = _watch(timeout_s)
    if verdict is None and not _loaded():
        verdict = _watch(timeout_s)  # 文档仍未加载完(慢网络), 宽限再观察一轮
    if verdict is not None:
        return verdict
    return registrar.logged_in(page)


class _Form:
    """认证表单操作封装(基于实测控件行为)"""

    def __init__(self, page, log):
        self.page = page
        self.log = log

    @staticmethod
    def _wrap(fid: str) -> str:
        return f"div.semi-form-field[x-field-id='{fid}']"

    def _input(self, fid: str):
        return self.page.locator(
            f"{self._wrap(fid)} input:not([type=file]):not([type=hidden]), "
            f"{self._wrap(fid)} textarea").first

    def _handle_ocr_conflict(self) -> bool:
        """证件OCR识别冲突弹窗处理, 返回是否遇到弹窗(绝不因弹窗自身消失而抛错)。

        - 有冲突行(含"→"箭头): 点"应用识别结果"(以证件图片为准, 审核也看图片)
        - 无冲突行的空壳弹窗(应用后收起动画期间会以空字段列表短暂重现, 实测):
          点"保留当前填写"安全关闭即可
        - 弹窗点击容忍超时: 按钮随React重渲染自行消失不算异常
        - 多弹窗叠加时取DOM最后一个(portal后挂载的在上层)
        """
        btns = self.page.locator(".semi-modal-content button", has_text="应用识别结果")
        if btns.count() == 0:
            return False
        try:
            body = self.page.locator(".semi-modal-content").last.inner_text()
            body = " | ".join(x.strip() for x in body.splitlines() if x.strip())[:200]
        except Exception:
            body = ""
        if "→" in body:  # 冲突行形如 "当前填写 X → 识别结果 Y"
            self.log(f"  证件识别与填写不一致, 应用识别结果: {body}", "warn")
            target = btns.last
        else:
            self.log(f"  识别冲突弹窗无差异字段(空壳), 直接关闭", "dim")
            target = self.page.locator(
                ".semi-modal-content button", has_text="保留当前填写").last
        try:
            target.click(timeout=5_000)
        except PWTimeout:
            try:
                target.dispatch_event("click", timeout=3_000)
            except Exception:
                self.log(f"  冲突弹窗已自行消失, 无需处理", "dim")
        except Exception:
            pass  # 弹窗随重渲染消失等, 不影响主流程
        time.sleep(1.0)
        return True

    def _collapse_datepicker(self) -> None:
        """收起残留的日期面板/下拉面板(portal悬浮层会拦截后续点击;
        Escape对已关闭的组件是无害空操作)"""
        for _ in range(2):
            if self.page.locator(
                    ".semi-datepicker:visible, .semi-select-option-list:visible"
            ).count() == 0:
                return
            self.page.keyboard.press("Escape")
            time.sleep(0.4)

    def _clear_overlays(self) -> None:
        """清掉遮挡层: 欢迎弹窗 + OCR冲突弹窗(可能多张证件连续弹) + 残留日期/下拉面板"""
        if registrar.dismiss_welcome_modal(self.page):
            self.log("  已关闭[开始使用]欢迎弹窗", "dim")
        for _ in range(4):
            if not self._handle_ocr_conflict():
                break
        self._collapse_datepicker()

    def _click(self, loc, what: str, *, timeout_s: float = 4, tries: int = 5) -> None:
        """遮挡自愈点击: 弹窗恰好在点击瞬间弹出时不挂死30s——
        短超时快速失败 → 清障 → 重试, 全部失败才报错"""
        for _ in range(tries):
            try:
                loc.click(timeout=timeout_s * 1000)
                return
            except PWTimeout:
                self._clear_overlays()
                time.sleep(0.3)
        raise CertError(f"[{what}] 点击持续被遮挡(已自动清障重试{tries}次)")

    def wait_ocr_done(self, label: str, timeout_s: int = 60, stop_event=None) -> None:
        """上传后主动排空该文件的OCR识别, 而不是任由它在后续填表途中弹冲突弹窗。

        识别完成时机不可控(实测曾在填日期时弹出, 与日期面板叠成双层遮罩导致卡死),
        故每个上传点后在此等待: 识别指示"正在智能识别"出现→消失, 期间弹出的冲突
        弹窗立即"应用识别结果", 把冲突处理收敛到安全的时点。
        """
        ind = self.page.locator("text=正在智能识别")
        seen, quiet = False, 0
        deadline = time.time() + timeout_s
        time.sleep(1.0)  # 上传完成瞬间指示可能尚未渲染
        while time.time() < deadline:
            _check_stop(stop_event)
            if self._handle_ocr_conflict():
                seen, quiet = True, 0  # 冲突弹窗=识别已完成
                continue
            if ind.count():
                if not seen:
                    self.log(f"  {label}进行中…", "dim")
                seen, quiet = True, 0
            else:
                quiet += 1
                if seen and quiet >= 2:
                    self.log(f"  {label}完成", "dim")
                    return
                if not seen and quiet >= 4:
                    return  # 无识别指示(识别极快或该文件不触发OCR), 不阻塞
            time.sleep(1)
        self.log(f"  {label}等待超时, 继续(提交前还有兜底处理)", "warn")

    def _read_value(self, loc) -> str:
        try:
            if loc.count() == 0:  # 字段不存在(如v-if隐藏)时input_value会等满超时
                return ""
            return loc.input_value().strip()
        except Exception:
            return ""

    def fill_text(self, fid: str, value, label: str) -> None:
        self._clear_overlays()
        value = str(value).strip()
        loc = self._input(fid)
        loc.scroll_into_view_if_needed(timeout=10_000)
        # 上传文件的OCR会自动回填字段: 已有内容则不重复填写(重复填写会与自动回填
        # 竞争, 打乱控件内部状态), 识别值与资料值不同时以识别为准
        cur = self._read_value(loc)
        if cur:
            if cur == value:
                self.log(f"  {label}: 已由识别自动填写, 跳过", "dim")
            else:
                self.log(f"  {label}: 识别已填[{cur[:40]}](资料[{value[:40]}]), 保留识别值", "warn")
            return
        self._click(loc, label)
        loc.fill(value)
        time.sleep(0.4)
        # OCR冲突弹窗若恰在此刻弹出并"应用识别结果", 可能覆盖刚填的值——以识别为准
        conflicted = self._handle_ocr_conflict()
        time.sleep(0.4)
        got = loc.input_value()
        if got != value:
            if conflicted:
                if got:
                    self.log(f"  {label}: 识别覆盖 [{value[:40]}]→[{got[:40]}]", "warn")
                    return
                loc.fill(value)  # 应用识别后字段短暂为空, 回填
                got = loc.input_value()
            if got != value:
                raise CertError(f"[{label}] 填写不符: 期望[{value}] 实际[{got}]")
        self.log(f"  {label}: {value[:60]}", "dim")

    def pick_country(self, name: str = COUNTRY) -> None:
        wrap = self._wrap("enterpriseMaterialInfo.registrationCountry")
        # 识别可能已回填国家下拉: 已是目标值则不动(重选可能级联清空已识别的省/市)
        try:
            cur = self.page.locator(
                f"{wrap} .semi-select-selection-text").first.inner_text().strip()
        except Exception:
            cur = ""
        if cur == name:
            self.log(f"  国家/地区: 已由识别自动填写, 跳过", "dim")
            return
        for attempt in (1, 2):
            self._clear_overlays()
            sel = self.page.locator(f"{wrap} .semi-select").first
            sel.scroll_into_view_if_needed(timeout=10_000)
            self._click(sel, "国家/地区下拉")
            try:
                self.page.wait_for_selector(
                    ".semi-select-option-list:visible", timeout=5_000)
            except PWTimeout:
                if attempt == 1:
                    self.log("  [国家/地区] 下拉未展开, 重试", "warn")
                    self.page.keyboard.press("Escape")
                    time.sleep(0.5)
                    continue
                raise CertError("[国家/地区] 下拉面板未展开")
            time.sleep(0.6)
            opt = self.page.locator(
                ".semi-select-option-list:visible .semi-select-option",
                has_text=name).first
            self._click(opt, f"国家/地区[{name}]")
            time.sleep(0.8)
            got = self.page.locator(
                f"{wrap} .semi-select-selection-text").first.inner_text().strip()
            if got == name:
                self.log(f"  国家/地区: {name}", "dim")
                return
            if attempt == 1:
                self.log(f"  [国家/地区] 选择未生效(实际[{got}]), 重试", "warn")
                self.page.keyboard.press("Escape")
                time.sleep(0.5)
        raise CertError(f"[国家/地区] 选择不符: 期望[{name}] 实际[{got}]")

    def fill_date(self, fid: str, value, label: str) -> None:
        """日期控件填写。已由识别自动回填则跳过(重复填写会打乱日期控件内部状态,
        实测曾导致表单无效提交卡死); 否则竞态自愈填写: OCR冲突弹窗可能在打开面板/
        键入/确认任一环节弹出, 与日期面板叠成双层遮罩——每环节检测处理+整轮重试。"""
        value = str(value).strip()
        self._clear_overlays()
        loc = self._input(fid)
        loc.scroll_into_view_if_needed(timeout=10_000)
        cur = self._read_value(loc)
        if cur:
            if value in cur or cur == value.replace("-", "/"):
                self.log(f"  {label}: 已由识别自动填写, 跳过", "dim")
            else:
                self.log(f"  {label}: 识别已填[{cur}](资料[{value}]), 保留识别值", "warn")
            return
        got = ""
        for attempt in (1, 2, 3):
            self._clear_overlays()
            self._click(loc, label)
            time.sleep(0.8)
            conflicted = self._handle_ocr_conflict()
            if conflicted:
                # 弹窗在点击后、键入前弹出: 处理后焦点被夺走, 重新聚焦
                self._collapse_datepicker()
                self._click(loc, label)
                time.sleep(0.5)
            self.page.keyboard.type(value)
            time.sleep(0.5)
            if self._handle_ocr_conflict():
                conflicted = True
                # 弹窗在键入后、确认前弹出: Enter会被弹窗吃掉, 先处理
                self._collapse_datepicker()
            self.page.keyboard.press("Enter")
            time.sleep(0.6)
            self._collapse_datepicker()
            got = loc.input_value().strip()
            if value in got or got == value.replace("-", "/"):
                self.log(f"  {label}: {got}", "dim")
                return
            if conflicted and got:
                # 识别结果覆盖了键入值(以证件为准), 不视为失败
                self.log(f"  {label}: 识别覆盖 [{value}]→[{got}]", "warn")
                return
            self.log(f"  [{label}] 输入未被确认(实际[{got}]), 重试", "warn")
        raise CertError(f"[{label}] 填写不符: 输入[{value}] 实际[{got}]")

    def check_box(self, fid: str, label: str) -> None:
        self._clear_overlays()
        el = self.page.locator(_css_id(fid))
        el.scroll_into_view_if_needed(timeout=10_000)
        if "semi-checkbox-checked" not in (el.get_attribute("class") or ""):
            self._click(self.page.locator(
                f"{_css_id(fid)} .semi-checkbox-inner").first, label)
            time.sleep(0.6)
        cls = self.page.locator(_css_id(fid)).get_attribute("class") or ""
        if "semi-checkbox-checked" not in cls:
            raise CertError(f"[{label}] 勾选失败")
        self.log(f"  {label}: 已勾选(对应日期免填)", "dim")

    def pick_radio(self, fid: str, value: str, label: str) -> None:
        self._clear_overlays()
        opt = self.page.locator(f"{_css_id(fid)} .semi-radio", has_text=value).first
        opt.scroll_into_view_if_needed(timeout=10_000)
        if "semi-radio-checked" not in (opt.get_attribute("class") or ""):
            self._click(opt, f"{label}[{value}]")
            time.sleep(0.6)
        cls = opt.get_attribute("class") or ""
        if "semi-radio-checked" not in cls:
            raise CertError(f"[{label}] 选择[{value}]失败")
        self.log(f"  {label}: {value}", "dim")

    def upload(self, fid: str, paths, label: str) -> None:
        """逐个set_input_files; 项目数=列表内非"添加"瓦片, 进度圈消失=上传完成。

        注: 多文件字段maxCount=2, 传满后"添加"瓦片消失, 项目计数可能不增,
        故用"非添加瓦片"计数(添加瓦片带semi-upload-add类)。
        """
        self._clear_overlays()
        wrap = self._wrap(fid)
        items = f"{wrap} .semi-upload-file-list-main > div:not(.semi-upload-add)"
        prog = f"{wrap} .semi-progress-circle"
        for raw in paths:
            path = Path(raw)
            if not path.exists():
                raise CertError(f"[{label}] 文件不存在: {path}")
            before = self.page.locator(items).count()
            self.page.locator(f"{wrap} input[type=file]").first.set_input_files(str(path))
            deadline = time.time() + 20
            while time.time() < deadline:
                time.sleep(0.5)
                if self.page.locator(items).count() > before:
                    break
            else:
                raise CertError(f"[{label}] 上传未开始: {path.name}")
            deadline = time.time() + 180
            while time.time() < deadline:
                time.sleep(1)
                if self.page.locator(prog).count() == 0:
                    break
            else:
                raise CertError(f"[{label}] 上传超时未完成: {path.name}")
            if self.page.locator(items).count() <= before:
                raise CertError(f"[{label}] 上传后项目消失(可能被服务端拒绝): {path.name}")
            time.sleep(0.8)
            self.log(f"  {label}: 已上传 {path.name}", "dim")


def click_text(page, text: str, timeout_s: int = 10) -> None:
    """点击包含指定文本的可见元素(侧栏菜单等)。

    locator(text=...)引擎对文本节点拆分敏感(2026-09-07实测侧栏菜单
    count恒为0但innerText有文本), locator失配时用evaluate按直接文本
    节点搜索最小可见元素并DOM点击(React合成事件响应, dispatch_event同机理)。"""
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        loc = page.locator(f"text={text}")
        if loc.count():
            try:
                loc.first.click(timeout=5_000)
                return
            except PWTimeout as e:
                last_err = e  # 被遮挡等, 走evaluate路径
        hit = page.evaluate("""(t) => {
            const els = Array.from(document.querySelectorAll(
                'a, button, li, div, span'));
            for (const e of els) {
                const own = Array.from(e.childNodes)
                    .filter(n => n.nodeType === 3)
                    .map(n => n.textContent).join('');
                if (!own.includes(t)) continue;
                const r = e.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) { e.click(); return true; }
            }
            return false;
        }""", text)
        if hit:
            return
        time.sleep(1)
    raise CertError(f"未找到可点击的[{text}]元素: {last_err or '定位失配'}")


def certify_page(page, profile: dict, *, log=None, stop_event=None,
                 state: dict | None = None, shoot=None,
                 institution: str = "") -> None:
    """在已登录page上完成认证表单填写+提交。不管理公司/机构名用量(调用方负责)。

    institution: 机构名称字段值, 必须来自机构名库(institutions.py 队列分配),
    不再使用企业名(2026-09-07用户要求: 一名一用)。

    填写策略(2026-09-07上传先行): ①四组文件先上传并排空OCR——空字段被静默
    回填, 不会触发"识别与填写不一致"冲突弹窗(企业证明→证件正反面→地址证明
    =证件正面→CP575); ②识别不覆盖的控件手动填
    (机构名/国家下拉/长期有效勾选/证件内容单选); ③其余文本/日期字段补录:
    识别已填的保留识别值(审核以证件图片为准), 只有仍为空的才填资料值。

    state["submitted"] 在点击提交前置True, 供调用方判断失败时是否回收公司。
    """
    log = log or _default_log
    name = profile["name"]
    if not institution:
        raise CertError("机构名为空: 认证表单机构名称必须来自机构名库(GUI②区导入)")

    # 会话恢复/注册结束的page已停在已登录/home时直接复用(免重复goto+
    # 全文稳定等待); 会话失效时/home渲染公开落地页且URL不变(2026-09-07实测)
    if not ensure_home(page, stop_event):
        raise CertError("登录会话失效")
    if registrar.dismiss_welcome_modal(page):
        log("  已关闭[开始使用]欢迎弹窗", "dim")
    click_text(page, "企业认证")
    time.sleep(3)
    wait_stable(page, 30, stop_event)
    add = page.locator("button", has_text="立即添加")
    if not add.count():
        raise AlreadyCertified("未找到[立即添加]按钮, 该账号可能已有认证记录")
    add.first.click()
    time.sleep(3)
    wait_stable(page, 40, stop_event)
    if not page.locator(_Form._wrap("institutionName")).count():
        raise CertError(f"认证表单未打开: {page.url}")
    log(f"  表单已打开, 开始填写: {name}")
    form = _Form(page, log)
    _check_stop(stop_event)

    # --- 上传先行(2026-09-07): 字段全空时OCR静默回填, 从根源消除
    # "预填文本→上传→识别→冲突弹窗"竞态; 冲突弹窗只在识别值与已填内容
    # 不一致时出现, 空字段=静默填充, 不弹窗 ---
    form.upload("enterpriseMaterialInfo.enterpriseQualification",
                [profile["biz_proof"]], "企业营业证明")
    form.wait_ocr_done("企业文件识别", stop_event=stop_event)
    form.upload("residentialMaterialInfo.identityDocuments",
                [profile["id_front"], profile["id_back"]], "身份证件(正反面)")
    form.wait_ocr_done("证件识别", stop_event=stop_event)
    # 地址证明=证件正面(美国驾照正面含居住地址, 与证件上传同图;
    # 选了"地址"单选后此文件与证件信息自洽)
    form.upload("residentialMaterialInfo.addressProof",
                [profile["id_front"]], "地址证明(证件正面)")
    form.wait_ocr_done("地址证明识别", stop_event=stop_event)
    form.upload("residentialMaterialInfo.representativeProof",
                [profile["cp575"]], "代表人证明(CP575)")
    form.wait_ocr_done("代表人证明识别", stop_event=stop_event)
    _check_stop(stop_event)

    # --- 识别不覆盖的控件(OCR只回填文本字段): 机构名/国家/长期有效/单选 ---
    form.fill_text("institutionName", institution, "机构名称")
    form.pick_country()
    if form._read_value(form._input("enterpriseMaterialInfo.expirationDate")):
        log("  营业执照有效期已由识别填写, 不勾选长期有效", "dim")
    else:
        form.check_box("enterpriseMaterialInfo.isLongTerm", "营业执照长期有效")
    form.pick_radio("residentialMaterialInfo.idContent", ID_CONTENT, "证件包含内容")

    # --- 补录: 识别没填的空字段才填, 已填的保留识别值(审核以证件图片为准) ---
    form.fill_text("enterpriseMaterialInfo.enterpriseNameLocal", name, "企业名称(当地语言)")
    form.fill_text("enterpriseMaterialInfo.enterpriseNameEn", name, "企业名称(英语)")
    form.fill_text("enterpriseMaterialInfo.registrationNumber", profile["reg_no"], "企业注册编号")
    form.fill_text("enterpriseMaterialInfo.registrationProvince", profile["reg_state"], "省/州")
    form.fill_text("enterpriseMaterialInfo.registrationCity", profile["reg_city"], "城市")
    form.fill_text("enterpriseMaterialInfo.registrationAddress", profile["reg_addr"], "企业注册地址")
    form.fill_text("residentialMaterialInfo.legalName", profile["rep_name"], "姓名(全名)")
    form.fill_date("residentialMaterialInfo.birthDate", profile["rep_birth"], "出生日期")
    form.fill_text("residentialMaterialInfo.idNumber", profile["id_no"], "证件号")
    form.fill_date("residentialMaterialInfo.idExpirationDate", profile["id_exp"], "证件有效期")
    _check_stop(stop_event)
    if shoot:
        shoot(page, "cert_filled")

    # --- 提交 ---
    # 等证件OCR识别完成(上传后站点异步识别; 完成时可能弹"识别结果与填写不一致"弹窗,
    # 弹窗不处理会拦截提交按钮点击)
    deadline = time.time() + 60
    recognized = False
    while time.time() < deadline:
        _check_stop(stop_event)
        if form._handle_ocr_conflict():
            continue
        if page.locator("text=正在智能识别").count() == 0:
            recognized = True
            break
        time.sleep(1)
    if recognized:
        log("  证件智能识别完成")
    else:
        log("  证件识别仍在进行, 继续提交", "warn")

    # 右下角悬浮球会拦截底部按钮点击: 注入样式表隐藏(React重渲染也不恢复),
    # 仍失败则降级为DOM事件点击
    form._clear_overlays()
    page.add_style_tag(content='[class*="floatinEntry"]{display:none !important;}')
    time.sleep(0.5)
    btn = page.locator("button", has_text="提交").first
    try:
        btn.click(timeout=10_000)
    except PWTimeout:
        log("  提交按钮被遮挡, 清障后改用DOM事件触发", "warn")
        form._clear_overlays()
        btn.dispatch_event("click")
    if state is not None:
        state["submitted"] = True  # 点击成功才算已提交(失败时调用方可回收公司)
    log("  已点击提交, 等待结果…")
    time.sleep(2)
    deadline = time.time() + 40
    while time.time() < deadline:
        if "/create" not in page.url:
            log(f"  已离开表单页: {page.url}", "ok")
            break
        if form._handle_ocr_conflict():
            continue  # 提交后弹出的识别冲突弹窗会拦着页面跳转
        confirm = page.locator(".semi-modal-content button", has_text="确")
        if confirm.count():
            try:
                confirm.first.click(timeout=3_000)
            except PWTimeout:
                confirm.first.dispatch_event("click")
            log("  已确认提交弹窗")
            continue
        errs = [e.strip() for e in
                page.locator(".semi-form-field-error-message").all_inner_texts() if e.strip()]
        if errs:
            raise CertError("表单校验未通过: " + "; ".join(errs)[:300])
        time.sleep(1)
    else:
        if shoot:
            shoot(page, "cert_submit_stuck")
        raise CertError(f"提交后仍停留在表单页: {page.url}")
    if shoot:
        shoot(page, "cert_submitted")


def certify_account(page, email: str, *, log=None, stop_event=None, shoot=None) -> str:
    """在已登录page上为email绑定一家可用公司+分配一个机构名并完成认证提交。

    自动维护公司用量(usage.json)、机构名用量(institutions.json)和号池认证状态。
    机构名从机构名库队列队首分配(一名一用): 未提交失败释放回队首复用,
    提交成功终态绑定; 已点提交未确认(failed)保持绑定防一名两用。
    返回公司名; 失败抛CertError/AlreadyCertified。
    """
    log = log or _default_log
    profile = materials.pick_company()
    if not profile:
        raise CertError("无可用公司资料(全部已绑定或资料不全, 见 资料/usage.json 与 overrides.json)")
    inst = institutions.allocate(email)
    if not inst:
        raise CertError("机构名库无可用机构名(请先在GUI②区导入; 每个机构名仅可使用一次)")
    inst_name = inst["name"]
    company = profile["name"]
    materials.mark_usage(company, email, "assigned")
    log(f"  绑定公司: {company}")
    log(f"  分配机构名: {inst_name}")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state: dict = {}
    try:
        certify_page(page, profile, log=log, stop_event=stop_event,
                     state=state, shoot=shoot, institution=inst_name)
    except AlreadyCertified:
        # 账号已有认证记录: 此前"结果未确认"的提交实际已生效, 修正记账
        materials.release_usage(company)
        institutions.release(inst_name)
        acc = next((a for a in pool.all_accounts() if a["email"] == email), {})
        prev = acc.get("cert_company")
        prev_inst = acc.get("institution")
        note = "检测到已有认证记录, 此前提交实际已生效"
        if prev:
            usage = materials.load_usage()
            ent = usage.get(prev)
            if ent and ent.get("email") == email and ent.get("status") != "submitted":
                ent.update(status="submitted", at=now, note=note)
                materials.save_usage(usage)
        if prev_inst:
            ent = institutions.find(prev_inst)
            if ent and ent.get("email") == email and ent.get("status") != "submitted":
                institutions.mark(prev_inst, email, "submitted", note=note)
        pool.update_account(email, cert_status="submitted", cert_company=prev,
                            institution=prev_inst, cert_at=now, cert_error=None)
        log(f"  {note}(公司: {prev or '未能确定'}, 机构名: {prev_inst or '未能确定'})", "warn")
        raise
    except Exception as e:
        if state.get("submitted"):
            materials.mark_usage(company, email, "failed", note=str(e)[:200])
            institutions.mark(inst_name, email, "failed", note=str(e)[:200])
            pool.update_account(email, cert_status="failed", cert_company=company,
                                institution=inst_name,
                                cert_at=now, cert_error=str(e)[:300])
            log(f"  已点击提交但结果未确认, 公司/机构名保持绑定: {company} / {inst_name}", "warn")
        else:
            materials.release_usage(company)
            institutions.release(inst_name)
            log(f"  未提交, 公司/机构名已释放可复用: {company} / {inst_name}", "dim")
        raise
    materials.mark_usage(company, email, "submitted")
    institutions.mark(inst_name, email, "submitted")
    pool.update_account(email, cert_status="submitted", cert_company=company,
                        institution=inst_name, cert_at=now, cert_error=None)
    return company


def open_session(browser, email: str, *, log=None, code_timeout: int = 180,
                 stop_event=None, shoot=None):
    """恢复auth登录会话; 失效则验证码重新登录(凭证自动刷新)。返回 (page, context)

    会话失效时/home渲染公开落地页而非重定向/login(2026-09-07实测),
    必须检查登录态侧栏标记, 否则后续流程会以超时失败。
    """
    log = log or _default_log
    auth_path = pool.AUTH_DIR / f"{email}.json"
    storage = None
    if auth_path.exists():
        try:
            state = json.loads(auth_path.read_text(encoding="utf-8"))
            storage = {"cookies": state.get("cookies", []),
                       "origins": state.get("origins", [])}
        except (json.JSONDecodeError, OSError):
            storage = None
    if storage is None:
        log("  无本地登录凭证, 将验证码登录", "warn")
    context = browser.new_context(
        locale="zh-CN", timezone_id="Asia/Shanghai",
        viewport={"width": 1380, "height": 900}, user_agent=UA,
        storage_state=storage)
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    page = context.new_page()
    if not ensure_home(page, stop_event):
        log("  登录会话已失效, 验证码重新登录…", "warn")
        registrar.login_with_code(page, email, log=log,
                                  code_timeout=code_timeout,
                                  stop_event=stop_event, shoot=shoot)
    return page, context


class Certifier:
    """批量为已注册账号做企业认证(独立模式: 每账号恢复会话, 过期自动验证码重登)"""

    def __init__(self, accounts: list[dict], headless: bool = False,
                 log=None, progress=None, code_timeout: int = 180,
                 stop_event: Event | None = None):
        self.accounts = accounts
        self.headless = headless
        self.log = log or _default_log
        self.progress = progress or (lambda done, total: None)
        self.code_timeout = code_timeout
        self.stop_event = stop_event or Event()

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

    def _open_session(self, browser, email: str):
        return open_session(browser, email, log=self.log,
                            code_timeout=self.code_timeout,
                            stop_event=self.stop_event, shoot=self._shoot)

    def run(self) -> tuple[int, int, int]:
        """串行认证所有账号, 返回 (成功, 失败, 跳过)"""
        total = len(self.accounts)
        ok = fail = skip = 0
        self.log(f"开始企业认证 {total} 个账号")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
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
                        page, context = self._open_session(browser, email)
                        company = certify_account(
                            page, email, log=self.log,
                            stop_event=self.stop_event, shoot=self._shoot)
                        ok += 1
                        self.log(f"[{i}/{total}] >>> {email} 认证提交成功 ({company})", "ok")
                    except AlreadyCertified as e:
                        skip += 1
                        self.log(f"[{i}/{total}] >>> {email} 跳过: {e}", "warn")
                    except Exception as e:
                        if self._stopped():
                            self.log(f"[{i}/{total}] 已手动停止", "warn")
                        else:
                            fail += 1
                            self.log(f"[{i}/{total}] >>> {email} 失败: {e}", "err")
                            if page:
                                self._shoot(page, f"cert_error_{email.split('@')[0]}")
                    finally:
                        if context:
                            context.close()
            finally:
                browser.close()
        self.progress(total, total)
        return ok, fail, skip
