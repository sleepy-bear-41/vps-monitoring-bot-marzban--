#!/usr/bin/env python3
# ============================================================
#  node_monitor.py  —  @NodeBot  |  مستقل از Main VPS
#  Jitter + همه هشدارها | دستورات: /node /logs /help
# ============================================================
import re, subprocess, sys, time, threading, logging
from datetime import datetime
from typing import Dict
import psutil, requests, schedule
from node_config import (
    TELEGRAM_NODE, THRESHOLDS, INTERVALS,
    CRITICAL_SERVICES, WIREGUARD_INTERFACE, LOG_LINES, MAIN_VPS_IP,
)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("node_monitor.log"),logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_alerts: Dict[str, bool] = {}
_last_update_id = 0


def tg(text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_NODE['token']}/sendMessage",
            json={"chat_id":TELEGRAM_NODE["chat_id"],"text":text[:4096],
                  "parse_mode":"HTML","disable_web_page_preview":True},timeout=10)
        return r.ok
    except Exception as e:
        log.error(f"Telegram: {e}"); return False


def tg_alert(key: str, condition: bool, msg: str) -> None:
    prev = _alerts.get(key, False)
    if condition and not prev:
        tg(f"🚨 <b>ALERT — Node</b>\n{msg}"); _alerts[key] = True
    elif not condition and prev:
        tg(f"✅ <b>RESOLVED — Node</b>\n{msg}"); _alerts[key] = False


def _bar(pct: float, w: int = 10) -> str:
    f = int(pct/100*w)
    return "[" + "█"*f + "░"*(w-f) + "]"


def get_system() -> dict:
    cpu  = psutil.cpu_percent(interval=1)
    load = psutil.getloadavg()
    mem  = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    dio  = psutil.disk_io_counters()
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
    nio = psutil.net_io_counters()
    return {
        "cpu_percent":  cpu,
        "load_1": round(load[0],2), "load_5": round(load[1],2), "load_15": round(load[2],2),
        "ram_total_mb": mem.total//1048576, "ram_used_mb": mem.used//1048576, "ram_percent": mem.percent,
        "swap_total_mb":swap.total//1048576,"swap_used_mb": swap.used//1048576, "swap_percent": round(swap.percent,1),
        "disk_total_gb":round(disk.total/1073741824,1),"disk_used_gb":round(disk.used/1073741824,1),"disk_percent":disk.percent,
        "cpu_temp": cpu_temp, "uptime": f"{d}d {h}h {m}m",
        "net_rx_gb": round(nio.bytes_recv/1073741824,3), "net_tx_gb": round(nio.bytes_sent/1073741824,3),
        "disk_io_r_mb": round(dio.read_bytes/1048576,1) if dio else 0,
        "disk_io_w_mb": round(dio.write_bytes/1048576,1) if dio else 0,
    }


def get_network() -> dict:
    conns = psutil.net_connections(kind="tcp")
    ping_ms = packet_loss = jitter = -1.0
    try:
        raw = subprocess.check_output(
            f"ping -c 5 -W 2 {MAIN_VPS_IP} 2>&1 | tail -2",
            shell=True, timeout=18).decode()
        m = re.search(r"(\d+\.?\d+)/(\d+\.?\d+)/(\d+\.?\d+)/(\d+\.?\d+)", raw)
        if m: ping_ms = float(m.group(2)); jitter = float(m.group(4))
        m2 = re.search(r"(\d+)%\s+packet loss", raw)
        if m2: packet_loss = float(m2.group(1))
    except Exception: pass
    return {
        "tcp_established": sum(1 for c in conns if c.status=="ESTABLISHED"),
        "tcp_time_wait":   sum(1 for c in conns if c.status=="TIME_WAIT"),
        "tcp_close_wait":  sum(1 for c in conns if c.status=="CLOSE_WAIT"),
        "ping_to_main_ms": ping_ms, "packet_loss_pct": packet_loss, "jitter_ms": jitter,
    }


