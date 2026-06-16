#!/usr/bin/env python3
# ============================================================
#  tracker.py  —  NetworkTracker + MarzbanTracker + PeakHoursTracker
# ============================================================
import json, time, logging
from collections import deque
from datetime import datetime, timedelta
from threading import Lock
from typing import Dict, List, Optional

log = logging.getLogger(__name__)
TEHRAN_OFFSET = timedelta(hours=3, minutes=30)

def tehran_now() -> datetime:
    return datetime.utcnow() + TEHRAN_OFFSET


# ── Network Rate Tracker ─────────────────────────────────────

class NetworkTracker:
    WINDOW_SEC = 600

    def __init__(self):
        self._lock = Lock()
        self._samples: Dict[str, deque] = {}

    def add_sample(self, key: str, rx_gb: float, tx_gb: float) -> None:
        with self._lock:
            buf = self._samples.setdefault(key, deque())
            now = time.time()
            if buf and rx_gb < buf[-1][1] * 0.5:
                buf.clear()
            buf.append((now, rx_gb, tx_gb))
            cutoff = now - self.WINDOW_SEC
            while buf and buf[0][0] < cutoff:
                buf.popleft()

    def get_rate(self, key: str) -> dict:
        with self._lock:
            samples = list(self._samples.get(key, []))
        if len(samples) < 2:
            return {"rx_mb_per_min": 0.0, "tx_mb_per_min": 0.0,
                    "window_min": 0.0, "samples": len(samples)}
        ts0, rx0, tx0 = samples[0]
        ts1, rx1, tx1 = samples[-1]
        elapsed = (ts1 - ts0) / 60.0
        if elapsed < 0.05:
            return {"rx_mb_per_min": 0.0, "tx_mb_per_min": 0.0,
                    "window_min": 0.0, "samples": len(samples)}
        return {
            "rx_mb_per_min": round(max(0.0, (rx1-rx0)*1024) / elapsed, 2),
            "tx_mb_per_min": round(max(0.0, (tx1-tx0)*1024) / elapsed, 2),
            "window_min":    round(elapsed, 1),
            "samples":       len(samples),
        }


# ── Peak Hours Tracker ───────────────────────────────────────

class PeakHoursTracker:
    """
    میانگین ترافیک (RX+TX MB/min) به تفکیک ساعت روز — توقیت تهران.
    روی دیسک ذخیره می‌شود تا بعد از ریستارت باقی بماند.
    """

    def __init__(self, file: str = "peak_hours.json"):
        self._lock = Lock()
        self._file = file
        self._data: Dict[str, Dict[str, dict]] = self._load()

    def _load(self) -> dict:
        try:
            with open(self._file) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            with open(self._file, "w") as f:
                json.dump(self._data, f)
        except Exception as e:
            log.warning(f"PeakHours save: {e}")

    def _init(self, key: str) -> None:
        if key not in self._data:
            self._data[key] = {
                str(h): {"sum_mb": 0.0, "count": 0} for h in range(24)
            }

    def add_sample(self, key: str, rx_mb_per_min: float, tx_mb_per_min: float) -> None:
        total = rx_mb_per_min + tx_mb_per_min
        if total <= 0:
            return
        hour = str(tehran_now().hour)
        with self._lock:
            self._init(key)
            self._data[key][hour]["sum_mb"] += total
            self._data[key][hour]["count"]  += 1
            self._save()

    def has_data(self, key: str) -> bool:
        with self._lock:
            return any(v.get("count", 0) > 0
                       for v in self._data.get(key, {}).values())

    def all_hours(self, key: str) -> List[dict]:
        with self._lock:
            slots = dict(self._data.get(key, {}))
        result = []
        for h in range(24):
            s = slots.get(str(h), {"sum_mb": 0.0, "count": 0})
            avg = s["sum_mb"] / s["count"] if s["count"] > 0 else 0.0
            result.append({"hour": h, "avg_mb": round(avg, 2), "samples": s["count"]})
        return result

    def top_peaks(self, key: str, n: int = 3) -> List[dict]:
        return sorted(self.all_hours(key), key=lambda x: x["avg_mb"], reverse=True)[:n]

    def node_keys(self) -> List[str]:
        with self._lock:
            return list(self._data.keys())

    def fmt(self, node_name: str, key: str) -> str:
        if not self.has_data(key):
            return (f"<b>📊 پیک مصرف — {node_name}</b>\n"
                    "⏳ داده کافی نیست — حداقل ۱ ساعت صبر کنید.")

        hours  = self.all_hours(key)
        top3   = self.top_peaks(key, 3)
        max_mb = max(h["avg_mb"] for h in hours) or 1.0
        medals = ["🥇", "🥈", "🥉"]
        now_h  = tehran_now().hour
        total_samples = sum(h["samples"] for h in hours)

        lines = [
            f"<b>📊 ساعات پیک — {node_name}</b>",
            "<i>ترافیک RX+TX  |  ساعت تهران</i>", ""
        ]
        for i, p in enumerate(top3):
            marker = "  ← الان" if p["hour"] == now_h else ""
            lines.append(
                f"  {medals[i]} {p['hour']:02d}:00  —  {p['avg_mb']:.1f} MB/min{marker}"
            )

        lines += ["", "<b>نمودار ۲۴ ساعته:</b>", "<code>"]
        for h_data in hours:
            bar_len  = int(h_data["avg_mb"] / max_mb * 16)
            bar      = "█" * bar_len + "░" * (16 - bar_len)
            in_top3  = any(p["hour"] == h_data["hour"] for p in top3)
            peak_mrk = "◄" if in_top3 else " "
            lines.append(
                f"{h_data['hour']:02d}  {bar}  {h_data['avg_mb']:5.1f}{peak_mrk}"
            )
        lines += ["</code>", f"<i>بر اساس {total_samples:,} نمونه</i>"]
        return "\n".join(lines)


