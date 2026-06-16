#!/usr/bin/env python3
# ============================================================
#  monitor.py  —  @MainBot  |  Main VPS
#  Multi-node | Peak Hours (Tehran) | Jitter | همه features
# ============================================================
import os, re, subprocess, sys, time, threading, logging
from datetime import datetime
from typing import Optional, Dict, List

import psutil, requests, schedule

from monitor_config import (
    NODES, MARZBAN, TELEGRAM, THRESHOLDS,
    INTERVALS, LOCAL_SERVICES, LOG_LINES,
)
from tracker import NetworkTracker, MarzbanTracker, PeakHoursTracker, tehran_now

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("monitor.log"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

net_tracker  = NetworkTracker()
mzb_tracker  = MarzbanTracker()
peak_tracker = PeakHoursTracker()

_node_data:    Dict[str, Optional[dict]] = {n["key"]: None for n in NODES}
_node_last_ok: Dict[str, float]          = {n["key"]: 0.0  for n in NODES}
_alerts:       Dict[str, bool]           = {}
_mz_token:     Optional[str]             = None
_mz_token_ts:  float                     = 0.0
TOKEN_TTL      = 1800
_last_update_id = 0
NODE_MAP: Dict[str, dict] = {n["key"]: n for n in NODES}


# ── Telegram ─────────────────────────────────────────────────

def tg(text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM['token']}/sendMessage",
            json={"chat_id": TELEGRAM["chat_id"], "text": text[:4096],
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10)
        return r.ok
    except Exception as e:
        log.error(f"Telegram: {e}"); return False


def tg_alert(key: str, condition: bool, msg: str) -> None:
    prev = _alerts.get(key, False)
    if condition and not prev:
        tg(f"🚨 <b>ALERT</b>\n{msg}"); _alerts[key] = True
    elif not condition and prev:
        tg(f"✅ <b>RESOLVED</b>\n{msg}"); _alerts[key] = False


# ── Node fetching ─────────────────────────────────────────────

def fetch_node(cfg: dict) -> Optional[dict]:
    key = cfg["key"]
    try:
        r = requests.get(
            cfg["metrics_url"],
            headers={"X-API-Key": cfg["api_key"]},
            timeout=cfg.get("timeout", 15))
        if r.ok:
            _node_data[key]    = r.json()
            _node_last_ok[key] = time.time()
            return _node_data[key]
    except Exception as e:
        log.warning(f"Node [{key}]: {e}")
    return None


def fetch_all_nodes() -> None:
    threads = []
    for cfg in NODES:
        t = threading.Thread(target=fetch_node, args=(cfg,), daemon=True)
        threads.append(t); t.start()
    for t in threads:
        t.join(timeout=20)


def check_heartbeats() -> None:
    for cfg in NODES:
        key  = cfg["key"]
        last = _node_last_ok.get(key, 0)
        if not last: continue
        elapsed = time.time() - last
        tg_alert(f"hb_{key}", elapsed > INTERVALS["heartbeat"],
                 f"{cfg['name']}\n⏰ <b>{int(elapsed//60)} دقیقه</b> پاسخ نمی‌دهد!")


# ── Local metrics ─────────────────────────────────────────────

def collect_local_system() -> dict:
    cpu  = psutil.cpu_percent(interval=1)
    load = psutil.getloadavg()
    mem  = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    dio  = psutil.disk_io_counters()

    try:
        raw = subprocess.check_output(
            "df -i / | tail -1 | awk '{print $5}' | tr -d '%'",
            shell=True, timeout=5).decode().strip()
        inode_pct = int(raw) if raw.isdigit() else 0
    except Exception:
        inode_pct = 0

    cpu_temp: any = "N/A"
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for k in ("coretemp","cpu_thermal","acpitz","k10temp"):
                if k in temps and temps[k]:
                    cpu_temp = round(temps[k][0].current, 1); break
        if cpu_temp == "N/A":
            raw = subprocess.check_output(
                "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null",
                shell=True, timeout=3).decode().strip()
            if raw.isdigit(): cpu_temp = round(int(raw)/1000, 1)
    except Exception: pass

    up = int(time.time() - psutil.boot_time())
    d, r = divmod(up, 86400); h, r = divmod(r, 3600); m, _ = divmod(r, 60)
    return {
        "cpu_percent":      cpu,
        "load_1":           round(load[0], 2), "load_5": round(load[1], 2), "load_15": round(load[2], 2),
        "ram_total_mb":     mem.total     // 1048576, "ram_used_mb":  mem.used      // 1048576,
        "ram_free_mb":      mem.available // 1048576, "ram_percent":  mem.percent,
        "swap_total_mb":    swap.total    // 1048576, "swap_used_mb": swap.used     // 1048576,
        "swap_percent":     round(swap.percent, 1),
        "disk_total_gb":    round(disk.total/1073741824, 1), "disk_used_gb": round(disk.used/1073741824, 1),
        "disk_free_gb":     round(disk.free/1073741824, 1),  "disk_percent": disk.percent,
        "inode_percent":    inode_pct,
        "disk_io_read_mb":  round(dio.read_bytes/1048576,1)  if dio else 0,
        "disk_io_write_mb": round(dio.write_bytes/1048576,1) if dio else 0,
        "cpu_temp":         cpu_temp,
        "uptime":           f"{d}d {h}h {m}m",
    }


def collect_local_network() -> dict:
    conns = psutil.net_connections(kind="tcp")
    nio   = psutil.net_io_counters()
    return {
        "tcp_established": sum(1 for c in conns if c.status=="ESTABLISHED"),
        "tcp_time_wait":   sum(1 for c in conns if c.status=="TIME_WAIT"),
        "tcp_close_wait":  sum(1 for c in conns if c.status=="CLOSE_WAIT"),
        "net_rx_gb":       round(nio.bytes_recv/1073741824, 4),
        "net_tx_gb":       round(nio.bytes_sent/1073741824, 4),
        "ping_to_node_ms": -1, "packet_loss_pct": -1, "jitter_ms": -1,
    }


def collect_local_services() -> dict:
    r = {}
    for svc in LOCAL_SERVICES:
        try:
            res = subprocess.run(["systemctl","is-active",svc],
                                 capture_output=True, text=True, timeout=5)
            r[svc] = res.stdout.strip() == "active"
        except Exception:
            r[svc] = False
    return r


def collect_local_logs() -> dict:
    cmds = {
        "xray":    f"journalctl -u xray -n {LOG_LINES} --no-pager 2>/dev/null | grep -iE 'error|warn|fail' | tail -10",
        "marzban": f"journalctl -u marzban -n {LOG_LINES} --no-pager 2>/dev/null | grep -iE 'error|warn|fail' | tail -10",
        "nginx":   f"tail -n {LOG_LINES} /var/log/nginx/error.log 2>/dev/null | tail -10",
        "docker":  f"journalctl -u docker -n {LOG_LINES} --no-pager 2>/dev/null | grep -iE 'error|fail' | tail -5",
    }
    logs = {}
    for svc, cmd in cmds.items():
        try:
            out = subprocess.check_output(cmd, shell=True, timeout=6).decode(errors="replace").strip()
            logs[svc] = out[:500] if out else ""
        except Exception:
            logs[svc] = ""
    return logs


# ── Marzban ──────────────────────────────────────────────────

def _mz_auth() -> Optional[str]:
    global _mz_token, _mz_token_ts
    if _mz_token and (time.time() - _mz_token_ts) < TOKEN_TTL:
        return _mz_token
    try:
        r = requests.post(f"{MARZBAN['url']}/api/admin/token",
                          data={"username": MARZBAN["user"], "password": MARZBAN["pass"]},
                          timeout=10)
        _mz_token    = r.json().get("access_token")
        _mz_token_ts = time.time()
        return _mz_token
    except Exception as e:
        log.warning(f"Marzban: {e}"); return None


def fetch_marzban_users() -> list:
    token = _mz_auth()
    if not token: return []
    try:
        r = requests.get(f"{MARZBAN['url']}/api/users?limit=500",
                         headers={"Authorization": f"Bearer {token}"}, timeout=20)
        if r.status_code == 401:
            global _mz_token; _mz_token = None; return []
        return r.json().get("users", [])
    except Exception as e:
        log.warning(f"Marzban users: {e}"); return []


def fetch_marzban_summary() -> dict:
    token = _mz_auth()
    if not token: return {"error": "unreachable"}
    h = {"Authorization": f"Bearer {token}"}
    try:
        sys_d = requests.get(f"{MARZBAN['url']}/api/system", headers=h, timeout=10).json()
        users = requests.get(f"{MARZBAN['url']}/api/users?limit=500", headers=h, timeout=15).json().get("users",[])
        now   = time.time()
        xray_c = 0
        try:
            inb = requests.get(f"{MARZBAN['url']}/api/inbounds", headers=h, timeout=10).json()
            if isinstance(inb, list): xray_c = sum(len(i.get("users",[])) for i in inb)
        except Exception: pass
        return {
            "users_total":        len(users),
            "users_online":       sum(1 for u in users if u.get("online_at") and (now-(u["online_at"] or 0))<180),
            "users_active":       sys_d.get("users_active", 0),
            "users_expired":      sum(1 for u in users if u.get("expire") and u["expire"]<now),
            "incoming_bandwidth": sys_d.get("incoming_bandwidth", 0),
            "outgoing_bandwidth": sys_d.get("outgoing_bandwidth", 0),
            "xray_version":       sys_d.get("xray_version", "?"),
            "xray_connections":   xray_c,
        }
    except Exception as e:
        return {"error": str(e)}


# ── Alerts ───────────────────────────────────────────────────

def run_alerts(label: str, key: str, s: dict, n: dict, svc: dict) -> None:
    T = THRESHOLDS
    tg_alert(f"{key}_cpu",  s.get("cpu_percent",  0) > T["cpu_percent"],
             f"<b>{label}</b>\n🔥 CPU: {s.get('cpu_percent',0):.1f}%")
    tg_alert(f"{key}_ram",  s.get("ram_percent",  0) > T["ram_percent"],
             f"<b>{label}</b>\n💾 RAM: {s.get('ram_percent',0):.1f}%")
    tg_alert(f"{key}_disk", s.get("disk_percent", 0) > T["disk_percent"],
             f"<b>{label}</b>\n💿 Disk: {s.get('disk_percent',0):.1f}%")
    tg_alert(f"{key}_load", s.get("load_1", 0) > T["load_avg_1"],
             f"<b>{label}</b>\n⚡ Load: {s.get('load_1',0):.2f}")
    p = n.get("ping_to_main_ms", n.get("ping_to_node_ms", -1))
    if p > 0:
        tg_alert(f"{key}_ping", p > T["ping_ms"],
                 f"<b>{label}</b>\n📡 Ping: {p:.1f}ms")
    loss = n.get("packet_loss_pct", -1)
    if loss >= 0:
        tg_alert(f"{key}_loss", loss > T["packet_loss"],
                 f"<b>{label}</b>\n📉 Loss: {loss:.0f}%")
    jit = n.get("jitter_ms", -1)
    if jit >= 0:
        tg_alert(f"{key}_jitter", jit > T["jitter_ms"],
                 f"<b>{label}</b>\n📊 Jitter: {jit:.1f}ms")
    temp = s.get("cpu_temp")
    if isinstance(temp, (int, float)):
        tg_alert(f"{key}_temp", temp > T["cpu_temp"],
                 f"<b>{label}</b>\n🌡️ Temp: {temp}°C")
    for name, up in svc.items():
        tg_alert(f"{key}_{name}", not up,
                 f"<b>{label}</b>\n⚠️ سرویس <b>{name}</b> DOWN!")


def check_expiring() -> None:
    if not mzb_tracker.is_fresh: return
    exp   = mzb_tracker.expiring_soon()
    count = len(exp["in_24h"])
    if count:
        names = ", ".join(u["username"] for u in exp["in_24h"][:5])
        tg_alert("expiring_24h", count > 0,
                 f"⏰ <b>{count}</b> کاربر ۲۴h دیگر منقضی: {names}")


# ── Formatting ───────────────────────────────────────────────

def _bar(pct: float, w: int = 10) -> str:
    f = int(pct/100*w)
    return "[" + "█"*f + "░"*(w-f) + "]"

def _ok(v: bool) -> str: return "🟢" if v else "🔴"

def _gb(b: int) -> str:
    if b >= 1073741824: return f"{b/1073741824:.2f} GB"
    if b >= 1048576:    return f"{b/1048576:.1f} MB"
    return f"{b/1024:.0f} KB"


def fmt_server(label: str, key: str, s: dict, n: dict, svc: dict) -> str:
    rate    = net_tracker.get_rate(key)
    win_lbl = f"{rate['window_min']:.0f} min" if rate["window_min"] > 0 else "جمع‌آوری..."
    ping_val = n.get("ping_to_main_ms", n.get("ping_to_node_ms", -1))
    ping_lbl = "→ Main" if "ping_to_main_ms" in n else "→ Node"
    loss     = n.get("packet_loss_pct", -1)
    jit      = n.get("jitter_ms", -1)
    now_str  = datetime.now().strftime("%H:%M  %Y-%m-%d")

    lines = [
        f"<b>{'─'*32}</b>",
        f"<b>📊 {label}</b>  •  {now_str}",
        f"⏱ <code>{s.get('uptime','?')}</code>",
        "",
        "<b>💻 سرور</b>",
        f"  CPU    {s.get('cpu_percent',0):.1f}%  {_bar(s.get('cpu_percent',0))}",
        f"  Load   {s.get('load_1',0):.2f} / {s.get('load_5',0):.2f} / {s.get('load_15',0):.2f}",
        f"  RAM    {s.get('ram_used_mb',0):,} / {s.get('ram_total_mb',0):,} MB  ({s.get('ram_percent',0):.0f}%)",
        f"  Swap   {s.get('swap_used_mb',0)} / {s.get('swap_total_mb',0)} MB  ({s.get('swap_percent',0):.0f}%)",
        f"  Disk   {s.get('disk_used_gb',0)} / {s.get('disk_total_gb',0)} GB  ({s.get('disk_percent',0):.0f}%)",
        f"  Inode  {s.get('inode_percent',0)}%  |  Temp  {s.get('cpu_temp','N/A')}{'°C' if isinstance(s.get('cpu_temp'), float) else ''}",
        "",
        "<b>🌐 شبکه</b>",
        f"  Ping {ping_lbl}:   {'N/A' if ping_val < 0 else f'{ping_val:.1f} ms'}",
        f"  Jitter:         {'N/A' if jit < 0 else f'{jit:.1f} ms'}",
        f"  Packet Loss:    {'N/A' if loss < 0 else f'{loss:.0f}%'}",
        f"  ESTAB / TW / CW:  {n.get('tcp_established',0)} / {n.get('tcp_time_wait',0)} / {n.get('tcp_close_wait',0)}",
        "",
        f"<b>📈 ترافیک  <i>({win_lbl})</i></b>",
        f"  📥 RX: {rate['rx_mb_per_min']:.2f} MB/min",
        f"  📤 TX: {rate['tx_mb_per_min']:.2f} MB/min",
        f"  کل RX/TX: {n.get('net_rx_gb',0):.2f} / {n.get('net_tx_gb',0):.2f} GB",
        "",
        "<b>⚙️ سرویس‌ها</b>",
    ]
    for name, up in svc.items():
        lines.append(f"  {_ok(up)} {name}")
    return "\n".join(lines)


def fmt_nodes_summary() -> str:
    now_str = datetime.now().strftime("%H:%M  %Y-%m-%d")
    lines   = [f"<b>📡 خلاصه نودها</b>  •  {now_str}", ""]

    for cfg in NODES:
        key  = cfg["key"]
        data = _node_data.get(key)
        last = _node_last_ok.get(key, 0)

        if not data:
            lines.append(f"🔴 <b>{cfg['name']}</b> — بدون پاسخ\n")
            continue

        s    = data.get("system",   {})
        n    = data.get("network",  {})
        svc  = data.get("services", {})
        rate = net_tracker.get_rate(key)
        ping = n.get("ping_to_main_ms", -1)
        jit  = n.get("jitter_ms", -1)
        loss = n.get("packet_loss_pct", -1)
        age  = int(time.time() - last) if last else -1

        svc_icons = "".join(_ok(up) for up in svc.values())

        lines += [
            f"🟢 <b>{cfg['name']}</b>  <i>{age}s</i>",
            f"  CPU {s.get('cpu_percent',0):.0f}%  RAM {s.get('ram_percent',0):.0f}%  Disk {s.get('disk_percent',0):.0f}%",
            f"  Ping {'N/A' if ping<0 else f'{ping:.0f}ms'}  "
            f"Jitter {'N/A' if jit<0 else f'{jit:.1f}ms'}  "
            f"Loss {'N/A' if loss<0 else f'{loss:.0f}%'}",
            f"  📥 {rate['rx_mb_per_min']:.1f}  📤 {rate['tx_mb_per_min']:.1f} MB/min",
            f"  {svc_icons}",
            "",
        ]
    return "\n".join(lines)


def fmt_users() -> str:
    if not mzb_tracker.is_fresh:
        return "<b>👥 کاربران</b>\n⚠️ داده‌ای موجود نیست"
    summ  = mzb_tracker.summary()
    new_c = mzb_tracker.new_users_count()
    top   = mzb_tracker.top_users(5)
    exp   = mzb_tracker.expiring_soon()
    sold  = mzb_tracker.sold_traffic()
    lines = [
        "<b>👥 کاربران — پنل اصلی</b>", "",
        "<b>📊 وضعیت کلی</b>",
        f"  👥 کل:               {summ['total']}",
        f"  🟢 آنلاین (< 3min):  {summ['online_now']}",
        f"  ⏱ فعال ۱۰ دقیقه:    {summ['active_10min']}",
        f"  ❌ منقضی‌شده:        {summ['expired']}",
        "",
        "<b>🆕 کاربران جدید</b>",
        f"  امروز:     {new_c['day']}",
        f"  این هفته:  {new_c['week']}",
        f"  این ماه:   {new_c['month']}",
        "",
        "<b>📦 حجم فروخته‌شده</b>",
        f"  کل:        {sold['all']:.1f} GB",
        f"  این ماه:   {sold['month']:.1f} GB",
        f"  این هفته:  {sold['week']:.1f} GB",
        f"  امروز:     {sold['day']:.1f} GB",
    ]
    if exp["in_24h"] or exp["in_7d"]:
        lines += ["", "<b>⏰ در حال انقضا</b>"]
        if exp["in_24h"]:
            names = "  |  ".join(f"{u['username']} ({u['remaining']})" for u in exp["in_24h"][:5])
            lines.append(f"  🔴 ۲۴h: {names}")
        if exp["in_7d"]:
            names = "  |  ".join(f"{u['username']} ({u['remaining']})" for u in exp["in_7d"][:5])
            lines.append(f"  🟡 ۷d:  {names}")
    if top:
        lines += ["", "<b>🏆 پرمصرف‌ترین‌ها</b>"]
        for i, u in enumerate(top, 1):
            pct   = f"  {u['percent']}%" if u["percent"] is not None else ""
            limit = f"/ {u['limit_gb']} GB" if u["limit_gb"] else "/ ∞"
            mark  = "🟢" if u["online"] else "⚫"
            lines.append(f"  {i}. {mark} {u['username']:20s} {u['used_gb']} {limit}{pct}")
    upd = datetime.fromtimestamp(summ["updated_at"]).strftime("%H:%M:%S") if summ["updated_at"] else "?"
    lines += ["", f"<i>آخرین به‌روزرسانی: {upd}</i>"]
    return "\n".join(lines)


def fmt_vpn_quick() -> str:
    if not mzb_tracker.is_fresh:
        return "<b>🔐 VPN</b>\n⚠️ داده قدیمی"
    s    = mzb_tracker.summary()
    sold = mzb_tracker.sold_traffic()
    rate = net_tracker.get_rate("main")
    mzb  = fetch_marzban_summary()
    lines = [
        "<b>🔐 VPN — پنل اصلی</b>", "",
        f"  👥 {s['total']}  |  🟢 {s['online_now']} آنلاین  |  ⏱ {s['active_10min']} (10min)",
        f"  ❌ منقضی: {s['expired']}",
    ]
    if "error" not in mzb:
        lines += [
            "", f"  📥 {_gb(mzb.get('incoming_bandwidth',0))}  📤 {_gb(mzb.get('outgoing_bandwidth',0))}",
            f"  Xray v{mzb.get('xray_version','?')}  |  🔗 {mzb.get('xray_connections',0)} conn",
        ]
    lines += [
        "", "<b>📦 حجم فروخته‌شده</b>",
        f"  کل: {sold['all']:.1f} GB  |  ماه: {sold['month']:.1f} GB",
        f"  هفته: {sold['week']:.1f} GB  |  امروز: {sold['day']:.1f} GB",
        "", f"<b>📈 نرخ ترافیک  ({rate['window_min']:.0f} min)</b>",
        f"  📥 {rate['rx_mb_per_min']:.2f}  📤 {rate['tx_mb_per_min']:.2f} MB/min",
    ]
    return "\n".join(lines)


def _fmt_logs(label: str, logs: dict) -> str:
    lines = [f"<b>📋 لاگ‌ها — {label}</b>", ""]
    has = False
    for svc, content in logs.items():
        if content.strip():
            has = True
            lines += [f"<b>▸ {svc}:</b>", f"<code>{content[:300]}</code>", ""]
    if not has: lines.append("✅ خطایی یافت نشد")
    return "\n".join(lines)


# ── Scheduled jobs ────────────────────────────────────────────

def job_poll_nodes() -> None:
    log.info("Polling nodes...")
    fetch_all_nodes()
    check_heartbeats()
    for cfg in NODES:
        key  = cfg["key"]
        data = _node_data.get(key)
        if not data: continue
        net  = data.get("network", {})
        rate = net_tracker.get_rate(key)
        net_tracker.add_sample(key, net.get("net_rx_gb",0), net.get("net_tx_gb",0))
        peak_tracker.add_sample(key, rate["rx_mb_per_min"], rate["tx_mb_per_min"])
        run_alerts(cfg["name"], key, data.get("system",{}), net, data.get("services",{}))


def job_poll_local() -> None:
    log.info("Polling main...")
    s   = collect_local_system()
    n   = collect_local_network()
    svc = collect_local_services()
    rate = net_tracker.get_rate("main")
    net_tracker.add_sample("main", n.get("net_rx_gb",0), n.get("net_tx_gb",0))
    peak_tracker.add_sample("main", rate["rx_mb_per_min"], rate["tx_mb_per_min"])
    run_alerts("🏠 Main VPS", "main", s, n, svc)


def job_fetch_users() -> None:
    log.info("Fetching users...")
    users = fetch_marzban_users()
    if users: mzb_tracker.update(users)
    check_expiring()


def job_check_logs() -> None:
    log.info("Checking logs...")
    ll = collect_local_logs()
    if any(v.strip() for v in ll.values()): tg(_fmt_logs("🏠 Main VPS", ll))
    for cfg in NODES:
        data = _node_data.get(cfg["key"])
        if data and data.get("logs"):
            nl = data["logs"]
            if any(v.strip() for v in nl.values()): tg(_fmt_logs(cfg["name"], nl))


def job_periodic_report() -> None:
    log.info("Periodic report...")
    s   = collect_local_system()
    n   = collect_local_network()
    svc = collect_local_services()
    tg(fmt_server("🏠 Main VPS", "main", s, n, svc))
    tg(fmt_nodes_summary())


def job_daily_report() -> None:
    log.info("Daily report...")
    tg(f"<b>📅 گزارش روزانه  {datetime.now().strftime('%Y-%m-%d')}</b>\n\n" + fmt_users())


# ── Commands ─────────────────────────────────────────────────

def poll_commands() -> None:
    global _last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM['token']}/getUpdates",
            params={"offset": _last_update_id+1, "timeout": 5}, timeout=10)
        for upd in r.json().get("result", []):
            _last_update_id = upd["update_id"]
            msg     = upd.get("message", {})
            text    = msg.get("text", "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            admins  = [str(x) for x in TELEGRAM.get("admin_ids", [])]
            if admins and chat_id not in admins: continue
            threading.Thread(target=dispatch, args=(text,), daemon=True).start()
    except Exception as e:
        log.warning(f"Poll: {e}")


def dispatch(cmd: str) -> None:
    static = {
        "/status":  lambda: (_cmd_main(), tg(fmt_nodes_summary())),
        "/وضعیت":  lambda: (_cmd_main(), tg(fmt_nodes_summary())),
        "/main":    _cmd_main,
        "/nodes":   lambda: tg(fmt_nodes_summary()),
        "/نودها":   lambda: tg(fmt_nodes_summary()),
        "/vpn":     lambda: tg(fmt_vpn_quick()),
        "/users":   _cmd_users,
        "/کاربران": _cmd_users,
        "/peak":    _cmd_peak_all,
        "/پیک":     _cmd_peak_all,
        "/logs":    _cmd_logs,
        "/لاگ":     _cmd_logs,
        "/report":  job_periodic_report,
        "/گزارش":   job_periodic_report,
        "/help":    _cmd_help,
        "/start":   _cmd_help,
        "/راهنما":  _cmd_help,
    }
    fn = static.get(cmd)
    if fn: fn(); return

    for cfg in NODES:
        key = cfg["key"]
        if cmd in (f"/{key}", f"/node_{key}"):
            _cmd_single_node(cfg); return
        if cmd in (f"/peak_{key}", f"/پیک_{key}"):
            tg(peak_tracker.fmt(cfg["name"], key)); return
        if cmd == f"/logs_{key}":
            data = _node_data.get(key)
            if data and data.get("logs"): tg(_fmt_logs(cfg["name"], data["logs"]))
            return


def _cmd_main() -> None:
    s = collect_local_system(); n = collect_local_network(); svc = collect_local_services()
    tg(fmt_server("🏠 Main VPS", "main", s, n, svc))


def _cmd_single_node(cfg: dict) -> None:
    data = fetch_node(cfg) or _node_data.get(cfg["key"])
    if not data: tg(f"⚠️ {cfg['name']} پاسخ نمی‌دهد."); return
    tg(fmt_server(cfg["name"], cfg["key"],
                  data.get("system",{}), data.get("network",{}), data.get("services",{})))


def _cmd_users() -> None:
    if not mzb_tracker.is_fresh:
        tg("⏳ در حال دریافت..."); job_fetch_users()
    tg(fmt_users())


def _cmd_peak_all() -> None:
    tg(peak_tracker.fmt("🏠 Main VPS", "main"))
    for cfg in NODES:
        tg(peak_tracker.fmt(cfg["name"], cfg["key"]))


def _cmd_logs() -> None:
    tg(_fmt_logs("🏠 Main VPS", collect_local_logs()))
    for cfg in NODES:
        data = _node_data.get(cfg["key"])
        if data and data.get("logs"): tg(_fmt_logs(cfg["name"], data["logs"]))


def _cmd_help() -> None:
    node_cmds = "\n".join(f"/{n['key']:10s} —  {n['name']}" for n in NODES)
    peak_cmds = "\n".join(f"/peak_{n['key']:6s} —  پیک {n['name']}" for n in NODES)
    tg(
        "🤖 <b>@MainBot — Multi-Node Monitor</b>\n\n"
        "<b>عمومی:</b>\n"
        "/status    —  Main + خلاصه همه نودها\n"
        "/main      —  Main VPS\n"
        "/nodes     —  خلاصه همه نودها\n"
        "/vpn       —  آمار VPN\n"
        "/users     —  کاربران کامل\n"
        "/peak      —  ساعات پیک همه نودها\n"
        "/logs      —  لاگ‌های خطا\n"
        "/report    —  گزارش کامل\n\n"
        f"<b>نودها:</b>\n{node_cmds}\n\n"
        f"<b>پیک per-node:</b>\n{peak_cmds}\n\n"
        "📌 گزارش خودکار: هر ۱ ساعت\n"
        "📅 گزارش روزانه: ۲۳:۵۹\n"
        "🔔 هر نود یک @NodeBot مستقل دارد"
    )


# ── Main ─────────────────────────────────────────────────────

def main() -> None:
    log.info(f"🚀 Starting — {len(NODES)} nodes")
    node_names = ", ".join(n["name"] for n in NODES)
    tg(f"🚀 <b>@MainBot شروع به کار کرد</b>\nنودها: {node_names}\n/help برای راهنما")

    schedule.every(INTERVALS["poll_nodes"]).seconds.do(job_poll_nodes)
    schedule.every(INTERVALS["poll_local"]).seconds.do(job_poll_local)
    schedule.every(INTERVALS["fetch_users"]).seconds.do(job_fetch_users)
    schedule.every(INTERVALS["log_check"]).seconds.do(job_check_logs)
    schedule.every(INTERVALS["periodic_report"]).seconds.do(job_periodic_report)
    schedule.every().day.at("23:59").do(job_daily_report)

    threading.Thread(target=job_fetch_users, daemon=True).start()
    threading.Thread(target=job_periodic_report, daemon=True).start()

    while True:
        try:
            schedule.run_pending(); poll_commands()
        except KeyboardInterrupt:
            tg("⛔ @MainBot متوقف شد."); break
        except Exception as e:
            log.error(f"Main: {e}")
        time.sleep(5)


if __name__ == "__main__":
    main()
