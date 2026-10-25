"""Monthly spend accounting and the decisions spend-check makes from it."""
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .model import Friend
from .policy import FALLBACK_USD_PER_MTOK, RUNAWAY_USD, RUNAWAY_WINDOW_S, WARN_FRACTION

LONDON = ZoneInfo("Europe/London")
SAMPLE_KEEP_S = 7200
STATE_FILE = "friend-spend.json"


def month_key(now: datetime) -> str:
    return now.astimezone(LONDON).strftime("%Y-%m")


def month_start(now: datetime) -> datetime:
    return now.astimezone(LONDON).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass
class MonthSpend:
    priced_usd: float
    unpriced: list[str]
    unpriced_usd: float

    @property
    def total_usd(self) -> float:
        return self.priced_usd + self.unpriced_usd


def month_spend(db: Path, start: datetime) -> MonthSpend:
    db = Path(db)
    if not db.exists():
        return MonthSpend(0.0, [], 0.0)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT id, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
            "estimated_cost_usd FROM sessions WHERE started_at >= ?",
            (start.timestamp(),),
        ).fetchall()
    finally:
        con.close()
    priced = 0.0
    unpriced: list[str] = []
    unpriced_usd = 0.0
    for sid, i, o, cr, cw, cost in rows:
        tokens = (i or 0) + (o or 0) + (cr or 0) + (cw or 0)
        if cost is None or cost <= 0:
            if tokens > 0:
                unpriced.append(str(sid))
                unpriced_usd += tokens / 1e6 * FALLBACK_USD_PER_MTOK
        else:
            priced += cost
    return MonthSpend(priced, unpriced, unpriced_usd)


@dataclass
class SpendState:
    month: str
    warned: bool = False
    stopped_by_cap: bool = False
    stopped_by_runaway: bool = False
    alerted_unpriced: list[str] = field(default_factory=list)
    samples: list[tuple[float, float]] = field(default_factory=list)

    @classmethod
    def load(cls, profile: Path, now: datetime) -> "SpendState":
        path = Path(profile) / STATE_FILE
        if not path.is_file():
            return cls(month=month_key(now))
        d = json.loads(path.read_text())
        return cls(month=d["month"], warned=d["warned"], stopped_by_cap=d["stopped_by_cap"],
                   stopped_by_runaway=d["stopped_by_runaway"],
                   alerted_unpriced=list(d["alerted_unpriced"]),
                   samples=[(float(t), float(v)) for t, v in d["samples"]])

    def to_json(self) -> str:
        return json.dumps({"month": self.month, "warned": self.warned,
                           "stopped_by_cap": self.stopped_by_cap,
                           "stopped_by_runaway": self.stopped_by_runaway,
                           "alerted_unpriced": self.alerted_unpriced,
                           "samples": [list(s) for s in self.samples]}, indent=2) + "\n"

    def save(self, profile: Path, system) -> None:
        system.write_file(Path(profile) / STATE_FILE, self.to_json(), 0o600)

    def reset_for_month(self, month: str) -> "SpendState":
        """Fresh state for a new month; the runaway hold is kept."""
        return SpendState(month=month, stopped_by_runaway=self.stopped_by_runaway)


def decide(f: Friend, spend: MonthSpend, st: SpendState, now: datetime) -> list[str]:
    actions: list[str] = []
    if st.month != month_key(now):
        actions.append("new_month")
        st = st.reset_for_month(month_key(now))  # evaluate the rest against the fresh month
    total = spend.total_usd
    if total >= WARN_FRACTION * f.cap_usd and not st.warned:
        actions.append("warn")
    if total >= f.cap_usd and not st.stopped_by_cap:
        actions.append("stop_cap")
    if not st.stopped_by_runaway:
        cutoff = now.timestamp() - RUNAWAY_WINDOW_S
        old = [s for s in st.samples if s[0] <= cutoff]
        if old:
            base = max(old, key=lambda s: s[0])[1]
            if total - base > RUNAWAY_USD:
                actions.append("stop_runaway")
    if any(i not in st.alerted_unpriced for i in spend.unpriced):
        actions.append("alert_unpriced")
    return actions
