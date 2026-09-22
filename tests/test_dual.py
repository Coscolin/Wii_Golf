"""
Pruebas de la geometría de dos tablas y del análisis dual.

    .venv\\Scripts\\python.exe -m pytest tests -q        (pytest solo para desarrollo)
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wiigolf.analysis import DUAL_HEADER, SINGLE_HEADER, analyze, load_csv  # noqa: E402
from wiigolf.dual import (BOARD_PHYS_DEPTH_CM, HALF_X_CM, HALF_Y_CM, Layout, combine,  # noqa: E402
                          foot_cop_cm, spread_foot_load)

ZERO = {"TR": 0.0, "BR": 0.0, "TL": 0.0, "BL": 0.0}
FLAT = {"TR": 10.0, "BR": 10.0, "TL": 10.0, "BL": 10.0}


def test_layout_center_distance():
    assert Layout(gap_cm=0.0).center_distance_cm == pytest.approx(BOARD_PHYS_DEPTH_CM)
    assert Layout(gap_cm=4.0).center_distance_cm == pytest.approx(BOARD_PHYS_DEPTH_CM + 4.0)
    assert Layout(gap_cm=None).center_distance_cm == pytest.approx(BOARD_PHYS_DEPTH_CM)  # sin indicar = pegadas
    with pytest.raises(ValueError):
        Layout(button="up")


def test_symmetric_load_gives_centered_cop():
    c = combine(FLAT, FLAT, Layout(gap_cm=0.0))
    assert c["total"] == pytest.approx(80.0)
    assert c["cop_x_cm"] == pytest.approx(0.0)
    assert c["cop_y_cm"] == pytest.approx(0.0)


def test_all_weight_on_right_board_is_at_plus_half_distance():
    lay = Layout(gap_cm=6.0)
    c = combine(ZERO, FLAT, lay)
    assert c["cop_x_cm"] == pytest.approx(lay.center_distance_cm / 2)
    c = combine(FLAT, ZERO, lay)
    assert c["cop_x_cm"] == pytest.approx(-lay.center_distance_cm / 2)


@pytest.mark.parametrize("button", ["left", "right"])
def test_toes_and_lateral_signs(button):
    """Con el botón hacia la izquierda la tabla está girada 90° antihorario: los sensores
    TR/BR (lado local +x) quedan hacia la punta; con el botón a la derecha, hacia el talón."""
    lay = Layout(gap_cm=0.0, button=button)
    toes_local_plus_x = {"TR": 20.0, "BR": 20.0, "TL": 0.0, "BL": 0.0}
    _, x, y = foot_cop_cm(toes_local_plus_x, lay)
    assert x == pytest.approx(0.0)
    assert y == pytest.approx(HALF_X_CM if button == "left" else -HALF_X_CM)
    top_local_plus_y = {"TR": 20.0, "TL": 20.0, "BR": 0.0, "BL": 0.0}
    _, x, y = foot_cop_cm(top_local_plus_y, lay)
    assert y == pytest.approx(0.0)
    assert x == pytest.approx(-HALF_Y_CM if button == "left" else HALF_Y_CM)


def test_spread_is_inverse_of_foot_cop():
    for button in ("left", "right"):
        lay = Layout(gap_cm=0.0, button=button)
        for x, y in ((0.0, 0.0), (3.5, -7.0), (-5.0, 12.0)):
            kg = spread_foot_load(40.0, x, y, lay)
            total, xx, yy = foot_cop_cm(kg, lay)
            assert total == pytest.approx(40.0)
            assert xx == pytest.approx(x, abs=1e-9)
            assert yy == pytest.approx(y, abs=1e-9)


def test_light_foot_sits_at_board_center():
    light = {"TR": 1.0, "BR": 0.0, "TL": 0.0, "BL": 0.0}   # < MIN_LOAD_KG
    _, x, y = foot_cop_cm(light, Layout(gap_cm=0.0))
    assert (x, y) == (0.0, 0.0)
    c = combine(light, FLAT, Layout(gap_cm=0.0))
    assert c["L"]["x_cm"] == 0.0 and c["L"]["y_cm"] == 0.0


def test_combine_accepts_arrays():
    arr = {k: np.array([v, 2 * v, 0.0]) for k, v in FLAT.items()}
    c = combine(arr, arr, Layout(gap_cm=0.0))
    assert c["total"].shape == (3,)
    assert c["total"][1] == pytest.approx(160.0)
    assert c["cop_x_cm"][2] == 0.0   # sin carga -> (0, 0), sin NaN


def _write_csv(path: Path, header: list[str], rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def test_load_csv_detects_mode(tmp_path):
    single = tmp_path / "s.csv"
    _write_csv(single, SINGLE_HEADER, [[0.0, 10, 10, 10, 10, 40, 0, 0], [0.01, 10, 10, 10, 10, 40, 0, 0]])
    cap = load_csv(single)
    assert not cap.dual and cap.total[0] == pytest.approx(40.0)
    dual = tmp_path / "d.csv"
    _write_csv(dual, DUAL_HEADER, [[0.0] + [5] * 4 + [10] * 4 + [60, 5.0, 0.0, 0.0, -0.004]])
    cap = load_csv(dual)
    assert cap.dual and cap.left()[0] == pytest.approx(20.0) and cap.right()[0] == pytest.approx(40.0)


def _synthetic(tmp_path: Path, *args: str) -> Path:
    out = tmp_path / ("dual.csv" if "--dual" in args else "single.csv")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_synthetic_swing.py"), str(out), *args],
                   check=True, capture_output=True, env={**__import__("os").environ, "PYTHONUTF8": "1"})
    return out


def test_dual_synthetic_analysis(tmp_path):
    csv_path = _synthetic(tmp_path, "--dual", "--gap", "3")
    cap = load_csv(csv_path)
    assert cap.dual
    an = analyze(cap, layout=Layout(gap_cm=3.0))
    assert an.dual and an.layout.center_distance_cm == pytest.approx(BOARD_PHYS_DEPTH_CM + 3.0)
    assert an.t_top == pytest.approx(2.80, abs=0.05)
    assert an.t_impact == pytest.approx(3.02, abs=0.05)
    m = an.metrics()
    assert m["trail_pct_en_top"] > 65
    assert m["trail_punta_talon_en_top_cm"] == pytest.approx(6.0, abs=0.6)      # el trail rueda a la punta
    assert m["lead_punta_talon_en_impacto_cm"] == pytest.approx(-6.0, abs=0.6)  # el lead hunde el talón
    assert m["pico_fuerza_lead_pct_bw"] > m["pico_fuerza_trail_pct_bw"]
    for key in ("cop_lead_rango_punta_talon_cm", "cop_trail_rango_punta_talon_cm", "lead_lateral_en_impacto_cm"):
        assert key in m
    # la separación entre tablas mueve el CoP global lateral: con todo el peso en un pie,
    # el rango lateral crece con el hueco
    an0 = analyze(cap, layout=Layout(gap_cm=0.0))
    assert m["cop_x_rango_cm"] > an0.metrics()["cop_x_rango_cm"]


def test_single_synthetic_unchanged(tmp_path):
    csv_path = _synthetic(tmp_path)
    cap = load_csv(csv_path)
    assert not cap.dual
    an = analyze(cap)
    assert not an.dual and an.feet is None
    assert an.t_top == pytest.approx(2.80, abs=0.05)
    assert an.t_impact == pytest.approx(3.02, abs=0.05)
    assert "trail_punta_talon_en_top_cm" not in an.metrics()


def test_flip_y_negates_dual_heel_toe(tmp_path):
    cap = load_csv(_synthetic(tmp_path, "--dual"))
    a = analyze(cap, layout=Layout(gap_cm=0.0))
    b = analyze(cap, layout=Layout(gap_cm=0.0), flip_y=True)
    assert b.metrics()["trail_punta_talon_en_top_cm"] == pytest.approx(-a.metrics()["trail_punta_talon_en_top_cm"])
