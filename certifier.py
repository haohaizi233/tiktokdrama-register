# -*- coding: utf-8 -*-
"""企业认证引擎: 恢复登录会话 → 企业认证表单 → 两段式填写(见certify_page) → 提交

- certify_account(): 在已登录page上绑定一家公司并完成认证(GUI注册后钩子/独立模式共用);
  该邮箱已有 assigned/failed 绑定则续上, 不新占槽位/机构名
- certify_page(): 纯表单操作(导航+填表+上传+提交), 不管理公司用量
- Certifier: 批量为已注册账号做认证(独立浏览器+会话恢复/过期重登)
- 公司资料来自 materials.py, 槽位制默认每企业5号(可随政策改), usage.json 跟踪各槽绑定
- 机构名称来自 institutions.py 机构名库队列(一名一用, 不再使用企业名)
- 认证结果写回号池: cert_status / cert_company / cert_at / cert_error

填写策略:
① 先上传除证件正反面外的三组文件(企业证明PDF→地址证明→代表人证明), 企业字段靠PDF识别
   静默回填; ② 补填机构名/国家等。代表人4字段: 表格(xlsx F~I)有值才先填, 随后上传证件
   冲突「保留当前填写」; 表格没有身份证信息则不预填, 上传后以站点识别为准(冲突「应用识别」)。
   企业字段仅在识别没填时用资料兜底; ③ 最后上传证件正反面。

表单结构(2026-09-06 实测 /cooperation/enterprise/create, Semi Design控件):
- 机构信息: institutionName(文本)
- 企业信息: enterpriseQualification(上传·单文件=州注册证明 / 中国英译执照)
  enterpriseNameLocal/En, registrationNumber, registrationCountry(下拉=美国/中国)
  registrationProvince/City/Address(文本), isLongTerm(勾选长期→免填expirationDate)
- 代表人: identityDocuments(上传·多文件=证件正反面), legalName, birthDate(日期控件)
  idNumber, idExpirationDate(日期控件)+isIdLongTerm(勾选长期), idContent(单选: 国籍/地址/两者都有→驾照与身份证正面均含住址, 选"地址")
  addressProof(上传=证件正面), representativeProof(上传=CP575 或中国英译执照)

字段ID已2026-09-07在线dump逐一核对(19个字段全吻合, 含residentialMaterialInfo.addressProof)
"""
import json
import time
from datetime import datetime
from pathlib import Path
from threading import Event

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import hubstudio
import institutions
import materials
import pool
import registrar

HOME_URL = "https://www.tiktokdramacenter.com/home"
# 认证表单全新实例入口(不带申请单ID; 带ID=create/7683889452686890002形
# 式会恢复旧申请单草稿, 2026-09-10用户实测, 换公司重跑必须用不带ID的)
CREATE_URL = "https://www.tiktokdramacenter.com/cooperation/enterprise/create/"
UA = registrar.UA
COUNTRY = "美国"
ID_CONTENT = "地址"  # 美国驾照正面 / 中国身份证正面均含住址


class CertError(RuntimeError):
    """认证失败(可重试)"""


class AlreadyCertified(CertError):
    """该账号已有认证记录, 无需重复提交。

    inst_on_page: 认证管理页header显示的当前生效机构名(2026-09-10
    实测截图), 无认证记录场景为空; 调用方用于号池institution对齐
    (服务端真相优先)。"""

    def __init__(self, detail: str, inst_on_page: str = ""):
        super().__init__(detail)
        self.inst_on_page = inst_on_page


class CompanyDuplicateError(CertError):
    """服务端拒绝: 该企业已重复认证不可再发起(2026-09-10实测toast)。

    该执照在服务端侧已无可用额度(本地槽位只统计本工具用量, 会低估),
    调用方应拉黑该公司并换下一家重跑整个表单。"""


class UploadTimeoutError(CertError):
    """上传时前端60s超时toast(2026-09-10用户实测, 英文如timeout/60s类,
    具体文案未记全——用关键词匹配)。文件没传上去, 需goto不带申请单ID
    的create/全新表单重新上传该公司全部资料。区别于服务端拒绝(有
    code/message): 这里是网络/前端超时, 公司与机构名绑定均有效,
    同一组公司/机构名重跑即可(≤3次)。"""


_UPLOAD_TIMEOUT_KW = ("timeout", "time out", "60s")


def _is_upload_timeout_toast(toast: str) -> bool:
    """英文上传超时toast短词匹配(用户原话: '类似time out具体准确的
    怎么写我忘了, 反正就是英文的60s超时之类的, 你短词匹配下吧')——
    文案记不全, 多关键词小写包含匹配兜住。"""
    t = (toast or "").lower()
    return any(k in t for k in _UPLOAD_TIMEOUT_KW)