def get_services() -> dict:
    r = {}
    for svc in CRITICAL_SERVICES:
        try:
            res = subprocess.run(["systemctl","is-active",svc],
                                 capture_output=True, text=True, timeout=5)
            r[svc] = res.stdout.strip() == "active"
        except Exception: r[svc] = False
    return r


def get_wireguard() -> dict:
    if not WIREGUARD_INTERFACE: return {"configured": False}
    try:
        raw = subprocess.check_output(
            f"wg show {WIREGUARD_INTERFACE} 2>/dev/null",
            shell=True, timeout=5).decode().strip()
        if not raw: return {"configured":True,"status":"down"}
        hs = re.search(r"latest handshake: (.+)", raw)
        tr = re.search(r"transfer: (.+)", raw)
        return {"configured":True,"status":"up",
                "handshake":hs.group(1).strip() if hs else "",
                "transfer":tr.group(1).strip() if tr else ""}
    except Exception: return {"configured":True,"status":"error"}


def run_alerts(s: dict, n: dict, svc: dict) -> None:
    T = THRESHOLDS
    tg_alert("cpu",  s.get("cpu_percent",0) > T["cpu_percent"],  f"🔥 CPU: {s.get('cpu_percent',0):.1f}%")
    tg_alert("ram",  s.get("ram_percent",0) > T["ram_percent"],  f"💾 RAM: {s.get('ram_percent',0):.1f}%")
    tg_alert("disk", s.get("disk_percent",0) > T["disk_percent"],f"💿 Disk: {s.get('disk_percent',0):.1f}%")
    tg_alert("load", s.get("load_1",0) > T["load_avg_1"],        f"⚡ Load: {s.get('load_1',0):.2f}")
    p = n.get("ping_to_main_ms",-1)
    if p > 0: tg_alert("ping", p > T["ping_ms"], f"📡 Ping: {p:.1f}ms")
    loss = n.get("packet_loss_pct",-1)
    if loss >= 0: tg_alert("loss", loss > T["packet_loss"], f"📉 Loss: {loss:.0f}%")
    jit = n.get("jitter_ms",-1)
    if jit >= 0: tg_alert("jitter", jit > T["jitter_ms"], f"📊 Jitter: {jit:.1f}ms")
    temp = s.get("cpu_temp")
    if isinstance(temp,(int,float)): tg_alert("temp", temp > T["cpu_temp"], f"🌡️ Temp: {temp}°C")
    for name, up in svc.items():
        tg_alert(f"svc_{name}", not up, f"⚠️ سرویس <b>{name}</b> DOWN!")


def fmt_status(s: dict, n: dict, svc: dict, wg: dict) -> str:
    p    = n.get("ping_to_main_ms",-1)
    loss = n.get("packet_loss_pct",-1)
    jit  = n.get("jitter_ms",-1)
    now  = datetime.now().strftime("%H:%M  %Y-%m-%d")
    lines = [
        f"<b>{'─'*32}</b>",
        f"<b>🖥️ Node VPS</b>  •  {now}",
        f"⏱ <code>{s.get('uptime','?')}</code>",
        "",
        "<b>💻 سرور</b>",
        f"  CPU    {s.get('cpu_percent',0):.1f}%  {_bar(s.get('cpu_percent',0))}",
        f"  Load   {s.get('load_1',0):.2f} / {s.get('load_5',0):.2f} / {s.get('load_15',0):.2f}",
        f"  RAM    {s.get('ram_used_mb',0):,} / {s.get('ram_total_mb',0):,} MB  ({s.get('ram_percent',0):.0f}%)",
        f"  Swap   {s.get('swap_used_mb',0)} / {s.get('swap_total_mb',0)} MB  ({s.get('swap_percent',0):.0f}%)",
        f"  Disk   {s.get('disk_used_gb',0)} / {s.get('disk_total_gb',0)} GB  ({s.get('disk_percent',0):.0f}%)",
        f"  Temp   {s.get('cpu_temp','N/A')}{'°C' if isinstance(s.get('cpu_temp'),float) else ''}",
        f"  I/O    R:{s.get('disk_io_r_mb',0)} MB  W:{s.get('disk_io_w_mb',0)} MB",
        "",
        "<b>🌐 شبکه</b>",
        f"  Ping → Main:   {'N/A' if p<0 else f'{p:.1f} ms'}",
        f"  Jitter:        {'N/A' if jit<0 else f'{jit:.1f} ms'}",
        f"  Packet Loss:   {'N/A' if loss<0 else f'{loss:.0f}%'}",
        f"  ESTAB / TW / CW:  {n.get('tcp_established',0)} / {n.get('tcp_time_wait',0)} / {n.get('tcp_close_wait',0)}",
        f"  RX / TX:  {s.get('net_rx_gb',0):.2f} / {s.get('net_tx_gb',0):.2f} GB",
        "",
        "<b>⚙️ سرویس‌ها</b>",
    ]
    for name, up in svc.items():
        lines.append(f"  {'🟢' if up else '🔴'} {name}")
    if wg.get("configured"):
        lines += ["", f"<b>🔒 WireGuard:</b> {wg.get('status','?')}"]
        if wg.get("handshake"): lines.append(f"  {wg['handshake']}")
    return "\n".join(lines)


