"""El borrador de dinero real no se puede encender ni subir de tope."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from mesa_trading.config import RealMoneyConfig, load_desk, repo_root
from mesa_trading.real_money import phase_is_on


def test_real_money_is_off_with_the_hard_caps():
    config = load_desk(repo_root() / "config" / "desk.yaml")
    assert config.real_money.enabled is False
    assert config.real_money.venue == "hyperliquid"
    assert config.real_money.max_capital_usd == Decimal("100")
    assert config.real_money.max_order_usd == Decimal("25")
    assert config.real_money.max_daily_loss_usd == Decimal("10")


def test_real_money_cannot_be_enabled(tmp_path):
    text = (repo_root() / "config" / "desk.yaml").read_text(encoding="utf-8")
    path = tmp_path / "desk.yaml"
    path.write_text(text.replace("real_money:\n  enabled: false", "real_money:\n  enabled: true"), encoding="utf-8")
    with pytest.raises(ValueError, match="dinero real sigue apagado"):
        load_desk(path)


def test_real_money_config_is_frozen():
    config = load_desk(repo_root() / "config" / "desk.yaml")
    with pytest.raises(ValidationError):
        config.real_money.enabled = True
    with pytest.raises(ValidationError):
        config.capital_usd = Decimal("1")
    config.bots["gambler"].mode = "shadow"
    config.bots["gambler"].mode = "simulated"
    built = RealMoneyConfig.model_construct(enabled=True)
    assert built.enabled is True
    assert phase_is_on(built) is False
    assert phase_is_on(config.real_money) is False
