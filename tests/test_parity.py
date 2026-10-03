"""Проверяет, что web/calc.js считает ровно так же, как calc/engine.py."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from calc.engine import CarInput, calculate, load_rules

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

CASES = [
    dict(price_cny=91000, age="new", engine_cc=1498, power_hp=147, fuel="ice", route="suifenhe", destination="moscow"),
    dict(price_cny=150000, age="new", engine_cc=1998, power_hp=170, fuel="ice", route="sea", destination="tula"),
    dict(price_cny=60000, age="3-5", engine_cc=1998, power_hp=150, fuel="ice", route="khorgos", destination="novosibirsk"),
    dict(price_cny=40000, age="5+", engine_cc=2400, power_hp=180, fuel="hybrid_par", route="zabaikalsk", destination="far_east"),
    dict(price_cny=150000, age="new", engine_cc=0, power_hp=204, fuel="ev", route="suifenhe", destination="moscow"),
    dict(price_cny=120000, age="new", engine_cc=0, power_hp=80, fuel="hybrid_seq", route="sea", destination="khabarovsk"),
    dict(price_cny=300000, age="new", engine_cc=3200, power_hp=272, fuel="ice", route="suifenhe", destination="moscow"),
    dict(price_cny=500000, age="new", engine_cc=4000, power_hp=450, fuel="ice", route="suifenhe", destination="moscow"),
    dict(price_cny=12345.5, age="new", engine_cc=999, power_hp=68, fuel="ice", route="suifenhe", destination="moscow"),
]
RATES = {"cny_rub": 12.4328, "eur_rub": 94.3201}

NODE_SCRIPT = """
const C = require(process.argv[2]);
const rules = JSON.parse(require('fs').readFileSync(process.argv[3], 'utf8'));
const cases = JSON.parse(require('fs').readFileSync(process.argv[4], 'utf8'));
process.stdout.write(JSON.stringify(cases.map(c => C.calculate(c, rules))));
"""


@pytest.mark.skipif(NODE is None, reason="node не установлен")
def test_js_matches_python(tmp_path: Path):
    rules = load_rules()
    cases = [dict(c, **RATES) for c in CASES]
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps(cases), encoding="utf-8")
    script = tmp_path / "run.js"
    script.write_text(NODE_SCRIPT, encoding="utf-8")
    out = subprocess.run(
        [NODE, str(script), str(ROOT / "web" / "calc.js"), str(ROOT / "data" / "rules.json"), str(cases_file)],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    js_results = json.loads(out.stdout)
    assert len(js_results) == len(cases)
    for case, js in zip(cases, js_results):
        py = calculate(CarInput(**case), rules).as_dict()
        assert js["total_mid"] == py["total_mid"], case
        assert js["total_low"] == py["total_low"], case
        assert js["total_high"] == py["total_high"], case
        assert js["unverified"] == py["unverified"], case
        assert js["warnings"] == py["warnings"], case
        assert [ln["key"] for ln in js["lines"]] == [ln["key"] for ln in py["lines"]]
        for a, b in zip(js["lines"], py["lines"]):
            assert a["mid"] == b["mid"], (case, a["key"])
            assert a["low"] == b["low"], (case, a["key"])
            assert a["high"] == b["high"], (case, a["key"])
            assert a["label"] == b["label"], (case, a["key"])
            assert a["note"] == b["note"], (case, a["key"])