def _toast_portal_text(page) -> str:
    """读tux顶部toast的完整文本(2026-09-10用户MutationObserver实测DOM:
    div[data-tux-toast-portal='top'] > [data-tux-config-provider] >
    toast-view(--enter/--leave动画态均可见), 文本在truncate的
    span[title]里。toast只显示约2-3秒, 提交后必须高频轮询(用户实测
    两种toast均出现在点提交后很短时间): ①「机构名称已存在，请重新命名」
    ②「识别到当前企业已重复认证，不可再发起」。与semi-toast(.semi-toast-
    content)是两套组件, 上传失败toast走semi, 提交拒绝toast走tux portal。"""
    try:
        parts = page.locator(
            '[data-tux-toast-portal] span[title]').all_inner_texts()
        return " ".join(t.strip() for t in parts if t.strip())
    except Exception:
        return ""


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
        # 识别冲突决策: False=上传阶段(字段尚空, 应用识别结果); True=人工补填后
        # (证件照片识别不准, 代表人字段以表格录入为准, 一律保留当前填写)
        self.conflict_keep = False

    @staticmethod
    def _wrap(fid: str) -> str:
        return f"div.semi-form-field[x-field-id='{fid}']"

    def _input(self, fid: str):
        return self.page.locator(
            f"{self._wrap(fid)} input:not([type=file]):not([type=hidden]), "
            f"{self._wrap(fid)} textarea").first

    def _handle_ocr_conflict(self) -> bool:
        """证件OCR识别冲突弹窗处理, 返回是否遇到弹窗(绝不因弹窗自身消失而抛错)。

        - conflict_keep=True(人工补填后): 一律点"保留当前填写"——业务规则
          (2026-09-08)证件照片识别不准, 已填内容以人工录入值为准
        - conflict_keep=False(上传阶段, 字段尚空): 有冲突行(含"→"箭头)点
          "应用识别结果"; 无冲突行的空壳弹窗(应用后收起动画期间会以空字段列表
          短暂重现, 实测)点"保留当前填写"安全关闭
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
        if self.conflict_keep:
            self.log(f"  识别冲突弹窗, 保留当前填写(人工录入优先): {body}", "warn")
            target = self.page.locator(
                ".semi-modal-content button", has_text="保留当前填写").last
        elif "→" in body:  # 冲突行形如 "当前填写 X → 识别结果 Y"
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

    def fill_text(self, fid: str, value, label: str, *, force: bool = False) -> None:
        """文本字段填写。

        - value为空: 跳过(5列精简表无该列, 依赖识别回填)
        - force=False(企业字段): 识别已回填则保留识别值(PDF识别准, 审核对照原文),
          仅识别没填才填表格值(旧表兜底)
        - force=True(代表人字段): 表格录入为准, 识别回填的杂值强制覆盖(照片识别不准)
        """
        self._clear_overlays()
        value = str(value or "").strip()
        if not value:
            self.log(f"  {label}: 表格未提供, 依赖识别回填", "dim")
            return
        loc = self._input(fid)
        loc.scroll_into_view_if_needed(timeout=10_000)
        cur = self._read_value(loc)
        if cur == value:
            self.log(f"  {label}: 已是目标值, 跳过", "dim")
            return
        if cur and not force:
            self.log(f"  {label}: 识别已填[{cur[:40]}](资料[{value[:40]}]), 保留识别值", "warn")
            return
        if cur:
            self.log(f"  {label}: 识别值[{cur[:40]}]与表格不符, 以表格录入为准", "warn")
        self._click(loc, label)
        loc.fill(value)
        time.sleep(0.4)
        # OCR冲突弹窗若恰在此刻弹出: keep模式点保留当前填写后值仍是刚填的表格值
        conflicted = self._handle_ocr_conflict()
        time.sleep(0.4)
        got = loc.input_value()
        if got != value:
            if conflicted and not self.conflict_keep and got:
                self.log(f"  {label}: 识别覆盖 [{value[:40]}]→[{got[:40]}]", "warn")
                return
            if conflicted and not got:
                loc.fill(value)  # 应用识别后字段短暂为空, 回填
                got = loc.input_value()
            if got != value:
                # 被输入框maxlength截断时给出精确原因(而非笼统"填写不符")
                try:
                    ml = int(loc.get_attribute("maxlength") or 0)
                except Exception:
                    ml = 0
                if ml and got == value[:ml]:
                    raise CertError(
                        f"[{label}] 超出输入框上限{ml}字符被截断: "
                        f"[{value}]({len(value)}字符)——该值不可用, 需更换")
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
            if got == name or name in got:
                self.log(f"  国家/地区: {got}", "dim")
                return
            if attempt == 1:
                self.log(f"  [国家/地区] 选择未生效(实际[{got}]), 重试", "warn")
                self.page.keyboard.press("Escape")
                time.sleep(0.5)
        raise CertError(f"[国家/地区] 选择不符: 期望[{name}] 实际[{got}]")

    def fill_date(self, fid: str, value, label: str) -> None:
        """日期控件填写(代表人字段专用, 表格录入为准: 识别已填但与表格不符时强制覆盖)。

        value为空则跳过(依赖识别回填)。竞态自愈: OCR冲突弹窗可能在打开面板/键入/
        确认任一环节弹出, 与日期面板叠成双层遮罩——每环节检测处理+整轮重试。
        """
        value = str(value or "").strip()
        if not value:
            self.log(f"  {label}: 表格未提供, 依赖识别回填", "dim")
            return
        self._clear_overlays()
        loc = self._input(fid)
        loc.scroll_into_view_if_needed(timeout=10_000)
        cur = self._read_value(loc)
        if cur and (value in cur or cur == value.replace("-", "/")):
            self.log(f"  {label}: 已是目标值, 跳过", "dim")
            return
        if cur:
            self.log(f"  {label}: 识别值[{cur}]与表格[{value}]不符, 以表格录入为准", "warn")
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
            # 全选清除识别残留值, 再键入表格值(强制覆盖)
            self.page.keyboard.press("Control+a")
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
            if conflicted and got and not self.conflict_keep:
                # 上传阶段识别结果覆盖了键入值(以识别为准), 不视为失败
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

        失败诊断(2026-09-09前端源码实证+实际失败DOM): 上传走 POST
        /api/content-partner/enterprise/upload-file, 服务端拒绝时响应
        {code:非0, message:原因} 但前端只显示统一的「文件上传失败」,
        message不显示——监听响应在引擎侧直接读出真实原因(如执照认证
        达上限)。失败DOM特征(2026-09-09实测截图): ①内层88x88 div带
        inline border 'semi-color-danger'(注意danger在内层框不在瓦片
        外层div上) ②瓦片下方持久错误文本span[style*='ui-text-danger']
        (如「文件上传失败」) ③中央refresh重试按钮。本地校验失败
        (>10MB/图片短边<480px/图片损坏)不发请求, 瓦片闪现即自动移除
        (autoRemove), 靠toast文本识别。
        """
        self._clear_overlays()
        wrap = self._wrap(fid)
        items = f"{wrap} .semi-upload-file-list-main > div:not(.semi-upload-add)"
        prog = f"{wrap} .semi-progress-circle"
        fail_tile = f"{items} > div[style*='danger']"  # 内层88x88框带danger红边
        responses: list = []

        def _on_resp(resp):
            try:  # 事件回调里禁止调用playwright API, 只存引用
                if "/api/content-partner/enterprise/upload-file" in resp.url:
                    responses.append(resp)
            except Exception:
                pass

        def _drain():
            """读新到的上传响应 → [(code, message, fileId)]; body未就绪会等"""
            out = []
            while responses:
                try:
                    body = responses.pop(0).json()
                except Exception:
                    continue
                data = body.get("data") or {}
                out.append((body.get("code"), body.get("message") or "",
                            data.get("fileId")))
            return out

        def _server_reject(code, msg):
            return (f"服务端拒绝: code={code} message=[{msg}]"
                    + ("(若为执照/认证数量上限, 请更换企业资料)" if "上限" in msg else ""))

        def _toast_text():
            try:
                t = self.page.locator(".semi-toast-content")
                return t.first.inner_text(timeout=2_000).strip() if t.count() else ""
            except Exception:
                return ""

        def _tile_err():
            """瓦片下方持久错误文本(失败时一直在, 比一闪而过的toast可靠);
            服务端拒绝=「文件上传失败」, 本地校验拒绝=具体文案"""
            try:
                s = self.page.locator(f"{items} span[style*='ui-text-danger']")
                return s.first.inner_text(timeout=2_000).strip() if s.count() else ""
            except Exception:
                return ""

        self.page.on("response", _on_resp)
        try:
            for raw in paths:
                path = Path(raw)
                if not path.exists():
                    raise CertError(f"[{label}] 文件不存在: {path}")
                before = self.page.locator(items).count()
                err = None
                # 失败自动重试1次(共2次尝试): 瓦片中央重试箭头(32x32 div,
                # onClick=onRetry)重新触发上传; 「上限」类拒绝重试必失败,
                # 无重试价值, 直接放弃(交给上层拉黑/抛错)
                for attempt in (1, 2):
                    if attempt == 1:
                        self.page.locator(f"{wrap} input[type=file]").first.set_input_files(str(path))
                        deadline = time.time() + 20
                        while time.time() < deadline:
                            time.sleep(0.5)
                            if self.page.locator(items).count() > before:
                                break
                        else:
                            toast = _toast_text()
                            raise CertError(
                                f"[{label}] 上传未开始: {path.name}"
                                + (f"(本地校验拒绝: {toast})" if toast else
                                   "(疑似本地校验拒绝: >10MB / 图片短边<480px / 文件损坏)"))
                    else:
                        if "上限" in (err or ""):
                            break
                        btn = self.page.locator(f"{items} .semi-icon-refresh")
                        if not btn.count():
                            break
                        self.log(f"  {label}: 上传失败[{err[:60]}], "
                                 "点瓦片重试箭头再试一次", "warn")
                        self._clear_overlays()
                        btn.first.click(timeout=5_000)
                        time.sleep(1)
                    deadline = time.time() + 180
                    err = None
                    done = False
                    while time.time() < deadline:
                        time.sleep(1)
                        # 前端60s超时toast(2026-09-10用户实测, 英文timeout/
                        # 60s类, 一闪而过): 文件没传上去, 须goto全新表单
                        # 整组重传(瓦片红框检测之前判断, 超时语义优先)
                        tux = _toast_portal_text(self.page)
                        if tux and _is_upload_timeout_toast(tux):
                            raise UploadTimeoutError(
                                f"[{label}] 上传超时(前端超时toast): {tux[:80]}")
                        for code, msg, fid_val in _drain():
                            if fid_val:
                                self.log(f"  {label}: 服务端已受理 fileId={fid_val}", "dim")
                            elif code not in (None, 0):
                                err = _server_reject(code, msg)
                        if err:
                            break
                        if self.page.locator(fail_tile).count():
                            time.sleep(1)  # 红框瓦片已现, 响应可能略晚到
                            for code, msg, fid_val in _drain():
                                if code not in (None, 0):
                                    err = _server_reject(code, msg)
                            if not err:
                                hint = _tile_err() or _toast_text()
                                err = (f"前端显示上传失败(红框瓦片)"
                                       + (f": {hint}" if hint else ", 未见服务端响应"))
                            break
                        if self.page.locator(prog).count() == 0:
                            time.sleep(1)  # 进度圈刚消失, 响应可能略晚到
                            for code, msg, fid_val in _drain():
                                if fid_val:
                                    self.log(f"  {label}: 服务端已受理 fileId={fid_val}", "dim")
                                elif code not in (None, 0):
                                    err = _server_reject(code, msg)
                            done = True
                            break
                    if err is None and done:
                        break
                    if err is None:
                        err = "上传超时未完成"
                if err:
                    raise CertError(f"[{label}] 上传失败 {path.name}: {err}")
                if self.page.locator(items).count() <= before:
                    toast = _toast_text()
                    raise CertError(
                        f"[{label}] 上传后项目消失: {path.name}"
                        + (f"(本地校验拒绝: {toast})" if toast else
                           "(疑似本地校验拒绝: >10MB / 图片短边<480px / 文件损坏)"))
                time.sleep(0.8)
                self.log(f"  {label}: 已上传 {path.name}", "dim")
        finally:
            try:
                self.page.remove_listener("response", _on_resp)
            except Exception:
                pass


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


