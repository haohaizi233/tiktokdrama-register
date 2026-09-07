# -*- coding: utf-8 -*-
"""图形界面: ①导入账号(txt/粘贴) → ②号池管理(多json自动合并) → ③自动注册+企业认证

线程模型: 注册/认证在工作线程执行, 通过 queue 把日志/进度/完成事件发回主线程渲染。
企业认证: 注册后自动认证(同页面免重登) 或 独立批量认证已注册账号(会话恢复, 过期自动重登);
公司资料由 materials.py 解析(资料/文件夹), 一司一号, 用量自动跟踪。
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
PWD_CN = {"success": "已设置", "already": "已设置(既有)",
          "failed": "失败", "unconfirmed": "待复核"}

def read_text_smart(path) -> str:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace")

class App(tk.Tk):
    def __init__(self, auto_close: bool = False):
        super().__init__()
        self.title("TikTok短剧中心 自动注册+企业认证工具")
        self.geometry("1120x880")
        self.minsize(960, 720)
        self.q: queue.Queue = queue.Queue()
        self.worker = None
        self.stop_event = None
        self.auto_close = auto_close
        self._build_style()
        self._build_ui()
        self.refresh_pool()
        self.after(150, self._drain)
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

    def _build_ui(self):
        # ① 导入区
        f1 = ttk.LabelFrame(self, text=" ① 导入账号(选txt文件 或 粘贴文本) ")
        f1.pack(fill="x", padx=12, pady=(10, 4))
        bar1 = ttk.Frame(f1)
        bar1.pack(fill="x", padx=10, pady=(6, 4))
        ttk.Button(bar1, text="选择 txt 文件导入…", command=self.import_file).pack(side="left")
        ttk.Button(bar1, text="导入下方粘贴内容", command=self.import_paste).pack(side="left", padx=8)
        self.lbl_import = ttk.Label(
            bar1, text="格式: 邮箱----密码----client_id----令牌(每行一条, 可带序号)",
            foreground="#777777")
        self.lbl_import.pack(side="left", padx=6)
        self.txt_paste = tk.Text(f1, height=4, wrap="none", font=("Consolas", 9))
        self.txt_paste.pack(fill="x", padx=10, pady=(0, 8))

        # ② 导入机构名区
        f2 = ttk.LabelFrame(self, text=" ② 导入机构名(认证表单机构名称, 按队列唯一分配, 一名一用) ")
        f2.pack(fill="x", padx=12, pady=4)
        bar2 = ttk.Frame(f2)
        bar2.pack(fill="x", padx=10, pady=(6, 4))
        ttk.Button(bar2, text="选择 txt 文件导入…",
                   command=self.import_inst_file).pack(side="left")
        ttk.Button(bar2, text="导入下方粘贴内容",
                   command=self.import_inst_paste).pack(side="left", padx=8)
        self.lbl_inst_stat = ttk.Label(
            bar2, text="格式: 每行一条机构名(可带序号)", foreground="#777777")
        self.lbl_inst_stat.pack(side="left", padx=6)
        self.txt_inst_paste = tk.Text(f2, height=3, wrap="none",
                                      font=("Consolas", 9))
        self.txt_inst_paste.pack(fill="x", padx=10, pady=(0, 8))

        # ③ 号池区
        f2 = ttk.LabelFrame(self, text=" ③ 号池(pool_data/ 文件夹内全部json自动合并) ")
        f2.pack(fill="both", padx=12, pady=4)
        bar2 = ttk.Frame(f2)
        bar2.pack(fill="x", padx=10, pady=(6, 2))
        self.lbl_pool = ttk.Label(bar2, text="…")
        self.lbl_pool.pack(side="left")
        self.lbl_mat = ttk.Label(bar2, text="", foreground="#777777")
        self.lbl_mat.pack(side="left", padx=14)
        self.btn_reset = ttk.Button(bar2, text="重置失败账号",
                                    command=self.reset_failed, width=12)
        self.btn_reset.pack(side="right")
        ttk.Button(bar2, text="刷新", command=self.refresh_pool, width=8).pack(side="right", padx=6)
        ttk.Button(bar2, text="导出Excel…", command=self.export_excel,
                   width=12).pack(side="right")
        self.btn_archive = ttk.Button(bar2, text="归档资料不全", width=12,
                                      command=self.archive_bad_materials)
        self.btn_archive.pack(side="right", padx=6)
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
        f3 = ttk.LabelFrame(self, text=" ④ 自动注册 + 企业认证 + 设置密码 ")
        f3.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        bar3 = ttk.Frame(f3)
        bar3.pack(fill="x", padx=10, pady=(8, 6))
        ttk.Label(bar3, text="本次消耗:").pack(side="left")
        self.cmb_count = ttk.Combobox(
            bar3, values=("1", "5", "10", "20", "max"), width=5, state="readonly")
        self.cmb_count.current(0)
        self.cmb_count.pack(side="left", padx=(4, 14))
        self.chk_headless = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar3, text="无头模式(后台运行不弹浏览器)",
                        variable=self.chk_headless).pack(side="left")
        self.chk_autocert = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar3, text="注册后自动企业认证",
                        variable=self.chk_autocert).pack(side="left", padx=(14, 0))
        self.chk_autopwd = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar3, text="注册后自动设置密码",
                        variable=self.chk_autopwd).pack(side="left", padx=(14, 0))
        self.btn_stop = ttk.Button(bar3, text="■ 停止", command=self.stop_register, state="disabled")
        self.btn_stop.pack(side="right", padx=8)
        self.btn_start = ttk.Button(bar3, text="▶ 开始注册", command=self.start_register)
        self.btn_start.pack(side="right")
        self.btn_cert = ttk.Button(bar3, text="▶ 认证已注册账号", command=self.start_certify)
        self.btn_cert.pack(side="right", padx=(0, 8))
        self.btn_pwd = ttk.Button(bar3, text="▶ 设置密码", command=self.start_pwd)
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
            new, dup = institutions.import_text(text, source)
        except Exception as e:
            messagebox.showerror("导入失败", str(e), parent=self)
            return
        parts = [f"新增 {new}"]
        if dup:
            parts.append(f"重复跳过 {dup}")
        self.lbl_inst_stat.config(text="  |  ".join(parts))
        self._append_log(f"[机构名导入:{source}] " + "; ".join(parts),
                         "ok" if new else "warn")
        self.refresh_pool()
        if new == 0 and dup > 0:
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
            ok_c, used_c, problems = materials.summary()
            tip = f"公司资料: 可用 {ok_c} / 已绑定 {used_c}"
            if problems:
                tip += f" / 资料不全 {len(problems)}"
            self.lbl_mat.config(text=tip)
        except Exception:
            self.lbl_mat.config(text="公司资料: 不可用(需 资料/ 文件夹 + openpyxl)")
        try:
            unused_i, used_i = institutions.summary()
            self.lbl_inst_stat.config(
                text=f"机构名库: 总数 {unused_i + used_i}    可用 {unused_i}    已用 {used_i}")
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
        ws.append(["邮箱", "密码", "密码状态", "企业认证公司", "认证状态", "注册时间"])
        for a in rows:
            ws.append([
                a["email"], a.get("password", ""),
                PWD_CN.get(a.get("pwd_status"), "未设置"),
                a.get("cert_company") or "",
                CERT_CN.get(a.get("cert_status"), "—"),
                a.get("registered_at") or "",
            ])
        for col, w in zip("ABCDEF", (34, 16, 12, 30, 10, 20)):
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

    # ---------- 注册 ----------
    def start_register(self, confirm: bool = True):
        if self.worker and self.worker.is_alive():
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
        preview = "\n".join(a["email"] for a in targets[:5])
        if len(targets) > 5:
            preview += f"\n… 等共 {len(targets)} 个"
        autocert = self.chk_autocert.get()
        if autocert:
            try:
                ok_c, used_c, _ = materials.summary()
                if ok_c == 0:
                    self._append_log("没有资料完整的可用公司, 本次注册将跳过自动认证", "warn")
                    autocert = False
            except Exception as e:
                self._append_log(f"公司资料解析失败({e}), 本次注册将跳过自动认证", "warn")
                autocert = False
        autopwd = self.chk_autopwd.get()
        cert_tip = ("\n\n已勾选[注册后自动企业认证]: 每个账号注册成功后立即绑定一家公司提交认证"
                    "\n(公司资料一司一号, 用量自动跟踪)。" if autocert else "")
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
        self._append_log(f"开始注册 {len(targets)} 个账号"
                         f"(无头模式={'开' if headless else '关'}, "
                         f"自动认证={'开' if autocert else '关'}, "
                         f"自动密码={'开' if autopwd else '关'})", "warn")
        self.worker = threading.Thread(
            target=self._work, args=(targets, headless, autocert, autopwd), daemon=True)
        self.worker.start()

    def _work(self, targets, headless: bool, autocert: bool, autopwd: bool):
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

        try:
            reg = Registrar(targets, headless=headless,
                            log=log, progress=prog, stop_event=self.stop_event,
                            after_register=after_register)
            ok, fail = reg.run()
            self.q.put(("done", ok, fail, self.stop_event.is_set()))
        except Exception as e:
            log(f"引擎异常: {e}", "err")
            self.q.put(("done", 0, total, True))

    # ---------- 独立企业认证 ----------
    def start_certify(self, confirm: bool = True):
        if self.worker and self.worker.is_alive():
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
        if not ok_c:
            msg = "没有资料完整的可用公司(全部已绑定或资料不全)。"
            if problems:
                msg += "\n\n资料不全的公司:\n" + "\n".join(problems[:6])
            messagebox.showwarning("提示", msg, parent=self)
            return
        if ok_c < len(targets):
            self._append_log(f"可用公司 {ok_c} 家 < 待认证账号 {len(targets)} 个, "
                             f"本次只处理前 {ok_c} 个账号", "warn")
            targets = targets[:ok_c]
        preview = "\n".join(a["email"] for a in targets[:5])
        if len(targets) > 5:
            preview += f"\n… 等共 {len(targets)} 个"
        if confirm and not messagebox.askyesno(
                "确认开始",
                f"将为以下 {len(targets)} 个已注册账号逐个提交企业认证:\n\n{preview}\n\n"
                f"公司资料: 可用 {ok_c} 家 / 已绑定 {used_c} 家\n"
                "每个账号自动绑定一家公司(按资料表顺序), 提交后该公司不再复用;\n"
                "登录会话过期时自动用邮箱验证码重登。\n确定开始?", parent=self):
            return
        self.stop_event = threading.Event()
        self._set_running(True)
        self.progress.config(maximum=len(targets), value=0)
        headless = self.chk_headless.get()
        self._append_log(f"开始企业认证 {len(targets)} 个账号"
                         f"(无头模式={'开' if headless else '关'})", "warn")
        self.worker = threading.Thread(
            target=self._work_cert, args=(targets, headless), daemon=True)
        self.worker.start()

    def _work_cert(self, targets, headless: bool):
        total = len(targets)

        def log(msg, level="info"):
            self.q.put(("log", level, f"[{datetime.now():%H:%M:%S}] {msg}"))

        def prog(done, tot):
            self.q.put(("progress", done, tot))

        try:
            cert = certifier.Certifier(targets, headless=headless,
                                       log=log, progress=prog,
                                       stop_event=self.stop_event)
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
        self._append_log(f"开始设置密码 {len(targets)} 个账号"
                         f"(无头模式={'开' if headless else '关'})", "warn")
        self.worker = threading.Thread(
            target=self._work_pwd, args=(targets, headless), daemon=True)
        self.worker.start()

    def _work_pwd(self, targets, headless: bool):
        total = len(targets)

        def log(msg, level="info"):
            self.q.put(("log", level, f"[{datetime.now():%H:%M:%S}] {msg}"))

        def prog(done, tot):
            self.q.put(("progress", done, tot))

        try:
            setter = password_setter.PasswordSetter(
                targets, headless=headless, log=log, progress=prog,
                stop_event=self.stop_event)
            ok, fail, skip = setter.run()
            self.q.put(("pwd_done", ok, fail, skip, self.stop_event.is_set()))
        except Exception as e:
            log(f"引擎异常: {e}", "err")
            self.q.put(("pwd_done", 0, total, 0, True))

    def stop_register(self):
        if self.stop_event:
            self.stop_event.set()
            self._append_log("已请求停止, 等待当前步骤结束…", "warn")

    # ---------- 事件循环 ----------
    def _drain(self):
        try:
            while True:
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
                            "公司绑定关系见 资料/usage.json, 过程截图见 logs/。", parent=self)
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
        except queue.Empty:
            pass
        self.after(150, self._drain)

    def _set_running(self, running: bool):
        st = "disabled" if running else "normal"
        self.btn_start.config(state=st)
        self.btn_cert.config(state=st)
        self.btn_pwd.config(state=st)
        self.btn_reset.config(state=st)
        self.btn_archive.config(state=st)
        self.cmb_count.config(state="disabled" if running else "readonly")
        self.btn_stop.config(state="normal" if running else "disabled")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("确认退出", "任务进行中, 退出将中止。确定退出吗?", parent=self):
                return
            if self.stop_event:
                self.stop_event.set()
        self.destroy()

    def _append_log(self, text: str, level: str = "info"):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n", level if level in LOG_TAGS else "info")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        try:
            with open(ROOT / "gui.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {text}\n")
        except OSError:
            pass

def run(smoke: bool = False, auto: int = 0) -> None:
    pool.migrate_legacy()
    app = App(auto_close=auto > 0)
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
