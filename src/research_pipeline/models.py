"""Public run specification and neutral source identities."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Instrument:
    market: str
    code: str
    issuer_id: str = ""

    @classmethod
    def parse(cls, value: str) -> Instrument:
        market, separator, code = value.partition(":")
        if not separator or market not in {"a_share", "hk", "us"}:
            raise ValueError(
                "company_requires_explicit_market: a_share:600348, hk:00883, us:XOM"
            )
        code = code.strip().upper()
        pattern = {
            "a_share": r"\d{6}",
            "hk": r"\d{1,5}",
            "us": r"[A-Z][A-Z0-9.\-]{0,14}",
        }[market]
        if not re.fullmatch(pattern, code):
            raise ValueError("invalid_instrument_code")
        if market == "hk":
            code = code.zfill(5)
        return cls(market, code)

    @property
    def key(self) -> str:
        return f"{self.market}:{self.code}"

    @property
    def issuer(self) -> str:
        return self.issuer_id or self.key

    @property
    def safe_id(self) -> str:
        return self.market + "_" + self.code.replace(".", "_").replace("-", "_")


@dataclass
class AnnualDocument:
    instrument: str
    issuer_id: str
    fiscal_year: int
    document_id: str
    url: str = ""
    published_at: str = ""
    period_start: str | None = None
    period_end: str | None = None
    kind: str = "annual"
    content_type: str = "application/pdf"
    local_path: str = ""
    sha256: str = ""
    profiles: list[str] = field(default_factory=list)
    currency: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ResearchSpec:
    companies: tuple[str, ...]
    output: Path
    budget_limit_usd: Decimal = Decimal(0)
    paid: bool = False
    years: int = 7
    start_year: int | None = None
    end_year: int | None = None
    focus: str = ""
    assumptions: Path | None = None
    market_snapshot: Path | None = None
    source_manifest: Path | None = None
    cache_roots: tuple[Path, ...] = ()
    store_root: Path | None = None
    repo: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])
    concurrency: int = 20
    repair_fraction: Decimal = Decimal("0.1")
    profiles: dict[str, list[str]] = field(default_factory=dict)
    issuer_map: dict[str, str] = field(default_factory=dict)
    origin: dict[str, str] = field(default_factory=dict)
    config: Path | None = None
    credential_root: Path | None = None
    authority_root: Path | None = None
    authority_isolated: bool = False

    def __post_init__(self) -> None:
        self.repo = Path(self.repo).resolve()
        self.credential_root = Path(self.credential_root or self.repo).resolve()
        self.output = Path(self.output).resolve()
        default_store = self.repo / "data/fundamental_data/shared_research"
        custom_store = (
            self.store_root is not None
            and Path(self.store_root).resolve() != default_store.resolve()
        )
        self.store_root = Path(self.store_root or default_store).resolve()
        if self.authority_root is None:
            from src.platform_composition import authority_for

            if custom_store and not os.getenv("TRADE_EYES_AUTHORITY_ROOT"):
                self.authority_root = self.store_root / "platform_authority"
                self.authority_isolated = True
            else:
                self.authority_root = authority_for(self.credential_root).root
        self.authority_root = Path(self.authority_root).resolve()
        self.cache_roots = tuple(Path(p).resolve() for p in self.cache_roots)
        self.budget_limit_usd = Decimal(str(self.budget_limit_usd))
        self.repair_fraction = Decimal(str(self.repair_fraction))
        self.companies = tuple(
            dict.fromkeys(Instrument.parse(x).key for x in self.companies)
        )
        if (
            not self.companies
            or not self.budget_limit_usd.is_finite()
            or self.budget_limit_usd < 0
        ):
            raise ValueError("nonempty_companies_and_nonnegative_budget_required")
        if not 1 <= self.years <= 100 or not 1 <= self.concurrency <= 20:
            raise ValueError("invalid_window_or_concurrency")
        if not self.repair_fraction.is_finite() or not 0 <= self.repair_fraction <= 1:
            raise ValueError("invalid_repair_fraction")
        if (self.start_year is None) != (self.end_year is None):
            raise ValueError("start_and_end_year_required_together")
        if (
            self.start_year is not None
            and not 1900 <= self.start_year <= self.end_year <= 2200
        ):
            raise ValueError("invalid_year_range")
        if self.output == self.store_root or self.store_root.is_relative_to(
            self.output
        ):
            raise ValueError("run_output_must_not_contain_shared_store")

    def to_dict(self) -> dict:
        value = asdict(self)
        for key in (
            "repo",
            "output",
            "store_root",
            "assumptions",
            "market_snapshot",
            "source_manifest",
            "config",
            "credential_root",
            "authority_root",
        ):
            value[key] = str(value[key]) if value[key] is not None else None
        value["cache_roots"] = [str(p) for p in self.cache_roots]
        value["budget_limit_usd"] = str(self.budget_limit_usd)
        value["repair_fraction"] = str(self.repair_fraction)
        value["companies"] = list(self.companies)
        return value

    @classmethod
    def from_dict(cls, value: dict) -> ResearchSpec:
        return cls(**value)
