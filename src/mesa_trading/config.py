"""Esquema estricto de la mesa. Cualquier modo fuera de sombra/simulado aborta el arranque."""

from __future__ import annotations

import os
from decimal import Decimal
from importlib.resources import as_file, files
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mesa_trading.constants import (
    ALLOWED_MODELS,
    DAILY_BUDGET_CAP_USD,
    DAILY_BUDGET_USD,
    MONTHLY_BUDGET_USD,
    PINNED_MODEL,
)


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_position_pct: Decimal
    max_exposure_pct: Decimal
    daily_loss_pct: Decimal
    leverage: Decimal
    single_position: bool


class JudgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    deadline_ms: int = 2000
    monthly_budget_usd: Decimal
    daily_budget_usd: Decimal = DAILY_BUDGET_USD
    price_usd_per_million_input_tokens: Decimal = Decimal("0.042")
    url: str = ""

    @field_validator("model")
    @classmethod
    def pin_model(cls, value: str) -> str:
        if "latest" in value or value not in ALLOWED_MODELS:
            raise ValueError(f"modelo no permitido: {value}; fijado {PINNED_MODEL}")
        return value

    @field_validator("monthly_budget_usd")
    @classmethod
    def cap_budget(cls, value: Decimal) -> Decimal:
        if value != MONTHLY_BUDGET_USD:
            raise ValueError(f"monthly_budget_usd debe ser {MONTHLY_BUDGET_USD}")
        return value

    @field_validator("daily_budget_usd")
    @classmethod
    def cap_daily(cls, value: Decimal) -> Decimal:
        if value <= 0 or value > DAILY_BUDGET_CAP_USD:
            raise ValueError(f"daily_budget_usd debe estar entre 0 y {DAILY_BUDGET_CAP_USD}")
        return value


class BotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    mode: Literal["shadow", "simulated"]
    markets: list[str]


class ConnectorToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class MarketConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    venue: Literal["coinbase", "geckoterminal", "hyperliquid", "fixture"]
    kind: Literal["spot", "perp"]
    product: str
    network: str = ""
    pool: str = ""
    fee_schedule: str = "coinbase_spot"
    tick: Decimal = Decimal("0.01")


class RangeBand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low: Decimal
    high: Decimal


class FilterConfig(BaseModel):
    """Umbrales del embudo. Staging los deja en el YAML para poder aflojarlos con un PR."""

    model_config = ConfigDict(extra="forbid")

    max_spread_bps: Decimal = Decimal("25")
    min_liquidity_usd: Decimal = Decimal("10000")
    max_vol: Decimal = Decimal("0.05")
    max_fee_fraction: Decimal = Decimal("0.02")


