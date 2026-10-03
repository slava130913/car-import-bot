from datetime import date

import pytest

from calc.engine import CarInput, age_category_from_date, calculate, load_rules, util_fee

RULES = load_rules()
RATES = dict(cny_rub=12.0, eur_rub=95.0)


def line(res, key):
    return next(ln for ln in res.lines if ln.key == key)


def test_age_category_boundaries():
    today = date(2026, 10, 3)
    assert age_category_from_date(date(2024, 1, 1), today) == "new"
    assert age_category_from_date(date(2023, 10, 4), today) == "new"
    assert age_category_from_date(date(2023, 10, 3), today) == "3-5"
    assert age_category_from_date(date(2021, 10, 4), today) == "3-5"
    assert age_category_from_date(date(2021, 10, 3), today) == "5+"


def test_preferential_util_new_and_old():
    car = CarInput(price_cny=100000, age="new", engine_cc=1500, power_hp=147, **RATES)
    assert util_fee(RULES, car) == (3400.0, "льготная ставка для физлица", False)
    car.age = "3-5"
    assert util_fee(RULES, car)[0] == 5200.0


def test_commercial_util_over_160hp():
    car = CarInput(price_cny=100000, age="new", engine_cc=1500, power_hp=170, **RATES)
    amount, _, unverified = util_fee(RULES, car)
    assert amount == 900000.0 and not unverified
    car.age = "5+"
    assert util_fee(RULES, car)[0] == 1492800.0


def test_util_exactly_160hp_is_preferential_but_161_is_not():
    a = CarInput(price_cny=1, age="new", engine_cc=2000, power_hp=160, **RATES)
    b = CarInput(price_cny=1, age="new", engine_cc=2000, power_hp=161, **RATES)
    assert util_fee(RULES, a)[0] == 3400.0
    assert util_fee(RULES, b)[0] == 900000.0  # 2000 см³ входит в сетку 1000-2000 (включительно), 161 л.с. -> сегмент до 190


def test_util_volume_over_3000_loses_preferential_even_under_160hp():
    car = CarInput(price_cny=1, age="new", engine_cc=3200, power_hp=150, **RATES)
    assert util_fee(RULES, car)[0] == 2584000.0


def test_util_small_and_big_engines_under_160hp():
    small = CarInput(price_cny=1, age="new", engine_cc=998, power_hp=170, **RATES)  # >160 л.с. при 1 л: нет льготы, сетка не подтверждена
    amount, _, unverified = util_fee(RULES, small)
    assert amount is None and unverified
    big = CarInput(price_cny=1, age="3-5", engine_cc=4400, power_hp=150, **RATES)
    assert util_fee(RULES, big)[0] == 4325800.0


def test_util_unverified_cells():
    car = CarInput(price_cny=1, age="new", engine_cc=4000, power_hp=300, **RATES)
    amount, _, unverified = util_fee(RULES, car)
    assert amount is None and unverified
    res = calculate(car, RULES)
    assert res.unverified
    assert any("неподтверждённая" in w for w in res.warnings)


def test_ev_preferential_threshold_80hp():
    ev = CarInput(price_cny=1, age="new", engine_cc=0, power_hp=80, fuel="ev", **RATES)
    assert util_fee(RULES, ev)[0] == 3400.0
    ev.power_hp = 81
    assert util_fee(RULES, ev)[0] == 991200.0


def test_duty_new_car_percent_vs_min_per_cc():
    # 100 000 ¥ * 12 = 1 200 000 ₽ = 12 631.6 € -> сегмент 8500..16700: 48%, min 3.5 €/см³
    car = CarInput(price_cny=100000, age="new", engine_cc=1500, power_hp=147, **RATES)
    res = calculate(car, RULES)
    duty = line(res, "duty")
    expected_eur = max(1200000 / 95 * 0.48, 1500 * 3.5)
    assert duty.mid == round(expected_eur * 95)
    # для 1500 см³ минимум 5250 € меньше, чем 48% = 6063 €, значит берём процент
    assert duty.mid == round(1200000 * 0.48)


def test_duty_new_car_min_per_cc_wins_for_cheap_big_engine():
    car = CarInput(price_cny=50000, age="new", engine_cc=2500, power_hp=150, **RATES)
    res = calculate(car, RULES)
    # 600 000 ₽ = 6315.8 € -> 54% = 3410 €, минимум 2.5*2500 = 6250 € -> минимум побеждает
    assert line(res, "duty").mid == round(6250 * 95)


def test_duty_used_car_per_cc():
    car = CarInput(price_cny=80000, age="3-5", engine_cc=1998, power_hp=150, **RATES)
    assert line(calculate(car, RULES), "duty").mid == round(1998 * 2.7 * 95)
    car.age = "5+"
    assert line(calculate(car, RULES), "duty").mid == round(1998 * 4.8 * 95)


def test_ev_scheme_duty_excise_vat():
    car = CarInput(price_cny=150000, age="new", engine_cc=0, power_hp=204, fuel="ev", **RATES)
    res = calculate(car, RULES)
    price = 150000 * 12
    duty = price * 0.15
    excise = 1004 * 204
    vat = (price + duty + excise) * 0.22
    assert line(res, "duty").mid == round(duty)
    assert line(res, "excise").mid == round(excise)
    assert line(res, "vat").mid == round(vat)
    assert line(res, "util").mid == 2193600.0  # 204 л.с. -> сегмент до 220


def test_customs_fee_brackets():
    cheap = CarInput(price_cny=10000, age="new", engine_cc=1000, power_hp=70, **RATES)   # 120 000 ₽
    mid = CarInput(price_cny=100000, age="new", engine_cc=1500, power_hp=150, **RATES)  # 1 200 000 ₽ (включительно)
    big = CarInput(price_cny=100001, age="new", engine_cc=1500, power_hp=150, **RATES)
    assert line(calculate(cheap, RULES), "customs_fee").mid == 775
    assert line(calculate(mid, RULES), "customs_fee").mid == 3100
    assert line(calculate(big, RULES), "customs_fee").mid == 8530


def test_totals_consistent_and_ranges_ordered():
    car = CarInput(price_cny=120000, age="new", engine_cc=1498, power_hp=147, route="sea", destination="tula", **RATES)
    res = calculate(car, RULES)
    assert res.total_mid == sum(ln.mid for ln in res.lines)
    assert res.total_low <= res.total_mid <= res.total_high
    assert line(res, "domestic").label.endswith("Тула")
    assert line(res, "logistics").label.startswith("Морем")


def test_fallback_rates_used_when_not_given():
    car = CarInput(price_cny=1000, age="new", engine_cc=1000, power_hp=70)
    res = calculate(car, RULES)
    assert res.rates["CNY"] == RULES["fallback_rates"]["CNY"]


def test_geely_coolray_sanity():
    """Geely Coolray 1.5T 147 л.с., ~91 000 ¥ новый: всё под ключ должно быть
    в районе 2 млн ₽, как в публичных разборах, а не 3 млн."""
    car = CarInput(price_cny=91000, age="new", engine_cc=1498, power_hp=147, destination="moscow", **RATES)
    res = calculate(car, RULES)
    assert 1_700_000 < res.total_mid < 2_300_000, res.total_mid
    assert line(res, "util").mid == 3400
