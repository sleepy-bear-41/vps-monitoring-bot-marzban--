# 🖥️ VPS Monitor — Multi-Node

```
Main VPS (Marzban + @MainBot)
    ▲        ▲        ▲
  poll     poll     poll
  Node1    Node2    NodeN
(@Bot1)  (@Bot2)  (@BotN)
```

## فایل‌ها
```
main/
├── monitor.py                ← @MainBot — همه دستورات
├── tracker.py                ← NetworkTracker + PeakHours + Marzban
├── monitor_config.example.py
├── requirements.txt
└── vps-monitor.service

node/                         ← روی هر نود کپی کن
├── agent.py                  ← FastAPI metrics API
├── node_monitor.py           ← @NodeBot — هشدار مستقل
├── node_config.example.py
├── requirements.txt
├── vps-agent.service
└── vps-node-monitor.service

nginx_node.conf               ← روی هر نود
```

## نصب Main VPS
```bash
mkdir /opt/vps-monitor && cd /opt/vps-monitor
pip install -r requirements.txt
cp monitor_config.example.py monitor_config.py && nano monitor_config.py

cp vps-monitor.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now vps-monitor
```

## نصب هر نود
```bash
mkdir /opt/vps-node && cd /opt/vps-node
pip install -r requirements.txt
cp node_config.example.py node_config.py && nano node_config.py

# SSL
certbot --nginx -d YOUR_NODE_DOMAIN
sed -i 's/YOUR_NODE_DOMAIN/yourdomain.com/g' nginx_node.conf
cp nginx_node.conf /etc/nginx/sites-available/agent
ln -s /etc/nginx/sites-available/agent /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# Services
cp vps-agent.service vps-node-monitor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vps-agent vps-node-monitor
```

## API Key
```bash
openssl rand -hex 32   # برای هر نود می‌توانی جداگانه بسازی
```

## دستورات @MainBot
| دستور | توضیح |
|-------|-------|
| /status | Main + خلاصه همه نودها |
| /main | Main VPS |
| /nodes | خلاصه همه نودها |
| /node1 | نود ۱ (جایگزین با key نود) |
| /vpn | آمار VPN |
| /users | کاربران کامل + sold traffic |
| /peak | ساعات پیک همه نودها |
| /peak_node1 | پیک نود خاص |
| /logs | لاگ‌های خطا |
| /report | گزارش کامل |

## نود جدید
فقط فایل‌های `node/` را کپی کن، `node_config.py` را پر کن،
و در `monitor_config.py` روی Main یک بلوک به `NODES` اضافه کن.
