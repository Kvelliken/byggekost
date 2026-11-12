"""Tester for byggekostnadsmodellen.

Kjøres med:  python -m pytest -q

Testene bruker kunstige serier der fasiten er kjent, slik at de ikke er
avhengige av SSB eller av hva de ekte tallene tilfeldigvis er.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as C  # noqa: E402
import modell as M  # noqa: E402


def kunstig(aar=30, vekst=0.04, stoy=0.003, mai_hopp=0.0, fro=1, slutt="2026-09"):
    """Indeks med kjent årlig vekst, valgfritt mai-hopp og tilfeldig støy."""
    n = 12 * aar + 1
    idx = pd.period_range(end=slutt, periods=n, freq="M")
    rng = np.random.default_rng(fro)
    d = np.full(n - 1, np.log1p(vekst) / 12) + rng.normal(0, stoy, n - 1)
    mnd = idx.month.values[1:]
    d = d + np.where(mnd == 5, mai_hopp, -mai_hopp / 11)
    return pd.Series(100 * np.exp(np.r_[0, np.cumsum(d)]), index=idx)


def test_vinduet_er_de_siste_tjue_aarene():
    m = M.Modell(kunstig(aar=40))
    assert len(m.vindu) == 12 * C.VINDU_AAR + 1
    assert m.vindu.index[-1] == m.full.index[-1]


def test_kort_serie_bruker_alt_som_finnes():
    m = M.Modell(kunstig(aar=12))
    assert len(m.vindu) == 12 * 12 + 1


def test_for_kort_serie_avvises():
    with pytest.raises(RuntimeError):
        M.Modell(kunstig(aar=5))


def test_finner_riktig_snittvekst():
    m = M.Modell(kunstig(vekst=0.05, stoy=0.001))
    assert abs(np.expm1(12 * m.drift_data) - 0.05) < 0.002


def test_sesongen_summerer_til_null_og_finner_mai_hoppet():
    m = M.Modell(kunstig(mai_hopp=0.01, stoy=0.0005))
    assert abs(m.sesong.sum()) < 1e-12
    assert m.sesong.argmax() == 4  # mai
    assert abs(m.sesong[4] - 0.01) < 0.001


def test_sesongen_folger_med_naar_monsteret_endrer_seg():
    """Mai-hoppet tredobles de siste fem årene. Mønsteret skal ligge nærmere
    det nye nivået enn snittet over hele vinduet gjør."""
    s = kunstig(mai_hopp=0.005, stoy=0.0002)
    d = np.diff(np.log(s.values))
    mnd, aar = s.index.month.values[1:], s.index.year.values[1:]
    ny = aar >= 2022
    d = d + np.where(ny & (mnd == 5), 0.010, 0) - np.where(ny & (mnd != 5), 0.010 / 11, 0)
    s2 = pd.Series(100 * np.exp(np.r_[0, np.cumsum(d)]), index=s.index)
    m = M.Modell(s2)
    assert m.sesong[4] > 0.010


def test_ankeret_folger_inflasjonsmaalet_som_gjaldt():
    assert M.anker_for(pd.Period("1999-06", "M")) is None
    assert M.anker_for(pd.Period("2010-01", "M")) == pytest.approx(0.025 + C.REALVEKST_ANTATT)
    assert M.anker_for(pd.Period("2026-09", "M")) == pytest.approx(0.020 + C.REALVEKST_ANTATT)


def test_ankeret_trekker_mot_seg_og_kan_slaas_av(monkeypatch):
    s = kunstig(vekst=0.06)
    m = M.Modell(s)
    assert m.anker < np.expm1(12 * m.drift) < np.expm1(12 * m.drift_data)
    assert 0 < m.datavekt < 1
    monkeypatch.setattr(C, "ANKER_USIKKERHET", 0.0)
    uten = M.Modell(s)
    assert uten.anker is None and uten.drift == uten.drift_data and uten.datavekt == 1.0


def test_samme_data_gir_samme_svar():
    s = kunstig()
    a = M.Modell(s).simuler(24, 200, 5)
    b = M.Modell(s).simuler(24, 200, 5)
    assert np.array_equal(a, b)


def test_aarlig_vekst_er_cagr():
    s = kunstig()
    m = M.Modell(s)
    y0 = np.log(s.values[-1])
    bane = np.tile(y0 + np.log(1.03) * np.arange(1, 121) / 12, (3, 1))
    for aar in (1, 3, 10):
        assert m.aarlig_vekst(bane, aar) == pytest.approx(0.03)


def test_medianen_ligger_naer_sann_vekst_og_intervallet_smalner():
    m = M.Modell(kunstig(vekst=0.03, stoy=0.001))
    b = m.simuler(120, 3000, 1)
    bredde = []
    for aar in (1, 3, 5, 10):
        p10, p50, p90 = np.percentile(m.aarlig_vekst(b, aar), [10, 50, 90])
        assert abs(p50 - 0.03) < 0.006
        bredde.append(p90 - p10)
    assert bredde == sorted(bredde, reverse=True)


def test_oppbyggingen_ender_paa_medianen():
    m = M.Modell(kunstig())
    b = m.simuler(120, 2000, 1)
    f = {a: m.aarlig_vekst(b, a) for a in (1, 10)}
    t = M.oppbygging(m, f)
    for a in (1, 10):
        assert t[a]["median"] == pytest.approx(float(np.median(f[a])))
        assert t[a]["data"] == pytest.approx(np.expm1(12 * m.drift_data))
        assert t[a]["p10"] < t[a]["median"] < t[a]["p90"]


def test_tilbaketesten_ser_ikke_inn_i_fremtiden(monkeypatch):
    """Endrer vi fremtiden etter et startpunkt, skal prognosen laget på
    startpunktet være uendret. Bare fasiten skal flytte seg."""
    monkeypatch.setattr(C, "TILBAKETEST_FRA", "2010M01")
    monkeypatch.setattr(C, "TILBAKETEST_STEG", 36)
    monkeypatch.setattr(C, "TILBAKETEST_BANER", 200)
    s = kunstig(aar=40)
    endret = s.copy()
    endret[endret.index > pd.Period("2016-01", "M")] *= 1.2
    a = M.tilbaketest(s)
    b = M.tilbaketest(endret)
    a1 = a[a.startpunkt <= "2016-01"].reset_index(drop=True)
    b1 = b[b.startpunkt <= "2016-01"].reset_index(drop=True)
    assert len(a1) > 0
    assert np.allclose(a1.modell, b1.modell)
    assert np.allclose(a1.p10, b1.p10)
    assert not np.allclose(a1.faktisk, b1.faktisk)


def test_dekningen_er_rimelig_paa_kunstige_data(monkeypatch):
    """På en serie som faktisk følger modellens forutsetninger skal rundt
    80 % av utfallene havne innenfor 80 %-intervallet."""
    monkeypatch.setattr(C, "TILBAKETEST_FRA", "1990M01")
    monkeypatch.setattr(C, "TILBAKETEST_STEG", 12)
    monkeypatch.setattr(C, "TILBAKETEST_BANER", 400)
    monkeypatch.setattr(C, "HORISONTER_AR", [1])
    monkeypatch.setattr(C, "ANKER_USIKKERHET", 0.0)
    treff = []
    for fro in range(6):
        tb = M.tilbaketest(kunstig(aar=60, stoy=0.004, fro=fro))
        treff.append(tb.innenfor80.mean())
    assert 0.65 < np.mean(treff) < 0.92


def test_periodetabellen_dekker_alle_startpunkter(monkeypatch):
    monkeypatch.setattr(C, "TILBAKETEST_FRA", "1998M01")
    monkeypatch.setattr(C, "TILBAKETEST_STEG", 12)
    monkeypatch.setattr(C, "TILBAKETEST_BANER", 100)
    tb = M.tilbaketest(kunstig(aar=45))
    per = M.tilbaketest_perioder(tb)
    for a in tb.horisont_ar.unique():
        assert sum(b["horisonter"].get(int(a), {"n": 0})["n"] for b in per) == (tb.horisont_ar == a).sum()


def test_datakontrollen_stopper_hull_og_urimelige_hopp():
    s = kunstig()
    M.kontroller_serie(s)
    with pytest.raises(RuntimeError):
        M.kontroller_serie(s.drop(s.index[50]))
    feil = s.copy()
    feil.iloc[100:] *= 1.5
    with pytest.raises(RuntimeError):
        M.kontroller_serie(feil)


def test_historikken_har_en_rad_per_datamaaned(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "DATA", str(tmp_path))
    s = kunstig()
    rader = [{"horisont_ar": 1, "median": 0.03, "p10": 0.02, "p90": 0.05}]
    M.oppdater_historikk(s, rader)
    rader[0]["median"] = 0.04
    M.oppdater_historikk(s, rader)  # samme datamåned kjørt på nytt
    M.oppdater_historikk(s.iloc[:-1], rader)  # en annen datamåned
    h = pd.read_csv(tmp_path / "resultater_historikk.csv")
    assert len(h) == 2
    assert h.loc[h.siste_periode == str(s.index[-1]), "median"].iloc[0] == 0.04


def test_norsk_tallformat():
    assert M.nf(3.14159, 2) == "3,14"
    assert M.nf(-0.5, 1) == "−0,5"
    assert M.nf(-0.001, 1) == "0,0"
    assert M.fortegn(0.25) == "+0,25"
    assert M.fortegn(-0.25) == "−0,25"


def test_lagret_oyeblikksbilde_kan_kjores():
    fil = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "indeks_siste.csv")
    s = M.les_lokal_indeks(fil)
    M.kontroller_serie(s)
    m = M.Modell(s)
    assert 0.0 < np.expm1(12 * m.drift) < 0.10
