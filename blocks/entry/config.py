"""Configuration for credit spread entry scans and opens."""
from __future__ import annotations

from dataclasses import dataclass, fields

import meic0dte.app.config as meic_config

_VALID_QUOTE_SOURCES = frozenset({'api', 'mqtt'})


@dataclass
class CreditEntryConfig:
    spread_width_min: int = 25
    spread_width_max: int = 35
    credit_min: float = 0.90
    credit_max_put: float = 1.85
    credit_max_call: float = 1.85
    otm_min: int = 5
    otm_max: int = 150
    quantity: int = 1
    quote_source: str = 'api'
    min_market_credit: float = 0.05

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.quantity < 1:
            raise ValueError(f'quantity must be >= 1, got {self.quantity}')
        if self.credit_min <= 0:
            raise ValueError(f'credit_min must be > 0, got {self.credit_min}')
        if self.min_market_credit < 0:
            raise ValueError(f'min_market_credit must be >= 0, got {self.min_market_credit}')
        if self.credit_max_put < self.credit_min:
            raise ValueError(
                f'credit_max_put ({self.credit_max_put}) must be >= credit_min ({self.credit_min})'
            )
        if self.credit_max_call < self.credit_min:
            raise ValueError(
                f'credit_max_call ({self.credit_max_call}) must be >= credit_min ({self.credit_min})'
            )
        if self.spread_width_min <= 0 or self.spread_width_max <= 0:
            raise ValueError('spread_width_min and spread_width_max must be > 0')
        if self.spread_width_min > self.spread_width_max:
            raise ValueError(
                f'spread_width_min ({self.spread_width_min}) must be <= spread_width_max ({self.spread_width_max})'
            )
        if self.otm_min < 0:
            raise ValueError(f'otm_min must be >= 0, got {self.otm_min}')
        if self.otm_min > self.otm_max:
            raise ValueError(f'otm_min ({self.otm_min}) must be <= otm_max ({self.otm_max})')
        if self.quote_source not in _VALID_QUOTE_SOURCES:
            raise ValueError(
                f"quote_source must be one of {sorted(_VALID_QUOTE_SOURCES)}, got {self.quote_source!r}"
            )

    @classmethod
    def from_meic_config(cls) -> 'CreditEntryConfig':
        return cls(
            spread_width_min=meic_config.SPREAD_WIDTH_MIN,
            spread_width_max=meic_config.SPREAD_WIDTH_MAX,
            credit_min=meic_config.CREDIT_MIN,
            credit_max_put=meic_config.CREDIT_MAX_P,
            credit_max_call=meic_config.CREDIT_MAX_C,
            otm_min=meic_config.OTM_MIN,
            otm_max=meic_config.OTM_MAX,
            quantity=meic_config.QUANTITY,
        )

    @classmethod
    def from_overrides(cls, overrides: dict | None) -> 'CreditEntryConfig':
        """Build config from defaults + YAML overrides (validated)."""
        base = cls.from_meic_config()
        if not overrides:
            return base
        allowed = {f.name for f in fields(cls)}
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f'unknown entry_config keys: {sorted(unknown)}')
        data = {f.name: getattr(base, f.name) for f in fields(cls)}
        data.update(overrides)
        return cls(**data)

    def credit_max_for_side(self, side: str) -> float:
        return self.credit_max_put if side.upper() == 'P' else self.credit_max_call
