# ============================================================
#  monitor_config.example.py  —  Main VPS
#  کپی کن به monitor_config.py و مقادیر واقعی را وارد کن
# ============================================================

# ─── نودها ───────────────────────────────────────────────────
# هر نود key یکتا دارد — در دستورات: /node1  /node2  /peak_node1
NODES = [
    {
        "key":         "node1",
        "name":        "🖥️ Node 1",
        "metrics_url": "https://node1.YOUR_DOMAIN/metrics",
        "api_key":     "NODE1_API_KEY",
        "timeout":     15,
    },
    {
        "key":         "node2",
        "name":        "🖥️ Node 2",
        "metrics_url": "https://node2.YOUR_DOMAIN/metrics",
        "api_key":     "NODE2_API_KEY",
        "timeout":     15,
    },
    # برای نود جدید یک بلوک کپی کن:
    # {
    #     "key":         "node3",
    #     "name":        "🖥️ Node 3",
    #     "metrics_url": "https://node3.YOUR_DOMAIN/metrics",
    #     "api_key":     "NODE3_API_KEY",
    #     "timeout":     15,
    # },
]

# ─── Marzban روی Main VPS ─────────────────────────────────────
MARZBAN = {
    "url":  "http://localhost:7777",
    "user": "admin",
    "pass": "YOUR_MARZBAN_PASSWORD",
}

# ─── ربات تلگرام اصلی (@MainBot) ─────────────────────────────
TELEGRAM = {
    "token":     "MAIN_BOT_TOKEN",
    "chat_id":   "YOUR_CHAT_ID",
    "admin_ids": [],
}

# ─── آستانه هشدارها ──────────────────────────────────────────
THRESHOLDS = {
    "cpu_percent":   85,
    "ram_percent":   85,
    "disk_percent":  85,
    "swap_percent":  70,
    "load_avg_1":    4.0,
    "packet_loss":   5,
    "ping_ms":       300,
    "jitter_ms":     50,     # ← هشدار jitter بالا
    "cpu_temp":      80,
    "inode_percent": 85,
}

# ─── فواصل زمانی (ثانیه) ─────────────────────────────────────
INTERVALS = {
    "poll_nodes":      60,
    "poll_local":      60,
    "fetch_users":     300,
    "log_check":       300,
    "periodic_report": 3600,
    "heartbeat":       180,
}

# ─── سرویس‌های حیاتی Main VPS ────────────────────────────────
LOCAL_SERVICES = ["docker", "marzban", "nginx", "xray"]

LOG_LINES = 50
