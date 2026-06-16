#!/usr/bin/env python3
# ============================================================
#  agent.py  —  Node VPS Metrics Agent
#  localhost:8765 | پشت Nginx
# ============================================================
import os, re, subprocess, time, logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import psutil
from fastapi import FastAPI, HTTPException, Header, Request

from node_config import (
    API_KEY, MAIN_VPS_IP, CRITICAL_SERVICES,
    WIREGUARD_INTERFACE, LOG_LINES, ALLOWED_IPS,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("agent.log"), logging.StreamHandler()])
log = logging.getLogger(__name__)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def auth(key: str, request: Request):
    if key != API_KEY:
        log.warning(f"Unauthorized: {request.client.host}")
        raise HTTPException(403, "Forbidden")
    if ALLOWED_IPS and request.client.host not in ALLOWED_IPS:
        raise HTTPException(403, "IP not allowed")


def collect_system() -> dict:
    cpu = psutil.cpu_percent(interval=1)
    load = psutil.getloadavg()
    mem = psutil.virtual_memory()
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
        "load_1":           round(load[0],2), "load_5": round(load[1],2), "load_15": round(load[2],2),
        "ram_total_mb":     mem.total//1048576, "ram_used_mb": mem.used//1048576,
        "ram_free_mb":      mem.available//1048576, "ram_percent": mem.percent,
        "swap_total_mb":    swap.total//1048576, "swap_used_mb": swap.used//1048576,
        "swap_percent":     round(swap.percent,1),
        "disk_total_gb":    round(disk.total/1073741824,1), "disk_used_gb": round(disk.used/1073741824,1),
        "disk_free_gb":     round(disk.free/1073741824,1),  "disk_percent": disk.percent,
        "inode_percent":    inode_pct,
        "disk_io_read_mb":  round(dio.read_bytes/1048576,1)  if dio else 0,
        "disk_io_write_mb": round(dio.write_bytes/1048576,1) if dio else 0,
        "cpu_temp":         cpu_temp,
        "uptime":           f"{d}d {h}h {m}m",
        "hostname":         os.uname().nodename,
    }


def collect_network() -> dict:
    conns = psutil.net_connections(kind="tcp")
    nio   = psutil.net_io_counters()
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
        "tcp_established":  sum(1 for c in conns if c.status=="ESTABLISHED"),
        "tcp_time_wait":    sum(1 for c in conns if c.status=="TIME_WAIT"),
        "tcp_close_wait":   sum(1 for c in conns if c.status=="CLOSE_WAIT"),
        "tcp_total":        sum(1 for c in conns if c.status in ("ESTABLISHED","TIME_WAIT","CLOSE_WAIT")),
        "net_rx_gb":        round(nio.bytes_recv/1073741824, 4),
        "net_tx_gb":        round(nio.bytes_sent/1073741824, 4),
        "ping_to_main_ms":  ping_ms,
        "packet_loss_pct":  packet_loss,
        "jitter_ms":        jitter,
    }


def collect_services() -> dict:
    r = {}
    for svc in CRITICAL_SERVICES:
        try:
            res = subprocess.run(["systemctl","is-active",svc],
                                 capture_output=True, text=True, timeout=5)
            r[svc] = res.stdout.strip() == "active"
        except Exception: r[svc] = False
    return r


def collect_wireguard() -> dict:
    if not WIREGUARD_INTERFACE: return {"configured": False}
    try:
        raw = subprocess.check_output(
            f"wg show {WIREGUARD_INTERFACE} 2>/dev/null",
            shell=True, timeout=5).decode().strip()
        if not raw: return {"configured": True, "status": "down"}
        hs = re.search(r"latest handshake: (.+)", raw)
        tr = re.search(r"transfer: (.+)", raw)
        return {"configured":True,"status":"up",
                "handshake": hs.group(1).strip() if hs else "",
                "transfer":  tr.group(1).strip() if tr else ""}
    except Exception: return {"configured":True,"status":"error"}


def collect_logs() -> dict:
    cmds = {
        "xray":   f"journalctl -u xray -n {LOG_LINES} --no-pager 2>/dev/null | grep -iE 'error|warn|fail' | tail -10",
        "nginx":  f"tail -n {LOG_LINES} /var/log/nginx/error.log 2>/dev/null | tail -10",
        "docker": f"journalctl -u docker -n {LOG_LINES} --no-pager 2>/dev/null | grep -iE 'error|fail' | tail -5",
    }
    logs = {}
    for svc, cmd in cmds.items():
        try:
            out = subprocess.check_output(cmd, shell=True, timeout=6).decode(errors="replace").strip()
            logs[svc] = out[:600] if out else ""
        except Exception: logs[svc] = ""
    return logs


@app.get("/health")
async def health(request: Request, x_api_key: str = Header(...)):
    auth(x_api_key, request)
    return {"status":"ok","timestamp":datetime.utcnow().isoformat(),"hostname":os.uname().nodename}


@app.get("/metrics")
async def metrics(request: Request, x_api_key: str = Header(...)):
    auth(x_api_key, request)
    start = time.time()
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_sys = ex.submit(collect_system)
        f_net = ex.submit(collect_network)
        f_svc = ex.submit(collect_services)
        f_wg  = ex.submit(collect_wireguard)
        f_log = ex.submit(collect_logs)
        system=f_sys.result(); network=f_net.result()
        services=f_svc.result(); wireguard=f_wg.result(); logs=f_log.result()
    return {
        "timestamp":        datetime.utcnow().isoformat(),
        "collect_time_sec": round(time.time()-start, 2),
        "system":           system, "network": network,
        "services":         services, "wireguard": wireguard, "logs": logs,
    }


if __name__ == "__main__":
    import uvicorn
    from node_config import AGENT_HOST, AGENT_PORT
    uvicorn.run(app, host=AGENT_HOST, port=AGENT_PORT, log_level="warning")
