"""What a tenant's communications may cost: rates, a hard monthly limit, and alerts.

The rates are the tenant's own estimates, per provider: vendors change prices and discount
by contract, so NOVA ships none and never guesses one. A provider without a rate costs
nothing in the estimate, and the usage report says so rather than hiding it.

The limit is hard: once the month's spend, plus what calls already on the line may still
cost, reaches it, the gate refuses new calls and follow-up emails wait. Calls in progress
are never cut off by it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from nova._fields import Doc
from nova.errors import SpecError

_RATE_KEYS = ("per_minute", "per_email")


@dataclass(frozen=True)
class Rate:
    per_minute: float = 0.0
    per_email: float = 0.0


@dataclass(frozen=True)
class BudgetSpec:
    currency: str = "USD"
    #: 0 means no limit: usage is still measured and reported.
    monthly_limit: float = 0.0
    #: Percentages of the limit at which an alert is raised, once per month each.
    alert_at: tuple[int, ...] = (80, 100)
    #: An agent alerts go to as work, besides the audit log. Optional.
    alert_assignee: str = ""
    rates: Mapping[str, Rate] = field(default_factory=dict)

    @property
    def declared(self) -> bool:
        return bool(self.rates or self.monthly_limit)

    def rate(self, provider: str) -> Rate:
        return self.rates.get(provider, Rate())

    def to_dict(self) -> dict[str, Any]:
        if not self.declared:
            return {}
        return {"currency": self.currency, "monthly_limit": self.monthly_limit,
                "alert_at": list(self.alert_at), "alert_assignee": self.alert_assignee,
                "rates": {k: {"per_minute": v.per_minute, "per_email": v.per_email}
                          for k, v in self.rates.items()}}


def _number(value: Any, where: str, source) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise SpecError("must be a number, 0 or more", field=where, source=source)
    return float(value)


def parse_budget(doc: Optional[Doc], *, providers: set[str]) -> BudgetSpec:
    if doc is None:
        return BudgetSpec()
    raw_rates = doc._raw("rates") or {}
    if not isinstance(raw_rates, Mapping):
        raise SpecError("must map a provider to its rates", field="communications.budget.rates",
                        source=doc.source)
    rates: dict[str, Rate] = {}
    for provider, value in raw_rates.items():
        where = f"communications.budget.rates.{provider}"
        if provider not in providers:
            raise SpecError(f"{provider!r} is not one of this tenant's providers", field=where,
                            source=doc.source)
        if not isinstance(value, Mapping) or set(value) - set(_RATE_KEYS):
            raise SpecError(f"may set only {', '.join(_RATE_KEYS)}", field=where,
                            source=doc.source)
        rates[str(provider)] = Rate(**{k: _number(v, f"{where}.{k}", doc.source)
                                       for k, v in value.items()})
    raw_alerts = doc._raw("alert_at")
    alerts = (80, 100) if raw_alerts is None else raw_alerts
    if not isinstance(alerts, (list, tuple)) or not all(
            isinstance(a, int) and not isinstance(a, bool) and 1 <= a <= 100 for a in alerts):
        raise SpecError("must be a list of percentages, 1 to 100",
                        field="communications.budget.alert_at", source=doc.source)
    limit = doc._raw("monthly_limit")
    spec = BudgetSpec(
        currency=doc.str_("currency", default="USD"),
        monthly_limit=_number(limit, "communications.budget.monthly_limit", doc.source)
        if limit is not None else 0.0,
        alert_at=tuple(sorted(set(alerts))),
        alert_assignee=doc.str_("alert_assignee"),
        rates=rates,
    )
    doc.reject_unknown()
    return spec
