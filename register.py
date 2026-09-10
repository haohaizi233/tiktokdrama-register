# -*- coding: utf-8 -*-
"""命令行版自动注册+企业认证(GUI版请用 run_gui.py / 启动.bat)

用法:
  python register.py                  # 取1个可用账号注册(默认自动企业认证+设置密码)
  python register.py --count 3        # 依次3个
  python register.py --email a@b.com  # 指定账号
  python register.py --no-cert        # 只注册(不做认证/密码等附加任务)
  python register.py --cert           # 不注册, 只给已注册未认证账号做企业认证
  python register.py --pwd            # 不注册, 只给已注册账号设置账户密码
  python register.py --headless       # 无头模式
  python register.py --hubstudio      # 接管HubStudio指纹浏览器环境(VIP套餐)
  python register.py --rotate-ip      # 配合--hubstudio: 记录每账号出口IP(代理自动隔离, 零额度)
"""
import argparse
import sys

import pool
from registrar import Registrar


def _after_tasks_hook(do_cert: bool = True):
    import password_setter

    def hook(page, email, shoot=None):
        print(f"--- 注册后附加任务(企业认证+设置密码): {email} ---", flush=True)
        password_setter.after_register_tasks(
            page, email, shoot=shoot, do_cert=do_cert)

    return hook


def run_cert(args) -> int:
    import certifier

    accounts = pool.all_accounts()
    targets = [a for a in accounts
               if a.get("status") == "registered" and a.get("cert_status") != "submitted"]
    if not targets:
        print("[提示] 没有待认证的已注册账号。")
        return 0
    runnable, skipped = pool.select_certifiable(targets)
    for email, reason in skipped:
        print(f"  跳过 {email}: {reason}")
    if not runnable:
        print("[错误] 没有可认证账号(无空槽/机构名, 且没有可续上的失败绑定)。")
        return 1
    if skipped:
        print(f"[提示] 库存/续用限制: {len(targets)} → 本次 {len(runnable)}")
    print(f"本次认证 {len(runnable)} 个账号: {[a['email'] for a in runnable]}")
    ok, fail, skip = certifier.Certifier(
        runnable, headless=args.headless, code_timeout=args.code_timeout,
        use_hubstudio=args.hubstudio, rotate_ip=args.rotate_ip).run()
    print(f"========== 认证汇总: 成功{ok} 失败{fail} 跳过{skip} ==========")
    return 0 if fail == 0 else 2


def run_pwd(args) -> int:
    import password_setter

    accounts = pool.all_accounts()
    targets = [a for a in accounts
               if a.get("status") == "registered"
               and a.get("pwd_status") not in ("success", "already")]
    if not targets:
        print("[提示] 没有待设置密码的已注册账号。")
        return 0
    print(f"本次设置密码 {len(targets)} 个账号: {[a['email'] for a in targets]}")
    ok, fail, skip = password_setter.PasswordSetter(
        targets, headless=args.headless, code_timeout=args.code_timeout,
        use_hubstudio=args.hubstudio, rotate_ip=args.rotate_ip).run()
    print(f"========== 密码设置汇总: 成功{ok} 失败{fail} 跳过{skip} ==========")
    return 0 if fail == 0 else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="tiktokdramacenter 自动注册+企业认证+设置密码(CLI)")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--email", type=str)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--hubstudio", action="store_true",
                        help="接管HubStudio指纹浏览器环境(需客户端运行+VIP套餐)")
    parser.add_argument("--rotate-ip", action="store_true",
                        help="配合--hubstudio: 实测并记录每账号出口IP"
                             "(代理对每个窗口自动分配独立IP, 零额度)")
    parser.add_argument("--code-timeout", type=int, default=180)
    parser.add_argument("--no-cert", action="store_true",
                        help="只注册, 不做企业认证/设置密码等附加任务")
    parser.add_argument("--cert", action="store_true",
                        help="只认证: 批量处理已注册未认证账号(不消耗新账号)")
    parser.add_argument("--pwd", action="store_true",
                        help="只设置密码: 批量给已注册账号设置账户密码(不消耗新账号)")
    args = parser.parse_args()

    pool.migrate_legacy()
    stats = pool.reclaim_stale_assignments()
    if stats["slots"] or stats["institutions"]:
        print(f"[提示] 已回收未提交占用: 公司槽 {len(stats['slots'])} 个, "
              f"机构名 {len(stats['institutions'])} 个")
    if args.cert:
        sys.exit(run_cert(args))
    if args.pwd:
        sys.exit(run_pwd(args))

    accounts = pool.all_accounts()
    if args.email:
        targets = [a for a in accounts if a["email"] == args.email]
        if not targets:
            print(f"[错误] 号池中不存在 {args.email}")
            sys.exit(1)
    else:
        targets = [a for a in accounts if a["status"] == "unused"][: args.count]
    if not targets:
        print("[提示] 没有可用账号, 请先导入txt(或运行GUI导入)。")
        sys.exit(0)

    do_cert = not args.no_cert
    if do_cert:
        import institutions
        import materials
        try:
            ok_c, _, _ = materials.summary()
        except Exception as e:
            print(f"[提示] 公司资料解析失败({e}), 本次只注册+设密, 不做认证")
            do_cert = False
            ok_c = 0
        if do_cert:
            unused_i, _ = institutions.summary()
            cap = min(ok_c, unused_i)
            if cap <= 0:
                print("[提示] 无可用公司槽位或机构名, 本次只注册+设密, 不做认证")
                do_cert = False
            elif not args.email and cap < len(targets):
                print(f"[提示] 可用槽位/机构名仅 {cap} 个, 本次只注册前 {cap} 个"
                      " (避免注册后无法认证)")
                targets = targets[:cap]

    print(f"本次处理 {len(targets)} 个账号: {[a['email'] for a in targets]}")
    hook = None if args.no_cert else _after_tasks_hook(do_cert=do_cert)
    ok, fail = Registrar(targets, headless=args.headless,
                         code_timeout=args.code_timeout,
                         after_register=hook,
                         use_hubstudio=args.hubstudio,
                         rotate_ip=args.rotate_ip).run()
    print(f"========== 汇总: 成功{ok} 失败{fail} ==========")
    sys.exit(0 if fail == 0 else 2)


if __name__ == "__main__":
    main()