def _read_mgmt_institution(page) -> str:
    """读认证管理页header的当前生效机构名(2026-09-10实测DOM:
    div.headerLeft > span[data-testid='tux-web-text']为机构名标题,
    同容器内是「解散机构」按钮)。以解散机构按钮为锚向上找容器,
    读不到返回空串(不影响检测判定, 只是少了号池对齐素材)。"""
    try:
        return (page.evaluate("""() => {
            const btns = Array.from(
                document.querySelectorAll('button')).filter(
                b => (b.innerText || '').includes('解散机构'));
            for (const b of btns) {
                let el = b.parentElement;
                for (let i = 0; i < 3 && el; i++, el = el.parentElement) {
                    const spans = el.querySelectorAll(
                        'span[data-testid="tux-web-text"]');
                    if (spans.length) {
                        const t = (spans[0].innerText || '').trim();
                        if (t && t !== '解散机构') return t;
                    }
                }
            }
            return '';
        }""") or "").strip()
    except Exception:
        return ""


def certify_page(page, profile: dict, *, log=None, stop_event=None,
                 state: dict | None = None, shoot=None,
                 institution: str = "", email: str = "",
                 on_form: bool = False) -> None:
    """在已登录page上完成认证表单填写+提交。不管理公司/机构名用量(调用方负责)。

    institution: 机构名称字段值, 必须来自机构名库(institutions.py 队列分配),
    不再使用企业名(2026-09-07用户要求: 一名一用)。
    email: 提交后toast「机构名称已存在，请重新命名」时用于换名重新分配
    (2026-09-10实测): 废弃旧名(discard终态)+allocate下一个+只改机构名框
    重新提交, 其他已填字段与已上传文件不动。空=无法换名直接报错。
    on_form: 页面已停在全新认证表单(调用方场景「企业已重复认证」后goto了
    不带申请单ID的create/), 跳过侧栏导航直接填写。
    服务端toast「识别到当前企业已重复认证，不可再发起」时抛
    CompanyDuplicateError, 由调用方(certify_account)拉黑公司+goto全新表单
    换下一家重跑。

    填写策略(2026-09-08两段式, 业务规则: PDF识别准/证件照片识别不准/冲突以人工录入为准):
    ① 上传除证件正反面外的三组文件(企业证明PDF→地址证明=证件正面→CP575), 每组传完
      等OCR排空——企业字段由PDF识别静默回填(字段为空不弹冲突窗);
    ② 人工补填(此后所有识别冲突一律「保留当前填写」): 机构名/国家/长期有效/单选 +
      代表人4字段用表格值强制覆盖(照片OCR回填的杂值不信任) + 企业字段仅在识别
      没填时用表格值兜底(5列精简表无这些列则依赖识别, 识别也没填则报错);
    ③ 最后上传证件正反面, 等OCR排空(冲突弹窗保留当前填写)。

    state["submitted"] 在点击提交前置True, 供调用方判断失败时是否回收公司。
    """
    log = log or _default_log
    name = profile["name"]
    if not institution:
        raise CertError("机构名为空: 认证表单机构名称必须来自机构名库(GUI②区导入)")
    if len(institution) > institutions.MAX_LEN:
        # 表单输入框 maxLength=30(2026-09-09前端源码实证), 超长被浏览器
        # 截断, 填写校验必失败; 提前在入口拦截, 免开表单白跑一趟
        raise CertError(
            f"机构名超长: [{institution}]({len(institution)}字符) > "
            f"{institutions.MAX_LEN}字符(表单输入框上限), 请在GUI②区清洗机构名库")

    if on_form:
        # 换公司重跑入口(2026-09-10用户实测): 场景「企业已重复认证」后已
        # goto到不带申请单ID的create/——带ID刷新会恢复旧申请单草稿(用户
        # 实测URL create/7683889452686890002), 不带ID的create/才是除机构名
        # 外全空的全新表单。页面已在表单上, 跳过侧栏导航直接填写
        log("  已在全新表单页(换公司重跑), 跳过导航直接填写", "dim")
    else:
        # 会话恢复/注册结束的page已停在已登录/home时直接复用(免重复goto+
        # 全文稳定等待); 会话失效时/home渲染公开落地页且URL不变(2026-09-07实测)
        if not ensure_home(page, stop_event):
            raise CertError("登录会话失效")
        if registrar.dismiss_welcome_modal(page):
            log("  已关闭[开始使用]欢迎弹窗", "dim")
        click_text(page, "企业认证")
        time.sleep(3)
        wait_stable(page, 30, stop_event)
        # 已提交过认证的账号进来的是管理页(2026-09-10用户实测截图/DOM):
        # 机构名标题+「解散机构」按钮+筛选tabs(全部通过/企业资质异常/…)+
        # 「认证新企业」按钮+「企业资质正在审核中」提示; 新账号进的是
        # 引导页(立即添加按钮)。先精确识别管理页, 不再靠"没有立即添加
        # 按钮"猜测(页面加载失败也会走到那, 语义完全不同)
        if page.locator("button", has_text="认证新企业").count() \
                or page.locator("button", has_text="解散机构").count():
            inst_on_page = _read_mgmt_institution(page)
            raise AlreadyCertified(
                "检测到企业认证管理页, 该账号之前已提交过认证"
                + (f"(页面显示机构: {inst_on_page})" if inst_on_page else ""),
                inst_on_page=inst_on_page)
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

    cn = profile.get("workflow") == "cn"
    license_pdf = profile["biz_proof"]
    rep_proof = profile.get("cp575") or license_pdf
    id_front = profile["id_front"]
    id_back = profile["id_back"]
    country = "中国" if cn else COUNTRY
    preset = materials.has_id_preset(profile)
    if cn:
        log("  中国工作流: 英译执照作企业/代表人证明"
            + ("，代表人按表格先填" if preset else "，代表人无表格值, 上传后以站点识别为准"),
            "dim")

    # --- ① 上传先行(除证件正反面): 企业字段靠PDF识别静默回填(字段为空不弹冲突窗) ---
    form.upload("enterpriseMaterialInfo.enterpriseQualification",
                [license_pdf], "企业营业证明")
    form.wait_ocr_done("企业文件识别", stop_event=stop_event)
    # 地址证明: 美国驾照正面 / 中国身份证正面(含住址)
    form.upload("residentialMaterialInfo.addressProof",
                [id_front], "地址证明(证件正面)")
    form.wait_ocr_done("地址证明识别", stop_event=stop_event)
    form.upload("residentialMaterialInfo.representativeProof",
                [rep_proof], "代表人证明(英译执照)" if cn else "代表人证明(CP575)")
    form.wait_ocr_done("代表人证明识别", stop_event=stop_event)
    _check_stop(stop_event)

    # --- ② 补填: 有表格身份证才预填代表人; 企业字段识别优先、资料兜底 ---
    form.conflict_keep = preset
    # force覆盖: 换公司重跑时机构名框残留上一轮旧名(goto不带ID的create/
    # 后仅机构名不清空, 2026-09-10用户实测), 库分配名优先于页面残留
    form.fill_text("institutionName", institution, "机构名称", force=True)
    form.pick_country(country)
    if form._read_value(form._input("enterpriseMaterialInfo.expirationDate")):
        log("  营业执照有效期已由识别填写, 不勾选长期有效", "dim")
    else:
        form.check_box("enterpriseMaterialInfo.isLongTerm", "营业执照长期有效")
    form.pick_radio("residentialMaterialInfo.idContent", ID_CONTENT, "证件包含内容")
    if cn:
        local_name = profile.get("name_zh") or name
        en_name = profile.get("name_en") or ""
        form.fill_text("enterpriseMaterialInfo.enterpriseNameLocal", local_name, "企业名称(当地语言)")
        form.fill_text("enterpriseMaterialInfo.enterpriseNameEn", en_name, "企业名称(英语)")
        form.fill_text("enterpriseMaterialInfo.registrationNumber", profile.get("reg_no"), "企业注册编号")
        form.fill_text("enterpriseMaterialInfo.registrationProvince", profile.get("reg_state"), "省/州")
        form.fill_text("enterpriseMaterialInfo.registrationCity", profile.get("reg_city"), "城市")
        form.fill_text("enterpriseMaterialInfo.registrationAddress", profile.get("reg_addr"), "企业注册地址")
    else:
        form.fill_text("enterpriseMaterialInfo.enterpriseNameLocal", name, "企业名称(当地语言)")
        form.fill_text("enterpriseMaterialInfo.enterpriseNameEn", name, "企业名称(英语)")
        form.fill_text("enterpriseMaterialInfo.registrationNumber", profile["reg_no"], "企业注册编号")
        form.fill_text("enterpriseMaterialInfo.registrationProvince", profile["reg_state"], "省/州")
        form.fill_text("enterpriseMaterialInfo.registrationCity", profile["reg_city"], "城市")
        form.fill_text("enterpriseMaterialInfo.registrationAddress", profile["reg_addr"], "企业注册地址")
    if preset:
        log("  代表人: 表格有身份证信息, 先填再上传, 冲突保留已填", "dim")
        form.fill_text("residentialMaterialInfo.legalName", profile.get("rep_name"),
                       "姓名(全名)", force=True)
        form.fill_date("residentialMaterialInfo.birthDate", profile.get("rep_birth"), "出生日期")
        form.fill_text("residentialMaterialInfo.idNumber", profile.get("id_no"),
                       "证件号", force=True)
        if profile.get("id_long_term"):
            form.check_box("residentialMaterialInfo.isIdLongTerm", "证件长期有效")
        else:
            form.fill_date("residentialMaterialInfo.idExpirationDate",
                           profile.get("id_exp"), "证件有效期")
    else:
        log("  代表人: 表格无身份证信息, 上传证件后以站点识别为准", "dim")
    # 企业必填字段核对: PDF识别未回填且表格未提供时提前报错(比提交时表单校验
    # 报错更可诊断; 该PDF的识别有问题, 需人工核查该份资料)
    _ENT_REQUIRED = (
        ("institutionName", "机构名称"),
        ("enterpriseMaterialInfo.enterpriseNameLocal", "企业名称(当地语言)"),
        ("enterpriseMaterialInfo.enterpriseNameEn", "企业名称(英语)"),
        ("enterpriseMaterialInfo.registrationNumber", "企业注册编号"),
        ("enterpriseMaterialInfo.registrationProvince", "省/州"),
        ("enterpriseMaterialInfo.registrationCity", "城市"),
        ("enterpriseMaterialInfo.registrationAddress", "企业注册地址"),
    )
    missing = [lbl for fid, lbl in _ENT_REQUIRED
               if not form._read_value(form._input(fid))]
    if missing:
        raise CertError("企业字段识别未回填且表格未提供: " + "、".join(missing))
    _check_stop(stop_event)

    # --- ③ 证件正反面最后上传 ---
    # 有表格值: 冲突「保留当前填写」; 无表格值: 冲突「应用识别结果」
    form.upload("residentialMaterialInfo.identityDocuments",
                [id_front, id_back], "身份证件(正反面)")
    form.wait_ocr_done("证件识别", stop_event=stop_event)
    _check_stop(stop_event)
    _ID_REQUIRED = [
        ("residentialMaterialInfo.legalName", "姓名(全名)"),
        ("residentialMaterialInfo.idNumber", "证件号"),
        ("residentialMaterialInfo.birthDate", "出生日期"),
    ]
    missing_id = [lbl for fid, lbl in _ID_REQUIRED
                  if not form._read_value(form._input(fid))]
    exp = form._read_value(form._input("residentialMaterialInfo.idExpirationDate"))
    long_cls = ""
    try:
        long_cls = form.page.locator(
            _css_id("residentialMaterialInfo.isIdLongTerm")).get_attribute("class") or ""
    except Exception:
        long_cls = ""
    if not exp and "semi-checkbox-checked" not in long_cls:
        missing_id.append("证件有效期")
    if missing_id:
        raise CertError(("身份证识别未回填: " if not preset else "证件字段未填: ")
                        + "、".join(missing_id))
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
    # 提交拒绝toast只显示约2-3秒且出现在点击后很短时间(2026-09-10实测):
    # 不做前置sleep, 0.5s高频轮询防错过
    deadline = time.time() + 40
    name_swaps = 0
    submit_errs = 0  # 提交报错(错误/500类toast)重试次数
    while time.time() < deadline:
        _check_stop(stop_event)
        toast = _toast_portal_text(page)
        if "已重复认证" in toast or "不可再发起" in toast:
            raise CompanyDuplicateError(
                f"服务端拒绝: {toast[:80]}(该企业已重复认证, 需换下一家公司)")
        if "机构名称已存在" in toast:
            # 换名重提: 废弃旧名(discard终态, 服务端已占用永不可复用)+
            # allocate下一个+只清填机构名框重新提交, 其他字段/已传文件不动
            name_swaps += 1
            if not email or name_swaps > 10:
                raise CertError(
                    f"机构名重复且无法换名(已试{name_swaps}次): {toast[:60]}")
            institutions.discard(institution, note="服务端toast: 机构名称已存在")
            nxt = institutions.allocate(email)
            if not nxt:
                raise CertError(
                    f"机构名[{institution}]重复且库中已无可用机构名")
            institution = nxt["name"]
            if state is not None:
                state["institution_final"] = institution
            log(f"  机构名已被占用, 废弃换[{institution}]重新提交"
                f"(第{name_swaps}次换名)", "warn")
            form.fill_text("institutionName", institution, "机构名称", force=True)
            form._clear_overlays()
            btn = page.locator("button", has_text="提交").first
            try:
                btn.click(timeout=10_000)
            except PWTimeout:
                btn.dispatch_event("click")
            time.sleep(3)  # 旧toast(leave动画)消散, 防止残留文本被再次读到
            deadline = time.time() + 40  # 重新提交重新计时
            continue
        if toast and ("错误" in toast or "500" in toast):
            # 提交报错(2026-09-10用户实测: 含「错误」两字或「500」的服务端
            # 报错, 同款tux toast, 一闪而过): 表单数据仍在, 重试点提交即可,
            # 最多10次; 区别于换名/换公司场景(那些是确定性拒绝, 重试无用)
            submit_errs += 1
            if submit_errs > 10:
                raise CertError(
                    f"提交连续报错(已重试{submit_errs - 1}次仍失败): {toast[:80]}")
            log(f"  提交报错[{toast[:60]}], 重试第{submit_errs}次(最多10次)", "warn")
            time.sleep(3)  # toast消散, 防止残留文本被再次读到
            form._clear_overlays()
            btn = page.locator("button", has_text="提交").first
            try:
                btn.click(timeout=10_000)
            except PWTimeout:
                btn.dispatch_event("click")
            deadline = time.time() + 40  # 重新提交重新计时
            continue
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
        time.sleep(0.5)
    else:
        if shoot:
            shoot(page, "cert_submit_stuck")
        raise CertError(f"提交后仍停留在表单页: {page.url}")
    if shoot:
        shoot(page, "cert_submitted")