def job_monitor() -> None:
    s=get_system(); n=get_network(); svc=get_services()
    run_alerts(s, n, svc)


def job_report() -> None:
    tg(fmt_status(get_system(), get_network(), get_services(), get_wireguard()))


def poll_commands() -> None:
    global _last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_NODE['token']}/getUpdates",
            params={"offset":_last_update_id+1,"timeout":5}, timeout=10)
        for upd in r.json().get("result",[]):
            _last_update_id = upd["update_id"]
            text = upd.get("message",{}).get("text","").strip().lower()
            threading.Thread(target=dispatch, args=(text,), daemon=True).start()
    except Exception as e:
        log.warning(f"Poll: {e}")


def dispatch(cmd: str) -> None:
    if cmd in ("/node","/status","/وضعیت"): job_report()
    elif cmd in ("/logs","/لاگ"):
        cmds = {
            "xray":   f"journalctl -u xray -n {LOG_LINES} --no-pager 2>/dev/null | grep -iE 'error|warn|fail' | tail -10",
            "nginx":  f"tail -n {LOG_LINES} /var/log/nginx/error.log 2>/dev/null | tail -10",
            "docker": f"journalctl -u docker -n {LOG_LINES} --no-pager 2>/dev/null | grep -iE 'error|fail' | tail -5",
        }
        lines = ["<b>📋 لاگ‌ها — Node</b>",""]
        has = False
        for svc, c in cmds.items():
            try:
                out = subprocess.check_output(c,shell=True,timeout=6).decode(errors="replace").strip()
                if out: has=True; lines+=[f"<b>▸ {svc}:</b>",f"<code>{out[:300]}</code>",""]
            except Exception: pass
        if not has: lines.append("✅ خطایی یافت نشد")
        tg("\n".join(lines))
    elif cmd in ("/help","/start","/راهنما"):
        tg("🤖 <b>Node Monitor</b>\n\n/node — وضعیت کامل\n/logs — لاگ‌ها\n/help — راهنما\n\n🔔 هشدارهای خودکار مستقل از Main")


def main() -> None:
    log.info("🚀 Node Monitor starting...")
    tg("🚀 <b>Node Monitor شروع به کار کرد</b>")
    schedule.every(INTERVALS["monitor"]).seconds.do(job_monitor)
    schedule.every(INTERVALS["periodic_report"]).seconds.do(job_report)
    threading.Thread(target=job_report, daemon=True).start()
    while True:
        try:
            schedule.run_pending(); poll_commands()
        except KeyboardInterrupt: tg("⛔ متوقف شد."); break
        except Exception as e: log.error(f"Main: {e}")
        time.sleep(5)

if __name__ == "__main__": main()
