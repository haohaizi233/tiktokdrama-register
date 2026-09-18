# -*- coding: utf-8 -*-
"""图形界面: 两个业务页(Notebook 跳转)

短剧注册: ①导入账号 → ②导入机构名 → ③号池管理 → ④自动注册+企业认证+设置密码
执照翻译: 中文营业执照照片 → 固定英文模板 PDF

线程模型: 注册/认证在工作线程执行, 通过 queue 把日志/进度/完成事件发回主线程渲染。
企业认证: 注册后自动认证(同页面免重登) 或 独立批量认证已注册账号(会话恢复, 过期自动重登);
公司资料由 materials.py 解析(资料/文件夹), 槽位制默认每企业5个账号(可随政策改, 须确认), 用量自动跟踪;
机构名由 institutions.py 管理(institutions.json 队列), 一名一用, 认证时依次分配。
"""
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import certifier
import institutions
import materials
import password_setter
import pool
from license_gui import LicensePage
from registrar import Registrar

ROOT = Path(__file__).resolve().parent
FONT = ("Microsoft YaHei UI", 10)
LOG_TAGS = {
    "info": "#222222",
    "ok": "#0a7d32",
    "err": "#c62828",
    "warn": "#b26a00",
    "dim": "#888888",
}
STATUS_CN = {"unused": "可用", "registered": "已注册", "failed": "失败"}
CERT_CN = {"submitted": "已提交", "failed": "认证失败"}
PWD_CN = {"success": "已设置", "already": "之前已设置",
          "failed": "失败", "unconfirmed": "待复核"}

def read_text_smart(path) -> str:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace")


def _hub_precheck(log):
    """HubStudio预检(只查状态不耗额度)。在工作线程内调用; 返回None=通过,
    否则返回错误文本。绝不能在主线程调(2026-09-09实测: 客户端API卡死时
    两个HTTP请求各等30s超时, 主线程同步调用会冻结GUI整整60s且零日志)。"""
    import hubstudio
    log("HubStudio预检中(只查状态, 不耗额度)…", "dim")
    try:
        hubstudio.check_ready()
        return None
    except Exception as e:
        log(f"HubStudio预检失败, 本次任务取消: {e}", "err")
        return str(e)