def certify_account(page, email: str, *, log=None, stop_event=None, shoot=None) -> str:
    """在已登录page上为email绑定一家可用公司+分配一个机构名并完成认证提交。

    自动维护公司用量(usage.json槽位制, 每企业 COMPANY_SLOTS 号)、机构名用量
    (institutions.json, 一名一用)和号池认证状态。
    机构名优先续上该邮箱未终态绑定, 否则从队列队首分配 unused。
    未提交失败释放回队首复用; 提交成功终态绑定;
    已点提交未确认(failed)保持绑定, 重试续上同一对公司/机构名(防一名两用、一号两槽)。
    返回公司名; 失败抛CertError/AlreadyCertified。
    """
    log = log or _default_log
    resume_slot = materials.find_open_slot(email)
    resume_inst = institutions.find_open(email)
    dup_swaps = 0  # 「企业已重复认证」换公司次数
    upload_timeouts = 0  # 上传前端60s超时重跑次数
    on_form = False  # 上一轮goto全新表单成功, 本轮跳过侧栏导航
    while True:
        try:
            from license_trans.pipeline import ensure_cn_reserve
            ensure_cn_reserve(log=log, stop_event=stop_event)
        except Exception as e:
            log(f"  预翻译中国执照失败: {e}", "warn")
        profile = materials.pick_company_for(email)
        if not profile:
            raise CertError("无可用公司槽位(全部用满或资料不全, 见 资料/usage.json)")
        if not profile.get("complete"):
            issues = "、".join(profile.get("issues") or [])
            raise CertError(
                f"续用上次绑定公司[{profile['name']}]但资料不完整({issues}), "
                "未改绑新公司(避免一号占两槽)")
        inst = institutions.allocate(email)
        if not inst:
            raise CertError("机构名库无可用机构名(请先在GUI②区导入; 每个机构名仅可使用一次)")
        inst_name = inst["name"]
        company = profile["name"]
        extras = materials.drop_extra_open_slots(email, company)
        if extras:
            log("  已回收该邮箱在其他公司的重复未终态占用: " + "、".join(extras), "warn")
        if resume_slot or resume_inst:
            log(f"  续用上次绑定: 公司[{company}] / 机构名[{inst_name}]", "warn")
        try:
            left = materials.mark_usage(company, email, "assigned")
        except materials.SlotFullError as e:
            if resume_inst and resume_inst.get("status") == "failed":
                institutions.mark(inst_name, email, "failed")
            else:
                institutions.release(inst_name)
            raise CertError(str(e)) from e
        log(f"  绑定公司: {company} (该企业剩余槽位 {left}/{materials.COMPANY_SLOTS})")
        log(f"  分配机构名: {inst_name}")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state: dict = {}
        try:
            certify_page(page, profile, log=log, stop_event=stop_event,
                         state=state, shoot=shoot, institution=inst_name,
                         email=email, on_form=on_form)
            on_form = False  # 正常路径进入, 下一账号重新导航
        except AlreadyCertified as e:
            note = "检测到已有认证记录, 此前提交实际已生效"
            # 管理页header读到的当前生效机构名=服务端真相, 号池institution
            # 与之冲突时以页面为准(如机构名库改名后提交的旧名/人工提交)
            inst_page = (getattr(e, "inst_on_page", "") or "").strip()
            final_inst = inst_page or state.get("institution_final") or inst_name
            if inst_page and resume_inst and resume_inst.get("name") != inst_page:
                log(f"  页面生效机构[{inst_page}]与号池记录"
                    f"[{resume_inst.get('name')}]不同, 号池已按页面更新", "warn")
            if resume_slot or resume_inst or state.get("submitted"):
                # 续上的就是那次未确认提交, 或本次已点提交: 把当前绑定记为成功
                materials.mark_usage(company, email, "submitted", note=note)
                institutions.mark(final_inst, email, "submitted", note=note)
                pool.update_account(email, cert_status="submitted", cert_company=company,
                                    institution=final_inst, cert_at=now,
                                    cert_error=None)
                log(f"  {note}(公司: {company}, 机构名: {final_inst})", "warn")
                raise
            # 新分配但平台已有认证: 退回新库存, 尽量对齐号池里记下的旧绑定
            materials.release_usage(company, email)
            institutions.release(final_inst)
            acc = next((a for a in pool.all_accounts() if a["email"] == email), {})
            prev = acc.get("cert_company")
            prev_inst = acc.get("institution")
            if prev:
                usage = materials.load_usage()
                for s in usage.get(prev) or []:
                    if s.get("email") == email and s.get("status") != "submitted":
                        s.update(status="submitted", at=now, note=note)
                materials.save_usage(usage)
            if prev_inst:
                ent = institutions.find(prev_inst)
                if ent and ent.get("email") == email and ent.get("status") != "submitted":
                    institutions.mark(prev_inst, email, "submitted", note=note)
            pool.update_account(email, cert_status="submitted", cert_company=prev,
                                institution=inst_page or prev_inst, cert_at=now,
                                cert_error=None)
            log(f"  {note}(公司: {prev or '未能确定'}, "
                f"机构名: {inst_page or prev_inst or '未能确定'})", "warn")
            raise
        except CompanyDuplicateError as e:
            # 服务端明确拒绝该执照(企业已重复认证不可再发起): 拉黑该公司
            # +释放本邮箱槽位+goto不带申请单ID的create/(2026-09-10用户实测:
            # 带ID刷新会恢复旧申请单草稿, 不带ID的create/才是除机构名外
            # 全空的全新表单), 顺延下一组企业重跑; 机构名未被服务端使用,
            # 下一轮allocate续用同一机构名
            dup_swaps += 1
            if dup_swaps > 6:
                raise CertError(
                    f"连续{dup_swaps - 1}家企业被服务端拒绝(重复认证), "
                    "中止本账号认证") from e
            materials.block_company(company, str(e)[:200])
            materials.release_usage(company, email)
            log(f"  [{company}] 服务端拒绝(企业已重复认证), 已拉黑并释放槽位, "
                f"打开全新表单换下一家企业重跑(第{dup_swaps}次)", "err")
            on_form = False
            try:
                page.goto(CREATE_URL)
                on_form = True
            except Exception:
                pass
            continue
        except UploadTimeoutError as e:
            # 上传前端60s超时(网络/服务端瞬断, 非服务端拒绝): 公司执照与
            # 机构名都没被服务端使用, goto全新表单**同一组**公司/机构名
            # 整体重跑(区别于重复认证=换公司), ≤3次
            upload_timeouts += 1
            if upload_timeouts > 3:
                # 超限按未提交处理绑定(原failed续上保持failed, 其余释放),
                # 与通用分支语义一致(2026-09-10)
                slot_keep = resume_slot and resume_slot[1].get("status") == "failed"
                if slot_keep:
                    materials.mark_usage(company, email, "failed", note=str(e)[:200])
                else:
                    materials.release_usage(company, email)
                inst_keep = resume_inst and resume_inst.get("status") == "failed"
                if inst_keep:
                    institutions.mark(inst_name, email, "failed", note=str(e)[:200])
                else:
                    institutions.release(inst_name)
                raise CertError(
                    f"上传超时已重跑{upload_timeouts - 1}次仍失败, "
                    "疑似网络/服务端持续异常") from e
            log(f"  上传超时, 打开全新表单整组重新上传({upload_timeouts}/3): "
                f"{str(e)[:80]}", "err")
            try:
                page.goto(CREATE_URL)
                on_form = True
            except Exception:
                pass
            continue
        except Exception as e:
            # 服务端以上限类message拒绝上传 → 该执照在服务端侧已无额度
            # (本地槽位只统计本工具用量, 可能低估), 拉黑该公司后续账号不再白试
            etxt = str(e)
            if "服务端拒绝" in etxt and "上限" in etxt:
                materials.block_company(company, etxt[:200])
                log(f"  该公司执照已达服务端认证上限, 已拉黑(blocked.json), "
                    f"后续账号自动换下一家: {company}", "err")
            final_inst = state.get("institution_final") or inst_name
            if state.get("submitted"):
                materials.mark_usage(company, email, "failed", note=str(e)[:200])
                institutions.mark(final_inst, email, "failed", note=str(e)[:200])
                pool.update_account(email, cert_status="failed", cert_company=company,
                                    institution=final_inst,
                                    cert_at=now, cert_error=str(e)[:300])
                log(f"  已点击提交但结果未确认, 公司/机构名保持绑定: "
                    f"{company} / {final_inst}", "warn")
            else:
                # 本次没点提交: 只有「原本就是 failed」的绑定才保持占用
                # (上次已点过提交, 可能平台已受理); 新占的 assigned 一律释放
                slot_keep = resume_slot and resume_slot[1].get("status") == "failed"
                inst_keep = resume_inst and resume_inst.get("status") == "failed"
                if slot_keep:
                    materials.mark_usage(company, email, "failed", note=str(e)[:200])
                else:
                    materials.release_usage(company, email)
                if inst_keep:
                    institutions.mark(final_inst, email, "failed", note=str(e)[:200])
                else:
                    institutions.release(final_inst)
                if slot_keep or inst_keep:
                    log(f"  未点到提交, 上次待核实绑定保持占用"
                        f"(公司={'保留' if slot_keep else '已释放'} / "
                        f"机构名={'保留' if inst_keep else '已释放'})", "warn")
                else:
                    log(f"  未提交, 公司/机构名已释放可复用: {company} / {final_inst}", "dim")
            raise
        # 成功: 机构名以实际提交的为准(提交中可能因重名换过名)
        final_inst = state.get("institution_final") or inst_name
        materials.mark_usage(company, email, "submitted")
        institutions.mark(final_inst, email, "submitted")
        pool.update_account(email, cert_status="submitted", cert_company=company,
                            institution=final_inst, cert_at=now, cert_error=None)
        return company


