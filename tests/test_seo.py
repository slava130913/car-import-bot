"""Страницы моделей для поиска: генерируются, выводы по утильсбору верные, sitemap полный."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("build_seo", ROOT / "scripts" / "build_seo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pages_verdicts_and_sitemap(tmp_path, monkeypatch):
    mod = load()
    monkeypatch.setattr(mod, "OUT", tmp_path / "auto")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "web").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "models.json").write_text((ROOT / "data" / "models.json").read_text(encoding="utf-8"), encoding="utf-8")
    mod.main()
    models = json.loads((ROOT / "data" / "models.json").read_text(encoding="utf-8"))["models"]
    pages = list((tmp_path / "auto").glob("*.html"))
    assert len(pages) == len(models) + 1  # + index
    coolray = (tmp_path / "auto" / "coolray.html").read_text(encoding="utf-8")
    assert "Проходит по льготному утильсбору" in coolray and "#model-coolray" in coolray
    monjaro = (tmp_path / "auto" / "monjaro.html").read_text(encoding="utf-8")
    assert "Льготный утильсбор не действует" in monjaro and "1 010 400 ₽" in monjaro
    li = (tmp_path / "auto" / "li_l7.html").read_text(encoding="utf-8")
    assert "не подтверждена" in li and "30-минутной" in li
    sitemap = (tmp_path / "web" / "sitemap.xml").read_text(encoding="utf-8")
    assert sitemap.count("<url>") == len(models) + 3
    assert "Sitemap:" in (tmp_path / "web" / "robots.txt").read_text(encoding="utf-8")
