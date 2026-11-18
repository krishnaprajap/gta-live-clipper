"""Spend ledger with hard monthly caps. Caps are read-only at runtime: nothing in
this codebase raises them; exceeding a cap simply disables the paid feature."""
from __future__ import annotations

import datetime as dt
import time

from .db import DB


def month_start(now: float | None = None) -> float:
    d = dt.datetime.fromtimestamp(now or time.time())
    return dt.datetime(d.year, d.month, 1).timestamp()


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, db: DB, ai_cap_inr: float, total_cap_inr: float):
        self.db = db
        self.ai_cap = float(ai_cap_inr)
        self.total_cap = float(total_cap_inr)

    def spent(self, category: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(amount_inr),0) AS s FROM spend WHERE created_at>=?"
        params: list = [month_start()]
        if category:
            sql += " AND category=?"
            params.append(category)
        return float(self.db.one(sql, params)["s"])

    def can_spend(self, category: str, amount: float) -> bool:
        if category == "ai" and self.spent("ai") + amount > self.ai_cap:
            return False
        return self.spent() + amount <= self.total_cap

    def charge(self, category: str, amount: float, note: str = "") -> None:
        """Reserve spend *before* making a paid call. Raises if over cap."""
        if not self.can_spend(category, amount):
            raise BudgetExceeded(f"{category} budget exhausted ({self.spent(category):.2f} INR spent)")
        self.db.execute("INSERT INTO spend(category,amount_inr,note,created_at) VALUES(?,?,?,?)",
                        (category, amount, note, time.time()))

    def summary(self) -> dict:
        return {"ai_spent_inr": round(self.spent("ai"), 2), "ai_cap_inr": self.ai_cap,
                "total_spent_inr": round(self.spent(), 2), "total_cap_inr": self.total_cap}