def open_session(browser, email: str, *, log=None, code_timeout: int = 180,
                 stop_event=None, shoot=None, use_hub: bool = False, chain=None):
    """恢复auth登录会话; 失效则验证码重新登录(凭证自动刷新)。返回 (page, context)

    会话失效时/home渲染公开落地页而非重定向/login(2026-09-07实测),
    必须检查登录态侧栏标记, 否则后续流程会以超时失败。
    use_hub=True时browser为HubStudio CDP连接, context不带指纹参数(环境承载)。
    chain=WindowChain时走单窗交接: 第1账号用环境自带窗(cookie清后注入凭证),
    之后每账号新窗+关上一窗; context=None表示自带窗(调用方勿close整个context)。
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
    if use_hub and chain is not None:
        page, context = chain.acquire(storage_state=storage)
    elif use_hub:
        context = hubstudio.new_context(browser, storage_state=storage)
        page = context.new_page()
    else:
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
                 stop_event: Event | None = None,
                 use_hubstudio: bool = False, rotate_ip: bool = False,
                 on_account=None):
        self.accounts = accounts
        self.headless = headless
        self.log = log or _default_log
        self.progress = progress or (lambda done, total: None)
        self.code_timeout = code_timeout
        self.stop_event = stop_event or Event()
        self.use_hub = use_hubstudio
        self.rotate_ip = rotate_ip and use_hubstudio
        self.on_account = on_account

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

    def _open_session(self, browser, email: str, chain=None):
        return open_session(browser, email, log=self.log,
                            code_timeout=self.code_timeout,
                            stop_event=self.stop_event, shoot=self._shoot,
                            use_hub=self.use_hub, chain=chain)

    def run(self) -> tuple[int, int, int]:
        """串行认证所有账号, 返回 (成功, 失败, 跳过)"""
        from license_trans.pipeline import reset_reserve_exhausted
        reset_reserve_exhausted()  # 新一轮认证: 重置预翻译耗尽标记
        total = len(self.accounts)
        ok = fail = skip = 0
        self.log(f"开始企业认证 {total} 个账号")
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
                    # 心跳: 每10个账号记一次时间戳, 崩溃时可定位最后存活点
                    if i % 10 == 0:
                        self.log(f"[heartbeat] 已处理 {i}/{total}, "
                                 f"成功 {ok} / 失败 {fail} / 跳过 {skip}", "dim")
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
                        page, context = self._open_session(browser, email, chain)
                        ip = (hubstudio.context_ip(page)
                              if self.use_hub and self.rotate_ip else None)
                        if ip:
                            self.log(f"  出口IP: {ip}", "dim")
                        company = certify_account(
                            page, email, log=self.log,
                            stop_event=self.stop_event, shoot=self._shoot)
                        ok += 1
                        if ip:
                            pool.update_account(email, last_ip=ip)
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
                        if context and not self.use_hub:
                            # Hub模式窗口由WindowChain交接(账号末不关: 零窗口会杀死浏览器)
                            context.close()
                        if self.on_account:
                            try:
                                self.on_account(email)
                            except Exception:
                                pass
            finally:
                if browser:
                    browser.close()
                if self.use_hub:
                    hubstudio.close_env(log=self.log)
        self.progress(total, total)
        return ok, fail, skip