class App(tk.Tk):
    def __init__(self, auto_close: bool = False):
        super().__init__()
        self.title("短剧注册 / 执照翻译")
        self.geometry("1120x880")
        self.minsize(1120, 720)  # ④区操作行(参数+4按钮)实测需~1086px, 再窄按钮截断
        self.q: queue.Queue = queue.Queue()
        self.worker = None
        self.stop_event = None
        # 登录按钮专用: 线程驻留模型(Playwright对象绑定创建线程, 成功后parked等释放)
        self._login_state = "idle"  # idle / running / parked
        self._login_thread = None
        self._login_release = None
        self._login_handle = None  # 驻留中的浏览器 (playwright, browser)
        self.auto_close = auto_close
        self._log_buffer: list[str] = []
        self._build_style()
        self._build_ui()
        self.refresh_pool()
        self.after(50, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._append_log("就绪。流程: ①导入账号 → ②导入机构名 → ③确认号池 → ④开始注册(可勾选自动企业认证/设置密码)", "dim")

    # ---------- 界面 ----------
    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        self.option_add("*Font", FONT)
        style.configure("TNotebook.Tab", padding=(18, 6), font=FONT)

    def _build_api_key_slot(self):
        """Fill the empty notebook tab-strip with the license AI key field."""
        from license_gui import _read_ai_api_key

        slot = ttk.Frame(self)
        ttk.Label(slot, text="API Key").pack(side="left")
        self.var_api_key = tk.StringVar(value=_read_ai_api_key())
        self.ent_api_key = ttk.Entry(
            slot, textvariable=self.var_api_key, width=28, show="*")
        self.ent_api_key.pack(side="left", padx=(6, 0))
        self.ent_api_key.bind("<Return>", lambda _e: self._save_api_key())
        ttk.Button(slot, text="保存", command=self._save_api_key, width=6).pack(
            side="left", padx=(6, 0))
        self.lbl_api_key = ttk.Label(slot, text="", foreground="#888888")
        self.lbl_api_key.pack(side="left", padx=(8, 0))
        self._api_slot = slot
        self._refresh_api_key_status()
        self.nb.bind("<<NotebookTabChanged>>", self._place_api_key_slot, add="+")
        self.nb.bind("<Configure>", self._place_api_key_slot, add="+")
        self.after(0, self._place_api_key_slot)

    def _place_api_key_slot(self, _event=None):
        try:
            on_license = self.nb.index("current") == self.nb.index(self.page_lic)
        except tk.TclError:
            return
        if not on_license:
            self._api_slot.place_forget()
            return
        self._api_slot.place(in_=self.nb, relx=1.0, x=-10, y=5, anchor="ne")
        self._api_slot.lift()

    def _refresh_api_key_status(self):
        from license_gui import _read_ai_api_key
        if _read_ai_api_key():
            self.lbl_api_key.config(text="已配置", foreground="#0a7d32")
        else:
            self.lbl_api_key.config(text="未填写", foreground="#888888")

    def _save_api_key(self):
        from license_gui import _save_ai_api_key
        key = self.var_api_key.get().strip()
        try:
            _save_ai_api_key(key)
        except Exception as exc:
            messagebox.showerror("执照翻译", f"API Key 保存失败: {exc}")
            return
        self.var_api_key.set(key)
        self._refresh_api_key_status()
        if hasattr(self, "page_lic") and hasattr(self.page_lic, "_append_log"):
            if key:
                self.page_lic._append_log("AI API Key 已保存，下次转换生效。", "ok")
            else:
                self.page_lic._append_log("AI API Key 已清空，转换为规则模式。", "warn")

    def _build_ui(self):
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=(6, 8))
        self.page_reg = ttk.Frame(self.nb)
        self.nb.add(self.page_reg, text="  短剧注册  ")
        self.page_lic = LicensePage(self.nb)
        self.nb.add(self.page_lic, text="  执照翻译  ")
        self._build_api_key_slot()

        # ① 导入区
        f1 = ttk.LabelFrame(self.page_reg, text=" ① 导入账号(选txt文件 或 粘贴文本) ")
        f1.pack(fill="x", padx=12, pady=(10, 4))
        bar1 = ttk.Frame(f1)
        bar1.pack(fill="x", padx=10, pady=(6, 4))
        ttk.Button(bar1, text="选择 txt 文件导入…", command=self.import_file).pack(side="left")
        ttk.Button(bar1, text="导入下方粘贴内容", command=self.import_paste).pack(side="left", padx=8)
        self.lbl_import = ttk.Label(
            bar1, text="格式: 邮箱----密码----client_id----令牌(每行一条, 可带序号)",
            foreground="#777777")
        self.lbl_import.pack(side="right", padx=(6, 8))
        self.txt_paste = tk.Text(f1, height=4, wrap="none", font=("Consolas", 9))
        self.txt_paste.pack(fill="x", padx=10, pady=(0, 8))

        # ② 导入机构名区
        f2 = ttk.LabelFrame(self.page_reg, text=" ② 导入机构名(认证表单机构名称, 按队列唯一分配, 一名一用) ")
        f2.pack(fill="x", padx=12, pady=4)
        bar2 = ttk.Frame(f2)
        bar2.pack(fill="x", padx=10, pady=(6, 4))
        ttk.Button(bar2, text="选择 txt 文件导入…",
                   command=self.import_inst_file).pack(side="left")
        ttk.Button(bar2, text="导入下方粘贴内容",
                   command=self.import_inst_paste).pack(side="left", padx=8)
        self.lbl_inst_stat = ttk.Label(
            bar2, text="格式: 每行一条机构名(可带序号)", foreground="#777777")
        self.lbl_inst_stat.pack(side="right", padx=(6, 8))
        self.txt_inst_paste = tk.Text(f2, height=3, wrap="none",
                                      font=("Consolas", 9))
        self.txt_inst_paste.pack(fill="x", padx=10, pady=(0, 8))

        # ③ 号池区
        f2 = ttk.LabelFrame(self.page_reg, text=" ③ 号池(pool_data/ 文件夹内全部json自动合并) ")
        f2.pack(fill="both", padx=12, pady=4)
        # 统计行(上): 三个统计标签右对齐(逆序pack保阅读顺序, 右缘统一-8px)
        stat = ttk.Frame(f2)
        stat.pack(fill="x", padx=10, pady=(6, 0))
        self.lbl_company = ttk.Label(stat, text="", foreground="#777777")
        self.lbl_company.pack(side="right", padx=(0, 8))
        self.lbl_mat = ttk.Label(stat, text="", foreground="#777777")
        self.lbl_mat.pack(side="right", padx=14)
        self.lbl_pool = ttk.Label(stat, text="…")
        self.lbl_pool.pack(side="right")
        # 按钮行(下): 右对齐
        bar2 = ttk.Frame(f2)
        bar2.pack(fill="x", padx=10, pady=(4, 2))
        self.btn_reset = ttk.Button(bar2, text="重置失败账号",
                                    command=self.reset_failed, width=12)
        self.btn_reset.pack(side="right", padx=(0, 8))
        ttk.Button(bar2, text="刷新", command=self.refresh_pool, width=8).pack(side="right", padx=6)
        ttk.Button(bar2, text="导出Excel…", command=self.export_excel,
                   width=12).pack(side="right")
        self.btn_archive = ttk.Button(bar2, text="归档资料不全", width=12,
                                      command=self.archive_bad_materials)
        self.btn_archive.pack(side="right", padx=6)
        self.btn_reclaim = ttk.Button(bar2, text="回收未提交占用", width=14,
                                      command=self.reclaim_stale)
        self.btn_reclaim.pack(side="right", padx=(0, 6))
        # 登录选中账号: 人工查看用(③区表格选中行→自动登录, 浏览器保持打开)
        self.btn_login = ttk.Button(bar2, text="▶ 登录选中账号", width=14,
                                    command=self.start_login_selected)
        self.btn_login.pack(side="right", padx=(0, 6))
        wrap = ttk.Frame(f2)
        wrap.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        cols = ("idx", "email", "status", "cert", "inst", "pwd", "time", "err")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=7)
        self.tree.heading("idx", text="#")
        self.tree.heading("email", text="邮箱")
        self.tree.heading("status", text="状态")
        self.tree.heading("cert", text="企业认证")
        self.tree.heading("inst", text="机构名")
        self.tree.heading("pwd", text="密码")
        self.tree.heading("time", text="注册时间")
        self.tree.heading("err", text="备注/错误")
        self.tree.column("idx", width=44, anchor="e", stretch=False)
        self.tree.column("email", width=230)
        self.tree.column("status", width=66, anchor="center", stretch=False)
        self.tree.column("cert", width=76, anchor="center", stretch=False)
        self.tree.column("inst", width=150)
        self.tree.column("pwd", width=76, anchor="center", stretch=False)
        self.tree.column("time", width=128, anchor="center", stretch=False)
        self.tree.column("err", width=220)
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.tag_configure("registered", foreground="#0a7d32")
        self.tree.tag_configure("failed", foreground="#c62828")
        self.tree.tag_configure("unused", foreground="#555555")

        # ④ 注册+认证+密码区
        f3 = ttk.LabelFrame(self.page_reg, text=" ④ 自动注册 + 企业认证 + 设置密码 ")
        f3.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        # 选项行(上): 5个复选框右端对齐(逆序pack, 右缘与下行「■ 停止」按钮齐平)
        bar3 = ttk.Frame(f3)
        bar3.pack(fill="x", padx=10, pady=(8, 2))
        self.chk_autopwd = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar3, text="注册后自动设置密码",
                        variable=self.chk_autopwd).pack(side="right", padx=(0, 8))
        self.chk_autocert = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar3, text="注册后自动企业认证",
                        variable=self.chk_autocert).pack(side="right", padx=(0, 14))
        # 每账号新IP: HubStudio代理对每个新context自动分配独立出口IP(零额度), 勾选后实测并记录
        self.chk_rot = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar3, text="每账号新IP",
                        variable=self.chk_rot).pack(side="right", padx=(0, 14))
        self.chk_hub = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar3, text="HubStudio指纹浏览器",
                        variable=self.chk_hub).pack(side="right", padx=(0, 8))
        self.chk_headless = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar3, text="无头模式(后台运行不弹浏览器)",
                        variable=self.chk_headless).pack(side="right", padx=(0, 14))
        self.chk_parallel_lic = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar3, text="并行执照翻译(扫描执照库)",
                        variable=self.chk_parallel_lic).pack(side="right", padx=(0, 14))
        # 操作行(下): 消耗数 + 按钮组
        bar4 = ttk.Frame(f3)
        bar4.pack(fill="x", padx=10, pady=(2, 6))
        ttk.Label(bar4, text="本次消耗:").pack(side="left")
        self.cmb_count = ttk.Combobox(
            bar4, values=("1", "5", "10", "20", "max"), width=5, state="readonly")
        self.cmb_count.current(0)
        self.cmb_count.pack(side="left", padx=(4, 14))
        # 默认5; 政策可变所以允许改, 必须点「应用」并确认(尤其缩小)
        ttk.Label(bar4, text="每企业认证账号数:").pack(side="left")
        self.spn_slots = ttk.Spinbox(
            bar4, from_=materials.MIN_COMPANY_SLOTS,
            to=materials.MAX_COMPANY_SLOTS, width=3)
        self.spn_slots.set(materials.get_company_slots())
        self.spn_slots.pack(side="left", padx=(4, 4))
        self.btn_apply_slots = ttk.Button(
            bar4, text="应用", width=4, command=self._apply_company_slots)
        self.btn_apply_slots.pack(side="left", padx=(0, 14))
        self.spn_slots.bind("<Return>", lambda _e: self._apply_company_slots())
        self.btn_stop = ttk.Button(bar4, text="■ 停止", command=self.stop_register, state="disabled")
        self.btn_stop.pack(side="right", padx=8)
        self.btn_start = ttk.Button(bar4, text="▶ 开始注册", command=self.start_register)
        self.btn_start.pack(side="right")
        self.btn_cert = ttk.Button(bar4, text="▶ 认证已注册账号", command=self.start_certify)
        self.btn_cert.pack(side="right", padx=(0, 8))
        self.btn_pwd = ttk.Button(bar4, text="▶ 设置密码", command=self.start_pwd)
        self.btn_pwd.pack(side="right", padx=(0, 8))
        self.progress = ttk.Progressbar(f3, maximum=100)
        self.progress.pack(fill="x", padx=10, pady=(0, 4))
        self.lbl_progress = ttk.Label(f3, text="空闲")
        self.lbl_progress.pack(anchor="w", padx=10)
        self.log_text = ScrolledText(f3, height=10, state="disabled",
                                     font=("Consolas", 9), bg="#fafafa")
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        for tag, color in LOG_TAGS.items():
            self.log_text.tag_configure(tag, foreground=color)

    # ---------- 导入 ----------
    def import_file(self):
        path = filedialog.askopenfilename(
            title="选择账号txt文件", parent=self,
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        self._do_import(read_text_smart(path), Path(path).name)

    def import_paste(self):
        text = self.txt_paste.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("提示", "粘贴框为空, 请先粘贴账号文本。", parent=self)
            return
        self._do_import(text, "粘贴内容")

    def _do_import(self, text: str, source: str):
        try:
            new, dup, bad = pool.import_text(text, source)
        except Exception as e:
            messagebox.showerror("导入失败", str(e), parent=self)
            return
        parts = [f"新增 {new}"]
        if dup:
            parts.append(f"重复跳过 {dup}")
        if bad:
            parts.append(f"无法解析 {len(bad)} 行")
        self.lbl_import.config(text="  |  ".join(parts))
        self._append_log(f"[导入:{source}] " + "; ".join(parts),
                         "ok" if new else "warn")
        for b in bad[:5]:
            self._append_log(f"  无法解析行: {b}", "dim")
        self.refresh_pool()
        if new == 0 and dup > 0:
            messagebox.showinfo("导入完成", "这些邮箱号池里已存在, 全部跳过。", parent=self)

    # ---------- 机构名导入 ----------
    def import_inst_file(self):
        path = filedialog.askopenfilename(
            title="选择机构名txt文件", parent=self,
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        self._do_import_inst(read_text_smart(path), Path(path).name)

    def import_inst_paste(self):
        text = self.txt_inst_paste.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("提示", "粘贴框为空, 请先粘贴机构名文本。", parent=self)
            return
        self._do_import_inst(text, "粘贴内容")

    def _do_import_inst(self, text: str, source: str):
        try:
            new, dup, over = institutions.import_text(text, source)
        except Exception as e:
            messagebox.showerror("导入失败", str(e), parent=self)
            return
        parts = [f"新增 {new}"]
        if dup:
            parts.append(f"重复跳过 {dup}")
        if over:
            parts.append(f"超长跳过 {over}(>30字符, 表单输入框上限)")
        self.lbl_inst_stat.config(text="  |  ".join(parts))
        self._append_log(f"[机构名导入:{source}] " + "; ".join(parts),
                         "ok" if new else "warn")
        self.refresh_pool()
        if new == 0 and dup > 0 and over == 0:
            messagebox.showinfo("导入完成", "这些机构名库中已存在, 全部跳过。", parent=self)

    # ---------- 号池 ----------
    def refresh_pool(self):
        accounts = pool.all_accounts()
        stats = {"unused": 0, "registered": 0, "failed": 0}
        self.tree.delete(*self.tree.get_children())
        for a in accounts:
            st = a.get("status", "unused")
            stats[st] = stats.get(st, 0) + 1
            cert = a.get("cert_status")
            pwd = a.get("pwd_status")
            note = (a.get("last_error") or a.get("cert_error")
                    or a.get("pwd_error") or "")
            if cert and a.get("cert_company"):
                note = f"[{a['cert_company']}] {note}".strip()
            self.tree.insert("", "end", tags=(st,), values=(
                a.get("index", ""), a["email"], STATUS_CN.get(st, st),
                CERT_CN.get(cert, "—"), a.get("institution") or "—",
                PWD_CN.get(pwd, "—"),
                a.get("registered_at") or "", note[:80]))
        self.lbl_pool.config(
            text=f"总数 {len(accounts)}    可用 {stats['unused']}    "
                 f"已注册 {stats['registered']}    失败 {stats['failed']}")
        try:
            ok_c, used_c, _ = materials.summary()
            self.lbl_mat.config(text=f"公司槽位: 可用 {ok_c} / 已用 {used_c}")
            total, used_co, free_co, bad_co = materials.company_counts()
            self.lbl_company.config(
                text=f"公司数: 总 {total} 家 / 已用 {used_co} / 空闲 {free_co} / 不全 {bad_co}")
        except Exception:
            self.lbl_mat.config(text="")
            self.lbl_company.config(text="")
        try:
            unused_i, used_i = institutions.summary()
            over_i = institutions.count_overlong()
            stat = (f"机构名库: 总数 {unused_i + used_i}    可用 {unused_i}    "
                    f"已用 {used_i}")
            if over_i:
                stat += f"    超长不可用 {over_i}"
            self.lbl_inst_stat.config(text=stat)
        except Exception:
            self.lbl_inst_stat.config(text="机构名库: 不可用")

    def export_excel(self):
        """导出已注册账号清单: 邮箱/密码/企业认证公司等"""
        try:
            import openpyxl
        except ImportError:
            messagebox.showerror(
                "缺少依赖",
                "导出需要 openpyxl(首次运行GUI时已自动安装)。\n请重新运行 启动.bat 后重试。",
                parent=self)
            return
        accounts = pool.all_accounts()
        rows = [a for a in accounts if a.get("status") == "registered"]
        if not rows:
            messagebox.showinfo("提示", "没有已注册账号可导出。", parent=self)
            return
        path = filedialog.asksaveasfilename(
            title="导出Excel", parent=self, defaultextension=".xlsx",
            initialfile=f"账号导出_{datetime.now():%Y%m%d_%H%M}.xlsx",
            filetypes=[("Excel 工作簿", "*.xlsx")])
        if not path:
            return
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "账号"
        ws.append(["邮箱", "密码", "密码状态", "企业认证公司", "机构名", "认证状态", "注册时间"])
        for a in rows:
            ws.append([
                a["email"], a.get("password", ""),
                PWD_CN.get(a.get("pwd_status"), "未设置"),
                a.get("cert_company") or "",
                a.get("institution") or "",
                CERT_CN.get(a.get("cert_status"), "—"),
                a.get("registered_at") or "",
            ])
        for col, w in zip("ABCDEFG", (34, 16, 12, 30, 26, 10, 20)):
            ws.column_dimensions[col].width = w
        try:
            wb.save(path)
        except OSError as e:
            messagebox.showerror("导出失败", f"保存失败: {e}\n(文件可能被Excel占用)", parent=self)
            return
        n_pwd = sum(1 for a in rows if a.get("pwd_status") in ("success", "already"))
        self._append_log(f"已导出 {len(rows)} 个账号(其中密码已设置 {n_pwd} 个) → {path}", "ok")
        messagebox.showinfo(
            "导出完成",
            f"已导出 {len(rows)} 个已注册账号:\n{path}\n\n"
            f"其中密码已设置: {n_pwd} 个。", parent=self)

    def reset_failed(self):
        n = pool.reset_failed()
        self.refresh_pool()
        self._append_log(f"已重置 {n} 个失败账号为可用状态。", "ok")

    def reclaim_stale(self):
        if self.worker and self.worker.is_alive():
            return
        n_slot = materials.count_assigned()
        n_inst = institutions.count_assigned()
        if n_slot == 0 and n_inst == 0:
            messagebox.showinfo(
                "提示",
                "当前没有「分配中、从未提交」的占用。\n\n"
                "认证失败(已点提交、结果未确认)的占用会在重试时续上同一对公司和机构名, 不会回收。",
                parent=self)
            return
        if not messagebox.askyesno(
                "确认回收",
                f"将释放分配中但从未提交的占用:\n\n"
                f"· 公司槽 {n_slot} 个\n· 机构名 {n_inst} 个\n\n"
                "不影响已提交, 也不影响认证失败待核实的占用。确定?",
                parent=self):
            return
        stats = pool.reclaim_stale_assignments()
        self._append_log(
            f"已回收未提交占用: 公司槽 {len(stats['slots'])} 个, "
            f"机构名 {len(stats['institutions'])} 个", "ok")
        self.refresh_pool()

    # ---------- 归档资料不全 ----------
    def archive_bad_materials(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            targets, bound = materials.incomplete_companies()
        except Exception as e:
            messagebox.showerror(
                "资料不可用",
                f"企业资料解析失败: {e}\n\n请确认 资料/ 文件夹与xlsx汇总表正常, "
                "且已安装 openpyxl。", parent=self)
            return
        if not targets:
            msg = "当前没有可归档的资料不全公司。"
            if bound:
                msg += (f"\n\n另有 {len(bound)} 家资料不全但已绑定账号的公司, "
                        "为保留绑定历史不做归档。")
            messagebox.showinfo("提示", msg, parent=self)
            return
        lines = [f"· {p['name']}  ({'; '.join(p['issues'])})" for p in targets[:6]]
        if len(targets) > 6:
            lines.append(f"… 等共 {len(targets)} 家")
        if not messagebox.askyesno(
                "确认归档",
                f"将归档以下 {len(targets)} 家资料不全的公司:\n\n" + "\n".join(lines) +
                "\n\n操作内容:\n"
                "· 公司文件夹移入 资料/资料不全/ (无文件夹的仅删表行)\n"
                "· 从xlsx汇总表删除对应行, 原表先自动备份到归档目录\n"
                "· 清理 overrides.json 中这些公司的条目\n"
                "· 已绑定账号的公司不会归档。确定执行?", parent=self):
            return
        try:
            report = materials.archive_incomplete()
        except Exception as e:
            messagebox.showerror("归档失败", str(e), parent=self)
            return
        for r in report["archived"]:
            if r["error"]:
                self._append_log(f"[归档] {r['name']}: {r['error']}", "err")
            else:
                where = "文件夹已移入归档" if r["folder"] else "无文件夹, 仅删表行"
                self._append_log(f"[归档] {r['name']}: {where}", "ok")
        if report["errors"]:
            for e in report["errors"]:
                self._append_log(f"[归档] {e}", "err")
            messagebox.showerror("归档失败", "\n".join(report["errors"]), parent=self)
        else:
            self._append_log(
                f"归档完成 {len(report['archived'])} 家; "
                f"原汇总表已备份 → {report['backup']}", "ok")
            messagebox.showinfo(
                "归档完成",
                f"已归档 {len(report['archived'])} 家资料不全公司。\n\n"
                f"原汇总表备份: {report['backup']}\n"
                "归档目录: 资料/资料不全/", parent=self)
        self.refresh_pool()

    def _shown_slots(self):
        try:
            return int(str(self.spn_slots.get()).strip())
        except (TypeError, ValueError):
            return None

    def _slots_applied(self) -> bool:
        """Start is blocked until the spinbox matches the persisted cap."""
        shown = self._shown_slots()
        cur = materials.get_company_slots()
        if shown is None:
            self.spn_slots.set(cur)
            return True
        shown = materials._clamp_slots(shown)
        if shown != cur:
            messagebox.showwarning(
                "槽位数未应用",
                f"界面显示 {shown}，但当前生效的是 {cur}。\n"
                "请先点「应用」（会有慎重提醒），或改回后再开始。",
                parent=self)
            return False
        return True

    def _apply_company_slots(self):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(
                "请先停止", "任务进行中不能改每企业认证账号数。", parent=self)
            return
        shown = self._shown_slots()
        cur = materials.get_company_slots()
        if shown is None:
            messagebox.showerror(
                "无效数字",
                f"请输入 {materials.MIN_COMPANY_SLOTS}~{materials.MAX_COMPANY_SLOTS} 的整数。",
                parent=self)
            self.spn_slots.set(cur)
            return
        n = materials._clamp_slots(shown)
        self.spn_slots.set(n)
        if n == cur:
            return
        occ, who = materials.max_slot_occupancy()
        msg = (
            "默认每企业绑定 5 个账号。平台政策可能会调整，所以允许改这个数字，"
            "但改动可能导致运行不稳定，尤其是从大改到小，不建议频繁修改。\n\n"
            f"当前生效: {cur}    将改为: {n}\n"
            "已占用的绑定记录不会删除。"
        )
        if n < cur:
            msg += (
                "\n\n这是缩小上限。已超过新上限的公司仍保留现有绑定，"
                "只是不再分配新账号；本地计数可能与平台不一致。"
            )
            if occ > n:
                extra = f"例如「{who}」" if who else "部分公司"
                msg += f"\n目前{extra}已占用 {occ} 个槽，超过新上限 {n}。"
        if not messagebox.askyesno("慎重：确认修改", msg + "\n\n确定应用？", parent=self):
            self.spn_slots.set(cur)
            return
        applied = materials.set_company_slots(n)
        self.spn_slots.set(applied)
        self._append_log(
            f"每企业认证账号数已改为 {applied}（原 {cur}；不建议频繁修改）", "warn")
        self.refresh_pool()

    # ---------- 注册 ----------
    def start_register(self, confirm: bool = True):
        if self.worker and self.worker.is_alive():
            return
        if not self._slots_applied():
            return
        unused = [a for a in pool.all_accounts() if a.get("status") == "unused"]
        if not unused:
            if self.auto_close:
                self._append_log("号池中没有可用账号, 自动退出。", "warn")
                self.after(300, self.destroy)
            else:
                messagebox.showwarning("提示", "号池中没有可用账号, 请先在①区导入。", parent=self)
            return
        sel = self.cmb_count.get()
        targets = unused if sel == "max" else unused[: int(sel)]
        autocert = self.chk_autocert.get()
        if autocert:
            try:
                ok_c, _, _ = materials.summary()
            except Exception as e:
                self._append_log(f"公司资料解析失败({e}), 本次注册将跳过自动认证", "warn")
                autocert = False
            else:
                unused_i, _ = institutions.summary()
                cap = min(ok_c, unused_i)
                if cap <= 0:
                    self._append_log(
                        "没有可用公司槽位或机构名, 本次注册将跳过自动认证", "warn")
                    autocert = False
                elif cap < len(targets):
                    self._append_log(
                        f"可用公司槽位/机构名仅 {cap} 个, 本次只注册前 {cap} 个账号"
                        " (避免注册后无法认证)", "warn")
                    targets = targets[:cap]
        preview = "\n".join(a["email"] for a in targets[:5])
        if len(targets) > 5:
            preview += f"\n… 等共 {len(targets)} 个"
        autopwd = self.chk_autopwd.get()
        cert_tip = ("\n\n已勾选[注册后自动企业认证]: 每个账号注册成功后立即绑定公司槽位提交认证"
                    f"\n(每企业最多认证{materials.COMPANY_SLOTS}个账号, 机构名一名一用, 用量自动跟踪)。"
                    if autocert else "")
        pwd_tip = "\n\n已勾选[注册后自动设置密码]: 每个账号注册成功后自动设置账户密码(复用邮箱密码)。" if autopwd else ""
        if confirm and not messagebox.askyesno(
                "确认开始",
                f"本次将依次注册 {len(targets)} 个账号:\n\n{preview}\n\n"
                f"预计每个耗时1~3分钟, 浏览器会自动弹出并完成操作。{cert_tip}{pwd_tip}\n确定开始?",
                parent=self):
            return
        self.stop_event = threading.Event()
        self._set_running(True)
        self.progress.config(maximum=len(targets), value=0)
        headless = self.chk_headless.get()  # tkinter变量只能主线程读, 提前取出
        use_hub = self.chk_hub.get()
        rot_ip = use_hub and self.chk_rot.get()
        self._append_log(f"开始注册 {len(targets)} 个账号"
                         f"(HubStudio={'开' if use_hub else '关'}, "
                         f"每账号新IP={'开' if rot_ip else '关'}, "
                         f"无头模式={'开' if headless else '关'}, "
                         f"自动认证={'开' if autocert else '关'}, "
                         f"自动密码={'开' if autopwd else '关'})", "warn")
        if rot_ip:
            self._append_log("每账号新IP: 代理对每个窗口自动分配独立出口IP(零额度), "
                             "本次实测并记录每账号IP", "dim")
        self.worker = threading.Thread(
            target=self._work, args=(targets, headless, use_hub, rot_ip, autocert, autopwd),
            daemon=True)
        self.worker.start()
        # Parallel license translation: scan 执照库 in background
        if self.chk_parallel_lic.get():
            self._start_parallel_license_scan()

    def _start_parallel_license_scan(self):
        """Start background license library scan while registration runs."""
        source = ROOT / "中国执照库"
        if not source.exists():
            self._append_log("并行执照翻译: 执照库不存在, 跳过", "dim")
            return
        company_dirs = [d for d in source.iterdir() if d.is_dir()]
        if not company_dirs:
            self._append_log("并行执照翻译: 执照库为空, 跳过", "dim")
            return
        self._append_log(
            f"并行执照翻译: 扫描执照库 {len(company_dirs)} 家公司 "
            f"(后台运行, 不影响注册)", "warn")
        self._lic_stop = threading.Event()
        self._lic_thread = threading.Thread(
            target=self._work_parallel_license,
            args=(source, ROOT / "资料", self._lic_stop),
            daemon=True)
        self._lic_thread.start()

    def _work_parallel_license(self, source: Path, dest: Path, stop_event: threading.Event):
        def log(msg, level="info"):
            self.q.put(("log", level, f"[执照] {msg}"))

        try:
            from license_trans.ocr_service import OcrService
            from license_trans.pipeline import process_license_library
        except ImportError as e:
            log(f"执照翻译依赖缺失: {e}", "err")
            return
        try:
            from license_trans.pipeline import load_config
            engine = OcrService(load_config())
        except Exception as e:
            log(f"OCR 引擎加载失败: {e}", "err")
            return
        prov = getattr(engine, "gpu_provider", "CPU")
        eng = getattr(engine, "engine_name", "RapidOCR")
        log(f"OCR 引擎就绪: {eng} ({prov}), 开始并行扫描+翻译…", "ok")
        try:
            result = process_license_library(
                source, dest, max_workers=5, engine=engine,
                log=log, stop_event=stop_event)
            log(
                f"===== 执照翻译完成: 翻译 {result['processed']} / "
                f"失败 {result['failed']} / 仲裁未过 {result.get('review', 0)} / "
                f"移动 {result['moved']} / "
                f"跳过 {result['skipped']} =====",
                "ok" if result["failed"] == 0 and not result.get("review") else "warn")
        except Exception as exc:
            log(f"执照翻译异常: {exc}", "err")

    def _work(self, targets, headless: bool, use_hub: bool, rot_ip: bool,
              autocert: bool, autopwd: bool):
        total = len(targets)

        def log(msg, level="info"):
            self.q.put(("log", level, f"[{datetime.now():%H:%M:%S}] {msg}"))

        def prog(done, tot):
            self.q.put(("progress", done, tot))

        def after_register(page, email, shoot=None):
            log(f"--- 注册后附加任务(企业认证+设置密码): {email} ---")
            password_setter.after_register_tasks(
                page, email, log=log, stop_event=self.stop_event,
                shoot=shoot, do_cert=autocert, do_pwd=autopwd)
            self.q.put(("pool",))

        if use_hub:
            err = _hub_precheck(log)
            if err:
                self.q.put(("hub_fail", err))
                return
        try:
            reg = Registrar(targets, headless=headless,
                            log=log, progress=prog, stop_event=self.stop_event,
                            after_register=after_register,
                            use_hubstudio=use_hub, rotate_ip=rot_ip)
            ok, fail = reg.run()
            self.q.put(("done", ok, fail, self.stop_event.is_set()))
        except Exception as e:
            log(f"引擎异常: {e}", "err")
            self.q.put(("done", 0, total, True))

    # ---------- 独立企业认证 ----------
    def start_certify(self, confirm: bool = True):
        if self.worker and self.worker.is_alive():
            return
        if not self._slots_applied():
            return
        accounts = pool.all_accounts()
        targets = [a for a in accounts
                   if a.get("status") == "registered" and a.get("cert_status") != "submitted"]
        if not targets:
            messagebox.showinfo("提示", "没有待认证的已注册账号。\n"
                                        "(已提交认证的账号不会重复处理)", parent=self)
            return
        try:
            ok_c, used_c, problems = materials.summary()
        except Exception as e:
            messagebox.showerror(
                "资料不可用",
                f"企业资料解析失败: {e}\n\n请确认 资料/ 文件夹(xlsx汇总表+公司文件夹)存在, "
                "且已安装 openpyxl。", parent=self)
            return
        unused_i, used_i = institutions.summary()
        runnable, skipped = pool.select_certifiable(targets)
        for email, reason in skipped[:8]:
            self._append_log(f"  跳过 {email}: {reason}", "dim")
        if len(skipped) > 8:
            self._append_log(f"  … 另有 {len(skipped) - 8} 个跳过", "dim")
        if not runnable:
            msg = "没有可认证账号(无空槽、无可用机构名, 且没有可续上的失败绑定)。"
            if skipped:
                msg += "\n\n" + "\n".join(f"· {e}: {r}" for e, r in skipped[:6])
            elif problems:
                msg += "\n\n资料不全的公司:\n" + "\n".join(problems[:6])
            messagebox.showwarning("提示", msg, parent=self)
            return
        if skipped:
            self._append_log(
                f"库存/续用限制: {len(targets)} 个待认证 → 本次处理 {len(runnable)} 个",
                "warn")
        targets = runnable
        preview = "\n".join(a["email"] for a in targets[:5])
        if len(targets) > 5:
            preview += f"\n… 等共 {len(targets)} 个"
        if confirm and not messagebox.askyesno(
                "确认开始",
                f"将为以下 {len(targets)} 个已注册账号逐个提交企业认证:\n\n{preview}\n\n"
                f"公司槽位: 可用 {ok_c} 个 / 已用 {used_c} 个"
                f" (每企业{materials.COMPANY_SLOTS}号, 先填满一家再换下一家)\n"
                f"机构名库: 可用 {unused_i} 个 / 已用 {used_i} 个\n"
                "已有认证失败绑定的账号会续上原公司/机构名, 不新占库存;\n"
                "新号各占一个公司槽位 + 一个机构名。登录过期自动验证码重登。\n确定开始?", parent=self):
            return
        self.stop_event = threading.Event()
        self._set_running(True)
        self.progress.config(maximum=len(targets), value=0)
        headless = self.chk_headless.get()
        use_hub = self.chk_hub.get()
        rot_ip = use_hub and self.chk_rot.get()
        self._append_log(f"开始企业认证 {len(targets)} 个账号"
                         f"(HubStudio={'开' if use_hub else '关'}, "
                         f"每账号新IP={'开' if rot_ip else '关'}, "
                         f"无头模式={'开' if headless else '关'})", "warn")
        if rot_ip:
            self._append_log("每账号新IP: 代理对每个窗口自动分配独立出口IP(零额度), "
                             "本次实测并记录每账号IP", "dim")
        self.worker = threading.Thread(
            target=self._work_cert, args=(targets, headless, use_hub, rot_ip), daemon=True)
        self.worker.start()

    def _work_cert(self, targets, headless: bool, use_hub: bool, rot_ip: bool):
        total = len(targets)

        def log(msg, level="info"):
            self.q.put(("log", level, f"[{datetime.now():%H:%M:%S}] {msg}"))

        def prog(done, tot):
            self.q.put(("progress", done, tot))

        if use_hub:
            err = _hub_precheck(log)
            if err:
                self.q.put(("hub_fail", err))
                return
        try:
            cert = certifier.Certifier(targets, headless=headless,
                                       log=log, progress=prog,
                                       stop_event=self.stop_event,
                                       use_hubstudio=use_hub, rotate_ip=rot_ip)
            ok, fail, skip = cert.run()
            self.q.put(("cert_done", ok, fail, skip, self.stop_event.is_set()))
        except Exception as e:
            log(f"引擎异常: {e}", "err")
            self.q.put(("cert_done", 0, total, 0, True))

    # ---------- 独立设置密码 ----------
    def start_pwd(self, confirm: bool = True):
        if self.worker and self.worker.is_alive():
            return
        accounts = pool.all_accounts()
        targets = [a for a in accounts
                   if a.get("status") == "registered"
                   and a.get("pwd_status") not in ("success", "already")]
        if not targets:
            messagebox.showinfo(
                "提示", "没有待设置密码的已注册账号。\n(已设置成功的账号不会重复处理)", parent=self)
            return
        preview = "\n".join(
            f"{a['email']}  ({PWD_CN.get(a.get('pwd_status'), '未设置')})"
            for a in targets[:5])
        if len(targets) > 5:
            preview += f"\n… 等共 {len(targets)} 个"
        if confirm and not messagebox.askyesno(
                "确认开始",
                f"将为以下 {len(targets)} 个已注册账号设置账户密码:\n\n{preview}\n\n"
                "密码取自号池中该账号的邮箱密码字段(导出Excel即邮箱+密码清单);\n"
                "登录会话过期时自动用邮箱验证码重登(不消耗新账号)。\n确定开始?", parent=self):
            return
        self.stop_event = threading.Event()
        self._set_running(True)
        self.progress.config(maximum=len(targets), value=0)
        headless = self.chk_headless.get()
        use_hub = self.chk_hub.get()
        rot_ip = use_hub and self.chk_rot.get()
        self._append_log(f"开始设置密码 {len(targets)} 个账号"
                         f"(HubStudio={'开' if use_hub else '关'}, "
                         f"每账号新IP={'开' if rot_ip else '关'}, "
                         f"无头模式={'开' if headless else '关'})", "warn")
        if rot_ip:
            self._append_log("每账号新IP: 代理对每个窗口自动分配独立出口IP(零额度), "
                             "本次实测并记录每账号IP", "dim")
        self.worker = threading.Thread(
            target=self._work_pwd, args=(targets, headless, use_hub, rot_ip), daemon=True)
        self.worker.start()

    def _work_pwd(self, targets, headless: bool, use_hub: bool, rot_ip: bool):
        total = len(targets)

        def log(msg, level="info"):
            self.q.put(("log", level, f"[{datetime.now():%H:%M:%S}] {msg}"))

        def prog(done, tot):
            self.q.put(("progress", done, tot))

        if use_hub:
            err = _hub_precheck(log)
            if err:
                self.q.put(("hub_fail", err))
                return
        try:
            setter = password_setter.PasswordSetter(
                targets, headless=headless, log=log, progress=prog,
                stop_event=self.stop_event, use_hubstudio=use_hub,
                rotate_ip=rot_ip)
            ok, fail, skip = setter.run()
            self.q.put(("pwd_done", ok, fail, skip, self.stop_event.is_set()))
        except Exception as e:
            log(f"引擎异常: {e}", "err")
            self.q.put(("pwd_done", 0, total, 0, True))

    # ---------- 登录选中账号(人工查看用) ----------
    def start_login_selected(self):
        """③区选中行 → 自动登录该账号; 优先auth会话恢复, 过期自动邮箱验证码重登。

        登录后浏览器保持打开供人工查看(强制有头); 登录线程驻留(Playwright对象
        绑定创建线程, 清理必须回原线程), 下次登录其他账号或退出GUI时释放。
        不消耗新账号(验证码重登只耗一封邮件)。
        """
        if self.worker and self.worker.is_alive():
            return
        if self._login_state == "running":
            return
        if self._login_state == "parked":  # 关上一个登录窗口(一次保持一个)
            self._release_login()
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在③号池表格中选中一个已注册账号。", parent=self)
            return
        email = str(self.tree.item(sel[0], "values")[1])
        acc = next((a for a in pool.all_accounts() if a["email"] == email), None)
        if not acc or acc.get("status") != "registered":
            messagebox.showinfo(
                "提示", f"{email} 不是已注册账号, 无法登录。\n"
                        "(未注册邮箱走验证码登录会被自动注册, 已拦截)", parent=self)
            return
        use_hub = self.chk_hub.get()
        if len(sel) > 1:
            self._append_log(f"选中了 {len(sel)} 行, 仅登录第一个: {email}", "dim")
        self.stop_event = threading.Event()
        self._login_state = "running"
        self._set_running(True)
        self.progress.config(maximum=1, value=0)
        self.lbl_progress.config(text=f"登录: {email}")
        self._append_log(
            f"开始登录 {email} (HubStudio={'开' if use_hub else '关'}, 有头窗口), "
            "优先auth会话恢复, 过期自动邮箱验证码重登", "warn")
        self._login_release = threading.Event()
        self._login_thread = threading.Thread(
            target=self._work_login, args=(email, use_hub, self._login_release),
            daemon=True)
        self._login_thread.start()

    def _work_login(self, email: str, use_hub: bool, release: threading.Event):
        def log(msg, level="info"):
            self.q.put(("log", level, f"[{datetime.now():%H:%M:%S}] {msg}"))

        def shoot(page, name):
            try:
                path = ROOT / "logs" / f"{datetime.now():%H%M%S}_login_{name}.png"
                path.parent.mkdir(exist_ok=True)
                page.screenshot(path=str(path))
                log(f"  截图: {path.name}", "dim")
            except Exception:
                pass

        from playwright.sync_api import sync_playwright
        import hubstudio
        pw = browser = None
        try:
            # HubStudio预检在工作线程内(主线程同步HTTP会冻结GUI)
            if use_hub:
                err = _hub_precheck(log)
                if err:
                    raise hubstudio.HubStudioError(err)
            # 不用with块: 登录成功后保持浏览器打开; Playwright sync对象绑定
            # 本线程, 线程驻留等待release, 清理(关浏览器/停driver)都在本线程做
            pw = sync_playwright().start()
            if use_hub:
                browser = hubstudio.connect(pw, log=log)  # 环境已开则复用, 不耗额度
                chain = hubstudio.WindowChain(browser, log=log)
                page, _ = certifier.open_session(
                    browser, email, log=log, stop_event=self.stop_event,
                    shoot=shoot, use_hub=True, chain=chain)
            else:
                browser = pw.chromium.launch(
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"])
                page, _ = certifier.open_session(
                    browser, email, log=log, stop_event=self.stop_event,
                    shoot=shoot, use_hub=False)
            self._login_handle = (pw, browser)
            self._login_state = "parked"
            self.q.put(("login_done", email, True, ""))
            release.wait()  # 驻留: 浏览器保持打开, 直到下次登录/退出GUI
        except Exception as e:
            self._login_state = "idle"
            if browser is not None:  # 启动/登录失败: 原地清理, 不留半登录窗口
                try:
                    browser.close()
                except Exception:
                    pass
            if use_hub:
                try:
                    hubstudio.close_env(log=log)
                except Exception:
                    pass
            if pw is not None:
                try:
                    pw.stop()
                except Exception:
                    pass
            log(f"登录失败: {e}", "err")
            self.q.put(("login_done", email, False, str(e)[:300]))
            return
        # 正常释放(下次登录/退出GUI): 原线程内清理
        if use_hub:
            log("关闭登录窗口(HubStudio环境保留供下次复用)", "dim")
        try:
            browser.close()  # Hub模式只断CDP连接, 环境不关
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
        self._login_handle = None
        self._login_state = "idle"

    def _release_login(self):
        """释放驻留的登录线程: 信号→等它在本线程内完成浏览器清理"""
        if self._login_state != "parked" or not (
                self._login_thread and self._login_thread.is_alive()):
            return
        if self._login_release:
            self._login_release.set()
        self._login_thread.join(timeout=10)
        self._login_thread = None
        self._append_log("已关闭上一个登录窗口", "dim")

    def stop_register(self):
        if self.stop_event:
            self.stop_event.set()
            self._append_log("已请求停止, 等待当前步骤结束…", "warn")

    # ---------- 事件循环 ----------
    def _drain(self):
        MAX_PER_TICK = 10
        processed = 0
        try:
            while processed < MAX_PER_TICK:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._append_log(msg[2], msg[1])
                elif kind == "progress":
                    _, done, tot = msg
                    self.progress.config(maximum=tot, value=done)
                    self.lbl_progress.config(text=f"进度: {done}/{tot}")
                elif kind == "pool":
                    self.refresh_pool()
                elif kind == "done":
                    _, ok, fail, stopped = msg
                    self._set_running(False)
                    self.progress.config(value=self.progress.cget("maximum"))
                    self.lbl_progress.config(text="空闲")
                    self.refresh_pool()
                    self._append_log(f"===== 汇总: 成功 {ok} / 失败 {fail} =====",
                                     "ok" if fail == 0 else "warn")
                    if self.auto_close:
                        self.after(800, self.destroy)
                    else:
                        messagebox.showinfo(
                            "已停止" if stopped else "注册完成",
                            f"成功: {ok} 个\n失败: {fail} 个\n\n"
                            "登录凭证已保存到 auth/ 文件夹(每个邮箱一个json)。", parent=self)
                elif kind == "cert_done":
                    _, ok, fail, skip, stopped = msg
                    self._set_running(False)
                    self.progress.config(value=self.progress.cget("maximum"))
                    self.lbl_progress.config(text="空闲")
                    self.refresh_pool()
                    self._append_log(
                        f"===== 认证汇总: 成功 {ok} / 失败 {fail} / 跳过 {skip} =====",
                        "ok" if fail == 0 else "warn")
                    if not self.auto_close:
                        messagebox.showinfo(
                            "已停止" if stopped else "认证完成",
                            f"提交成功: {ok} 个\n失败: {fail} 个\n跳过(已有认证): {skip} 个\n\n"
                            "公司槽位绑定见 资料/usage.json, 过程截图见 logs/。", parent=self)
                elif kind == "pwd_done":
                    _, ok, fail, skip, stopped = msg
                    self._set_running(False)
                    self.progress.config(value=self.progress.cget("maximum"))
                    self.lbl_progress.config(text="空闲")
                    self.refresh_pool()
                    self._append_log(
                        f"===== 密码设置汇总: 成功 {ok} / 失败 {fail} / 跳过 {skip} =====",
                        "ok" if fail == 0 else "warn")
                    if not self.auto_close:
                        messagebox.showinfo(
                            "已停止" if stopped else "密码设置完成",
                            f"设置成功: {ok} 个\n失败: {fail} 个\n跳过(已设置过): {skip} 个\n\n"
                            "结果已记录在号池, 可点[导出Excel…]导出邮箱+密码清单。", parent=self)
                elif kind == "hub_fail":
                    _, err = msg
                    self._set_running(False)
                    self.lbl_progress.config(text="空闲")
                    self._append_log("===== HubStudio不可用, 任务未执行 =====", "err")
                    if not self.auto_close:
                        messagebox.showerror(
                            "HubStudio不可用",
                            f"{err}\n\n常见原因与处理:\n"
                            "· 客户端没运行 → 打开HubStudio客户端(VIP及以上套餐)\n"
                            "· 端口在监听但超时 → 客户端卡死/残留进程, 重启HubStudio客户端\n"
                            "· 或取消勾选「HubStudio指纹浏览器」改用普通浏览器", parent=self)
                elif kind == "login_done":
                    _, email, ok, err = msg
                    self._set_running(False)
                    self.progress.config(value=1)
                    self.lbl_progress.config(text="空闲")
                    if ok:
                        self._append_log(f"===== {email} 登录完成, 浏览器保持打开 =====", "ok")
                        if not self.auto_close:
                            messagebox.showinfo(
                                "登录完成",
                                f"{email} 已登录, 浏览器窗口保持打开, 可直接查看账号。\n\n"
                                "(登录凭证已自动刷新到 auth/; 下次登录其他账号或退出GUI时\n"
                                "会自动关闭这个窗口; HubStudio模式下环境保留供复用)",
                                parent=self)
                    else:
                        self._append_log(f"===== {email} 登录失败: {err} =====", "err")
                        if not self.auto_close:
                            messagebox.showerror("登录失败", f"{email}\n\n{err}", parent=self)
                processed += 1
        except queue.Empty:
            pass

        # Batch flush log buffer to file (one write per tick)
        if self._log_buffer:
            try:
                with open(ROOT / "gui.log", "a", encoding="utf-8") as f:
                    f.writelines(self._log_buffer)
            except OSError:
                pass
            self._log_buffer.clear()

        self.after(50, self._drain)

    def _set_running(self, running: bool):
        st = "disabled" if running else "normal"
        self.btn_start.config(state=st)
        self.btn_cert.config(state=st)
        self.btn_pwd.config(state=st)
        self.btn_login.config(state=st)
        self.btn_reset.config(state=st)
        self.btn_archive.config(state=st)
        self.btn_reclaim.config(state=st)
        self.cmb_count.config(state="disabled" if running else "readonly")
        self.spn_slots.config(state="disabled" if running else "normal")
        self.btn_apply_slots.config(state="disabled" if running else "normal")
        self.btn_stop.config(state="normal" if running else "disabled")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("确认退出", "任务进行中, 退出将中止。确定退出吗?", parent=self):
                return
            if self.stop_event:
                self.stop_event.set()
        if self._login_state == "running" and self.stop_event:
            self.stop_event.set()  # 驻留前的登录流程中止
        self._release_login()
        if getattr(self, "_lic_stop", None):
            self._lic_stop.set()
        if getattr(self, "page_lic", None):
            self.page_lic.request_stop()
        self.destroy()

    def _append_log(self, text: str, level: str = "info"):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n", level if level in LOG_TAGS else "info")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        self._log_buffer.append(
            f"{datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {text}\n")

def run(smoke: bool = False, auto: int = 0) -> None:
    pool.migrate_legacy()
    stats = pool.reclaim_stale_assignments()
    app = App(auto_close=auto > 0)
    n_slot = len(stats["slots"])
    n_inst = len(stats["institutions"])
    if n_slot or n_inst:
        app._append_log(
            f"已回收未提交占用: 公司槽 {n_slot} 个, 机构名 {n_inst} 个"
            " (认证失败待核实的占用会在重试时续上, 未回收)", "warn")
    if smoke:
        app.after(1200, app.destroy)
    if auto:
        sel = "max" if auto < 0 else str(auto)
        app.cmb_count.set(sel)

        def _auto_start():
            try:
                app.start_register(confirm=False)
            except Exception:
                import traceback
                app._append_log(f"自动启动异常:\n{traceback.format_exc()}", "err")

        app.after(600, _auto_start)
    app.mainloop()

if __name__ == "__main__":
    run()