class RealMoneyConfig(BaseModel):
    """Borrador. Encenderlo exige otro PR y el OK explícito de Pedro."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True)

    enabled: bool = False
    venue: str = "hyperliquid"
    max_capital_usd: Decimal = Decimal("100")
    max_order_usd: Decimal = Decimal("25")
    max_daily_loss_usd: Decimal = Decimal("10")

    @model_validator(mode="after")
    def stay_off(self) -> RealMoneyConfig:
        if self.enabled:
            raise ValueError("dinero real sigue apagado: hace falta el OK explícito de Pedro y otro PR")
        if self.max_capital_usd > Decimal("100"):
            raise ValueError("el capital real no puede pasar de 100 USD")
        if self.max_order_usd > Decimal("25"):
            raise ValueError("una operación real no puede pasar de 25 USD")
        if self.max_daily_loss_usd > Decimal("10"):
            raise ValueError("la pérdida diaria real no puede pasar de 10 USD")
        if self.venue != "hyperliquid":
            raise ValueError("el borrador solo contempla Hyperliquid")
        return self


class CarryConfig(BaseModel):
    """Funding carry en papel (#31): spot largo + perp corto del mismo nocional."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    capital_usd: Decimal = Decimal("10000")
    activos: list[str] = Field(default_factory=lambda: ["ETH", "BTC"])
    # Tabla de config/fees.yaml para la pata spot de cada activo. La pata perp usa fee_perp.
    fee_spot: dict[str, str] = Field(
        default_factory=lambda: {"ETH": "uniswap_v3_weth_usdc_005", "BTC": "dex_pool"}
    )
    fee_perp: str = "hyperliquid_perp"
    # Spread supuesto del spot (no hay libro spot): ~10 bp como el pool sintético.
    spread_spot_bps: Decimal = Decimal("10")
    # Liquidez supuesta para el impacto del deslizamiento de las dos patas.
    liquidez_supuesta_usd: Decimal = Decimal("50000000")
    extra_bps: Decimal = Decimal("1")
    # Nocional de cada pata como fracción del equity del libro de carry.
    nocional_pct: Decimal = Decimal("0.25")
    # Horas de funding para el anualizado esperado (Hyperliquid paga cada hora).
    ventana_funding_horas: int = 72
    # Los costos de ida y vuelta se reparten en este horizonte para anualizarlos.
    horizonte_dias: int = 30
    # Entra solo si funding anualizado - costos anualizados > umbral.
    umbral_neto_anual: Decimal = Decimal("0.05")
    # Cierra si el funding medio de estas últimas horas es negativo.
    horas_negativo_cierre: int = 24
    # Cierra si la base (perp - spot) / spot se mueve más que esto desde la entrada.
    limite_base: Decimal = Decimal("0.01")
    descarte_dias: int = 30
    descarte_min_pct_positivo: Decimal = Decimal("0.60")


class TendenciaConfig(BaseModel):
    """Tendencia diaria/4h en papel (#31). Perps de Hyperliquid, largo o corto."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    capital_usd: Decimal = Decimal("10000")
    activos: list[str] = Field(default_factory=lambda: ["ETH", "BTC"])
    fee_schedule: str = "hyperliquid_perp"
    liquidez_supuesta_usd: Decimal = Decimal("50000000")
    extra_bps: Decimal = Decimal("1")
    # Riesgo por operación: nocional = riesgo_pct × equity / (distancia al stop / precio).
    riesgo_pct: Decimal = Decimal("0.005")
    # Señal: régimen por SMA de cierres diarios; entrada por ruptura Donchian en 4h.
    sma_1d: int = 50
    donchian_entrada_4h: int = 20
    donchian_salida_4h: int = 10
    atr_1d: int = 14
    atr_mult_stop: Decimal = Decimal("2")
    # Supuesto (no medido), igual que expected_edge_bps de gambler/house en policy.yaml.
    # Se fija en config/desk.yaml (sombra.tendencia.expected_edge_bps); es decisión de producto.
    expected_edge_bps: Decimal = Field(default=Decimal("80"), ge=0)
    descarte_operaciones: int = 20
    descarte_dias: int = 60


class SombraConfig(BaseModel):
    """Estrategias en papel con libro propio. No tocan el libro de house/gambler."""

    model_config = ConfigDict(extra="forbid")

    funding_historial_dias: int = 120
    # Tope de tiempo total de sombra en cada tick, en segundos. Se revisa antes de
    # cada llamada HTTP; si se pasa, sombra corta limpio y sigue en el próximo tick.
    # Lo más que se excede es una llamada (timeout HTTP de 8 s).
    deadline_segundos: float = Field(default=20.0, gt=0)
    # Páginas de fundingHistory (≤ 500 h) por activo y por tick. El relleno inicial
    # se reparte en varios ticks guardando el avance.
    paginas_funding_por_tick: int = Field(default=2, ge=1)
    carry: CarryConfig = Field(default_factory=CarryConfig)
    tendencia: TendenciaConfig = Field(default_factory=TendenciaConfig)


class DeskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True)

    cadence_minutes: int
    snapshot_max_age_s: int
    capital_usd: Decimal
    timezone_reports: str
    risk: RiskConfig
    judge: JudgeConfig
    bots: dict[str, BotConfig]
    connectors: dict[str, ConnectorToggle]
    markets: dict[str, MarketConfig]
    house_range: dict[str, RangeBand] = Field(default_factory=dict)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    policy_version: int = 1
    data_retries: int = 2
    real_money: RealMoneyConfig = Field(default_factory=RealMoneyConfig)
    sombra: SombraConfig = Field(default_factory=SombraConfig)

    @model_validator(mode="after")
    def required_bots(self) -> DeskConfig:
        missing = {"gambler", "house", "spy"} - set(self.bots)
        if missing:
            raise ValueError(f"faltan bots: {sorted(missing)}")
        if self.risk.leverage != Decimal("1"):
            raise ValueError("apalancamiento permitido: 1")
        if not self.risk.single_position:
            raise ValueError("la mesa opera una sola posición")
        coinbase = self.connectors.get("coinbase")
        if coinbase is not None and coinbase.enabled:
            raise ValueError("el conector de Coinbase está apagado: sus términos no permiten este uso")
        for name, market in self.markets.items():
            if market.venue == "coinbase":
                raise ValueError(f"el mercado {name} no puede usar Coinbase")
        # Sombra (#31): el mismo tope por posición que la mesa; no lo sube.
        if self.sombra.carry.nocional_pct > self.risk.max_position_pct:
            raise ValueError("sombra.carry.nocional_pct no puede pasar de risk.max_position_pct")
        return self


class PromotionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_shadow_days: int
    min_resolved_per_question: int
    min_brier_skill: Decimal
    min_bucket_samples: int
    max_bucket_deviation_pp: Decimal
    max_missed_cycle_pct: Decimal
    # Veredicto de la estrategia en simulado. No cambia el modo.
    min_closed_trades: int = 200
    min_pnl_usd: Decimal = Decimal("0")
    max_drawdown_pct: Decimal = Decimal("0.15")


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"config inválida: {path}")
    return data


def load_desk(path: Path) -> DeskConfig:
    return DeskConfig.model_validate(load_yaml(path))


def load_promotion(path: Path) -> PromotionConfig:
    return PromotionConfig.model_validate(load_yaml(path))


def repo_root() -> Path:
    """Raíz del checkout, o `/app` / el cwd si ahí está `config/desk.yaml`.

    En el árbol fuente, `src/mesa_trading/config.py` queda dos niveles bajo la raíz.
    Tras `pip install .`, `__file__` vive en site-packages y ese cálculo no es el repo.
    """
    source = _source_checkout_root()
    if source is not None:
        return source
    for base in (Path("/app"), Path.cwd()):
        if (base / "config" / "desk.yaml").is_file():
            return base.resolve()
    return Path(__file__).resolve().parents[2]


def default_desk_path() -> Path:
    return _resolve_config_file("desk.yaml", "MESA_DESK_CONFIG")


def default_promotion_path() -> Path:
    return _resolve_config_file("promotion.yaml", "MESA_PROMOTION_CONFIG")


def default_fees_path() -> Path:
    return _resolve_config_file("fees.yaml", "MESA_FEES_CONFIG")


def default_policy_path() -> Path:
    return _resolve_config_file("policy.yaml", "MESA_POLICY_CONFIG")


def _resolve_config_file(filename: str, env_name: str) -> Path:
    override = os.environ.get(env_name, "").strip()
    if override:
        return Path(override)
    candidates: list[Path] = []
    source = _source_checkout_root()
    if source is not None:
        candidates.append(source / "config" / filename)
    bundled = _bundled_file(filename)
    if bundled is not None:
        candidates.append(bundled)
    for base in (Path("/app"), Path.cwd()):
        candidates.append(base / "config" / filename)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if candidates:
        return candidates[0]
    return Path(__file__).resolve().parents[2] / "config" / filename


def _source_checkout_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "config" / "desk.yaml").is_file():
        return candidate
    return None


def _bundled_file(filename: str) -> Path | None:
    """YAML copiados dentro del paquete al construir el wheel."""
    try:
        resource = files("mesa_trading").joinpath("bundled_config", filename)
    except ModuleNotFoundError:
        return None
    try:
        present = resource.is_file()
    except OSError:
        return None
    if not present:
        return None
    with as_file(resource) as extracted:
        path = Path(extracted)
        if path.is_file():
            return path
    return None
