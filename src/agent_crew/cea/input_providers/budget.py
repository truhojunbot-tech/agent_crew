"""J6 provider budget — Qouta's quota cache + the #308 cooldown, read-only (G16 as input, O9).

``budget_class`` per provider:

* cooldown active (``{provider: until_epoch}`` in the cooldown file) ⇒ EXHAUSTED;
* ``five_hour.utilization`` (or ``seven_day``, whichever is higher) ≥ 1.0 ⇒ EXHAUSTED,
  ≥ ``constrained_at`` ⇒ CONSTRAINED, else OK.

O9 fail direction when the observation is **stale or missing** (``fetched_at``
older than ``max_age_seconds``, unreadable, or an ``error`` body):

* ``paid`` / ``overage`` credit ⇒ EXHAUSTED (fail closed: unobserved spend is refused);
* ``plan`` credit ⇒ CONSTRAINED with the stale ``observed_at`` kept, so the work is
  admitted and the receipt says the budget was not current (fail open *with receipt*).

A missing cooldown file means "no cooldown recorded"; an unreadable one is
treated like a stale observation.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from agent_crew.cea.receipt import BudgetClass, ProviderBudget

DEFAULT_QUOTA_DIR = "/home/truhojun/alfred/quota"
PLAN, PAID, OVERAGE = "plan", "paid", "overage"


class QuotaBudgetProvider:
    def __init__(self, quota_dir: Optional[str] = None, *, cooldown_file: Optional[str] = None,
                 credit_class: Optional[dict] = None, max_age_seconds: float = 900.0,
                 constrained_at: float = 0.9, clock=time.time, env: Optional[dict] = None):
        e = os.environ if env is None else env
        self.quota_dir = quota_dir or (e.get("AGENT_CREW_CEA_QUOTA_CACHE_DIR") or "").strip() \
            or DEFAULT_QUOTA_DIR
        self.cooldown_file = cooldown_file or (e.get("AGENT_CREW_CEA_COOLDOWN_FILE") or "").strip() or None
        self.credit_class = dict(credit_class or {})
        self.max_age_seconds = max_age_seconds
        self.constrained_at = constrained_at
        self._clock = clock

    def _stale(self, provider: str, observed_at: Optional[float]) -> ProviderBudget:
        cls = self.credit_class.get(provider, PAID)     # unknown credit class = the costly one
        state = BudgetClass.CONSTRAINED if cls == PLAN else BudgetClass.EXHAUSTED
        return ProviderBudget(provider=provider, state=state, observed_at=observed_at)

    def _cooldown_until(self, provider: str) -> Optional[float]:
        if not self.cooldown_file:
            return None
        if not os.path.exists(self.cooldown_file):
            return None
        with open(self.cooldown_file, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        v = (doc or {}).get(provider)
        if isinstance(v, dict):
            v = v.get("until")
        return float(v) if isinstance(v, (int, float)) else None

    def budget(self, provider: str) -> ProviderBudget:
        now = self._clock()
        try:
            until = self._cooldown_until(provider)
        except (OSError, ValueError, TypeError):
            return self._stale(provider, None)
        path = os.path.join(self.quota_dir, f"{provider}_monitor", "quota_cache.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cache = json.load(fh)
            observed = float(cache["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return self._stale(provider, None)
        if until is not None and until > now:
            return ProviderBudget(provider=provider, state=BudgetClass.EXHAUSTED, observed_at=observed)
        if cache.get("error") or now - observed > self.max_age_seconds:
            return self._stale(provider, observed)
        util = 0.0
        for window in ("five_hour", "seven_day"):
            w = cache.get(window) or {}
            if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
                util = max(util, float(w["utilization"]))
        if util >= 1.0:
            state = BudgetClass.EXHAUSTED
        elif util >= self.constrained_at:
            state = BudgetClass.CONSTRAINED
        else:
            state = BudgetClass.OK
        return ProviderBudget(provider=provider, state=state, observed_at=observed)