# ── Marzban User Tracker ─────────────────────────────────────

class MarzbanTracker:

    def __init__(self):
        self._lock       = Lock()
        self._users: List[dict] = []
        self._updated_at: float = 0.0

    def update(self, users: List[dict]) -> None:
        with self._lock:
            self._users      = users
            self._updated_at = time.time()

    @property
    def is_fresh(self) -> bool:
        return bool(self._users) and (time.time() - self._updated_at) < 600

    def _snap(self) -> List[dict]:
        with self._lock:
            return list(self._users)

    def summary(self) -> dict:
        u = self._snap(); now = time.time()
        return {
            "total":        len(u),
            "online_now":   sum(1 for x in u if self._ol(x, now, 180)),
            "active_10min": sum(1 for x in u if self._ol(x, now, 600)),
            "expired":      sum(1 for x in u if x.get("expire") and x["expire"] < now),
            "updated_at":   self._updated_at,
        }

    def new_users_count(self) -> dict:
        now = time.time(); c = {"day": 0, "week": 0, "month": 0}
        for u in self._snap():
            ts = self._ts(u.get("created_at"))
            if not ts: continue
            age = now - ts
            if age < 86_400:    c["day"]   += 1
            if age < 604_800:   c["week"]  += 1
            if age < 2_592_000: c["month"] += 1
        return c

    def top_users(self, n: int = 5) -> List[dict]:
        now = time.time()
        us  = sorted(self._snap(), key=lambda u: u.get("used_traffic") or 0, reverse=True)
        out = []
        for u in us[:n]:
            used  = u.get("used_traffic") or 0
            limit = u.get("data_limit")   or 0
            out.append({
                "username": u.get("username", "?"),
                "used_gb":  round(used  / 1_073_741_824, 2),
                "limit_gb": round(limit / 1_073_741_824, 2) if limit else None,
                "percent":  round(used / limit * 100, 1)    if limit else None,
                "online":   self._ol(u, now, 180),
            })
        return out

    def expiring_soon(self) -> dict:
        now = time.time(); in_24h, in_7d = [], []
        for u in self._snap():
            exp = u.get("expire")
            if not exp or exp <= now: continue
            name = u.get("username", "?")
            if   exp < now + 86_400:  in_24h.append({"username": name, "remaining": f"{int((exp-now)/3600)}h"})
            elif exp < now + 604_800: in_7d.append( {"username": name, "remaining": f"{int((exp-now)/86400)}d"})
        return {"in_24h": in_24h, "in_7d": in_7d}

    def sold_traffic(self) -> dict:
        now = time.time(); t = {"all": 0, "month": 0, "week": 0, "day": 0}
        for u in self._snap():
            lim = u.get("data_limit") or 0
            if not lim: continue
            t["all"] += lim
            ts = self._ts(u.get("created_at"))
            if ts:
                age = now - ts
                if age < 86_400:    t["day"]   += lim
                if age < 604_800:   t["week"]  += lim
                if age < 2_592_000: t["month"] += lim
        return {k: round(v / 1_073_741_824, 2) for k, v in t.items()}

    @staticmethod
    def _ol(u: dict, now: float, threshold: int) -> bool:
        oa = u.get("online_at")
        return bool(oa and (now - oa) < threshold)

    @staticmethod
    def _ts(value) -> Optional[float]:
        if not value: return None
        if isinstance(value, (int, float)): return float(value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except Exception: pass
        return None
