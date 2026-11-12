"""
Byggekostnadsmodell for boligblokk (SSB tabell 08655).

Kjøres med:  python modell.py            (henter ferske tall fra SSB)
             python modell.py --lokal    (bruker sist lagrede tall)

Gjør fem ting:
  1. Henter indeksen fra SSB og lagrer et øyeblikksbilde
  2. Estimerer en modell for månedlig kostnadsvekst
  3. Simulerer 10 000 mulige fremtider og regner ut fordelingen av
     gjennomsnittlig årlig vekst for 1, 3, 5 og 10 år
  4. Tester modellen mot historien, med nøyaktig samme regler som i dag
  5. Skriver grafer og en HTML-side til mappen docs/

Ingen andre filer enn config.py trenger å endres for vanlig bruk.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import matplotlib

matplotlib.use("Agg")  # ingen skjerm på serveren
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

import config as C

# ===========================================================================
# Utseende
# ===========================================================================

FARGE = {
    "blekk": "#17232B",
    "historikk": "#2E3F4A",
    "median": "#0F4C5C",
    "band": "#6B9AA6",
    "anker": "#8A6A2F",
    "svak": "#8E9AA0",
    "rutenett": "#DCE0DE",
    "flate": "#FFFFFF",
}

plt.rcParams.update(
    {
        "figure.facecolor": FARGE["flate"],
        "axes.facecolor": FARGE["flate"],
        "axes.edgecolor": FARGE["rutenett"],
        "axes.labelcolor": FARGE["blekk"],
        "axes.titlesize": 12,
        "axes.titleweight": "normal",
        "axes.grid": True,
        "grid.color": FARGE["rutenett"],
        "grid.linewidth": 0.7,
        "text.color": FARGE["blekk"],
        "xtick.color": FARGE["svak"],
        "ytick.color": FARGE["svak"],
        "font.size": 10,
        "figure.dpi": 130,
        "savefig.bbox": "tight",
    }
)

# Økes når metoden endres, slik at historikken ikke blander anslag fra
# ulike metoder.
MODELLVERSJON = "2"

UT = "docs"
DATA = "data"


def logg(melding: str) -> None:
    print(melding, flush=True)


# ===========================================================================
# 1. DATA
# ===========================================================================


BASIS = "https://data.ssb.no/api/pxwebapi/v2/tables"


def skriv_ut_kodeliste() -> str:
    """Henter metadata, viser hvilke koder tabellen har, og returnerer
    navnet på serien ARBEIDSTYPE_KODE peker på (tom streng hvis ukjent).

    Navnet skrives i rapporten, slik at det alltid går an å se hvilken
    serie tallene gjelder.
    """
    url = f"{BASIS}/{C.TABELL}/metadata?lang=no&outputFormat=json-stat2"
    try:
        svar = requests.get(url, timeout=60)
        svar.raise_for_status()
        meta = svar.json()
    except Exception as feil:  # metadata er hyggelig, men ikke kritisk
        logg(f"  (klarte ikke hente kodeliste: {feil})")
        return ""

    valgt = ""
    logg("\n  TILGJENGELIGE KODER I TABELL " + C.TABELL)
    dim = meta.get("dimension", {})
    for navn in meta.get("id", []):
        if navn == "Tid":
            continue
        kat = dim.get(navn, {}).get("category", {})
        koder = list(kat.get("index", {}))
        etiketter = kat.get("label", {})
        logg(f"    {navn}:")
        for k in koder:
            logg(f"      {k:<10} {etiketter.get(k, '')}")
        if navn == "Arbeidstype":
            valgt = str(etiketter.get(C.ARBEIDSTYPE_KODE, ""))
    logg("")
    if valgt:
        logg(f"  Valgt serie: {C.ARBEIDSTYPE_KODE} = {valgt}")
    else:
        logg(f"  ADVARSEL: fant ikke koden {C.ARBEIDSTYPE_KODE} i kodelisten")
    return valgt


def hent_indeks() -> pd.Series:
    """Henter indeksserien fra SSB som en pandas Series."""
    url = (
        f"{BASIS}/{C.TABELL}/data"
        f"?lang=no"
        f"&valueCodes[Arbeidstype]={C.ARBEIDSTYPE_KODE}"
        f"&valueCodes[ContentsCode]=Byggindeks"
        f"&valueCodes[Tid]=from({C.DATA_FRA})"
        f"&outputFormat=json-stat2"
    )
    logg(f"  Henter fra SSB: tabell {C.TABELL}, kode {C.ARBEIDSTYPE_KODE}")

    siste_feil = None
    for forsok in range(1, 4):
        try:
            svar = requests.get(url, timeout=120)
            svar.raise_for_status()
            rad = svar.json()
            break
        except Exception as feil:
            siste_feil = feil
            logg(f"  Forsøk {forsok} feilet ({feil}) - prøver igjen")
    else:
        raise RuntimeError(f"Fikk ikke tak i data fra SSB: {siste_feil}")

    perioder = list(rad["dimension"]["Tid"]["category"]["index"])
    verdier = rad["value"]
    if len(perioder) != len(verdier):
        raise RuntimeError("Uventet svarformat fra SSB - antall perioder stemmer ikke")

    par = [(p, v) for p, v in zip(perioder, verdier) if v is not None]
    stempler = pd.PeriodIndex([p.replace("M", "-") for p, _ in par], freq="M")
    serie = pd.Series([v for _, v in par], index=stempler, dtype=float).sort_index()

    logg(f"  Fikk {len(serie)} måneder: {serie.index[0]} til {serie.index[-1]}")
    logg(f"  Siste verdi: {serie.iloc[-1]:.1f}")
    return serie


def les_lokal_indeks(fil: str | None = None) -> pd.Series:
    """Leser siste lagrede øyeblikksbilde i stedet for å hente fra SSB."""
    fil = fil or os.path.join(DATA, "indeks_siste.csv")
    ramme = pd.read_csv(fil)
    serie = pd.Series(
        ramme["indeks"].values,
        index=pd.PeriodIndex(ramme["periode"], freq="M"),
        dtype=float,
    ).sort_index()
    logg(f"  Leste {fil}: {len(serie)} måneder, {serie.index[0]} til {serie.index[-1]}")
    return serie


def kontroller_serie(serie: pd.Series) -> None:
    """Stopper kjøringen hvis dataene åpenbart er feil."""
    if len(serie) < Modell.MIN_OBS + 12:
        raise RuntimeError(f"For kort serie: bare {len(serie)} måneder")
    if not (serie.values > 0).all():
        raise RuntimeError("Serien inneholder null eller negative indeksverdier")
    forventet = pd.period_range(serie.index[0], serie.index[-1], freq="M")
    if len(forventet) != len(serie):
        mangler = forventet.difference(serie.index)
        raise RuntimeError(f"Serien har hull: mangler {list(map(str, mangler[:6]))}")
    hopp = np.abs(np.diff(np.log(serie.values)))
    if hopp.max() > 0.10:
        verst = serie.index[1:][hopp.argmax()]
        raise RuntimeError(f"Urimelig månedshopp på over 10 % i {verst} - sjekk dataene")


def lagre_oyeblikksbilde(serie: pd.Series) -> None:
    """Lagrer rådata, navngitt etter siste datamåned, slik at enhver kjøring
    kan rekonstrueres senere."""
    os.makedirs(DATA, exist_ok=True)
    fil = os.path.join(DATA, "indeks_siste.csv")
    ramme = pd.DataFrame({"periode": serie.index.astype(str), "indeks": serie.values})
    ramme.to_csv(fil, index=False)

    arkiv = os.path.join(DATA, f"indeks_{serie.index[-1]}.csv")
    ramme.to_csv(arkiv, index=False)
    logg(f"  Lagret {fil} og {arkiv}")


# ===========================================================================
# 2. MODELL
# ===========================================================================


def anker_for(periode: pd.Period) -> float | None:
    """Ankeret slik det ville sett ut på et gitt tidspunkt.

    Inflasjonsmålet som gjaldt da, pluss antatt realvekst. Returnerer None
    før det fantes et inflasjonsmål, og hvis ankeret er slått av.
    """
    if not C.ANKER_USIKKERHET or C.ANKER_USIKKERHET <= 0:
        return None
    mal = None
    for fra, verdi in C.INFLASJONSMAL:
        if periode >= pd.Period(fra.replace("M", "-"), freq="M"):
            mal = verdi
    return None if mal is None else mal + C.REALVEKST_ANTATT


class Modell:
    """Månedlig logvekst = normalvekst + sesongmønster + treg avviksprosess.

    Normalveksten er snittet de siste VINDU_AAR årene, veid sammen med et
    anker (inflasjonsmål + antatt realvekst).

    Sesongmønsteret fanger at indeksen hopper i bestemte måneder, særlig når
    tariffoppgjøret slår inn i mai. Mønsteret får endre seg over tid.

    Avviksprosessen (en AR-prosess) fanger at når veksten først har lagt seg
    over eller under normalen, blir den værende der en stund. Det er denne
    som gjør at ettårsprognosen tar hensyn til hvor vi står i dag.
    """

    MIN_OBS = 120  # minst ti år før vi tror på et estimat

    def __init__(self, indeks: pd.Series, lags: int = 12):
        self.full = indeks
        self.lags = lags

        # De siste VINDU_AAR årene. Finnes det mindre enn det, brukes alt.
        # Samme regel gjelder alltid, også i tilbaketesten.
        vindu = indeks.iloc[-(12 * C.VINDU_AAR + 1) :]
        if len(vindu) < self.MIN_OBS:
            raise RuntimeError("For lite data til å estimere")
        self.vindu = vindu

        y = np.log(vindu.values)
        d = np.diff(y)  # månedlig logvekst
        mnd = vindu.index.month.values[1:]  # måned for hver d

        # -- normalvekst i dataene ---------------------------------------
        self.drift_data = float(d.mean())
        avvik = d - self.drift_data

        # -- sesong -------------------------------------------------------
        # For hver kalendermåned: et glidende, vektet snitt over årene.
        # sesong_hist er mønsteret slik det var i hver historiske måned,
        # self.sesong er det nyeste mønsteret, som brukes fremover.
        sesong_hist = np.zeros(len(d))
        sesong = np.zeros(12)
        for m in range(1, 13):
            hvor = np.where(mnd == m)[0]
            nr = np.arange(len(hvor))
            vekt = 0.5 ** (np.abs(nr[:, None] - nr[None, :]) / C.SESONG_HALVERINGSTID)
            glatt = (vekt @ avvik[hvor]) / vekt.sum(axis=1)
            sesong_hist[hvor] = glatt
            sesong[m - 1] = glatt[-1]
        sesong -= sesong.mean()  # summerer til null over et år
        self.sesong = sesong
        self.sesong_hist = sesong_hist

        # -- avvik og AR-prosess ------------------------------------------
        u = avvik - sesong_hist
        self.u = u

        X = np.column_stack([u[lags - i - 1 : len(u) - i - 1] for i in range(lags)])
        yv = u[lags:]

        # Ridge-straff. Uten den blir de 12 koeffisientene så støyete at en
        # enkelt uvanlig måned kan kaste ettårsprognosen flere tideler.
        XtX = X.T @ X
        lam = C.RIDGE * np.trace(XtX) / lags
        koef = np.linalg.solve(XtX + lam * np.eye(lags), X.T @ yv)
        self.phi = self._stabiliser(koef)
        resid = yv - X @ self.phi
        self.resid = resid - resid.mean()  # sjokkene skal ikke ha egen drift
        self.sist_u = u[-lags:][::-1].copy()  # nyeste først

        # -- usikkerhet om normalveksten, og sammenveiing med ankeret ------
        self.se_drift = self._se_drift(d)
        self.anker = anker_for(indeks.index[-1])
        if self.anker is None:
            self.drift = self.drift_data
            self.drift_sd = self.se_drift
            self.datavekt = 1.0
        else:
            pri_m = np.log1p(self.anker) / 12.0
            pri_sd = C.ANKER_USIKKERHET / 12.0
            p_d, p_p = 1.0 / self.se_drift**2, 1.0 / pri_sd**2
            self.drift = float((p_d * self.drift_data + p_p * pri_m) / (p_d + p_p))
            self.drift_sd = float(np.sqrt(1.0 / (p_d + p_p)))
            self.datavekt = float(p_d / (p_d + p_p))

    @staticmethod
    def _stabiliser(koef: np.ndarray) -> np.ndarray:
        """Krymper AR-koeffisientene hvis prosessen ikke er stabil.

        Uten dette kan modellen i sjeldne tilfeller produsere baner som
        eksploderer, og da blir 10-årsintervallet meningsløst.
        """
        k = koef.copy()
        for _ in range(60):
            rotter = np.roots(np.r_[1.0, -k])
            if np.all(np.abs(rotter) < 0.999):
                return k
            k *= 0.95
        return np.zeros_like(k)

    def _se_drift(self, d: np.ndarray, trekk: int = 4000) -> float:
        """Standardfeil for snittveksten, via blokk-bootstrap.

        Vanlig standardfeil undervurderer usikkerheten her, fordi månedene
        ikke er uavhengige. Blokker tar høyde for det.
        """
        rng = np.random.default_rng(C.TILFELDIG_FRO + 7)
        L = min(C.BLOKK_LENGDE, max(2, len(d) // 4))
        nb = int(np.ceil(len(d) / L))
        start = rng.integers(0, len(d) - L + 1, size=(trekk, nb))
        idx = (start[:, :, None] + np.arange(L)[None, None, :]).reshape(trekk, -1)[
            :, : len(d)
        ]
        return float(d[idx].mean(axis=1).std(ddof=1))

    # -- simulering -------------------------------------------------------

    def simuler(
        self, måneder: int, baner: int, fro: int, uten_driftusikkerhet: bool = False
    ) -> np.ndarray:
        """Returnerer en matrise (baner x måneder) med simulert logindeks."""
        rng = np.random.default_rng(fro)
        e = self.resid

        # blokk-bootstrap av sjokkene
        L = min(C.BLOKK_LENGDE, max(2, len(e) // 4))
        nb = int(np.ceil(måneder / L))
        start = rng.integers(0, len(e) - L + 1, size=(baner, nb))
        idx = (start[:, :, None] + np.arange(L)[None, None, :]).reshape(baner, -1)[
            :, :måneder
        ]
        sjokk = e[idx]

        # egen normalvekst per bane, fordi vi ikke vet den sanne
        drift = rng.normal(self.drift, self.drift_sd, size=(baner, 1))
        if uten_driftusikkerhet:
            drift[:] = self.drift

        siste_mnd = self.full.index[-1].month
        mnd = (siste_mnd - 1 + np.arange(1, måneder + 1)) % 12

        tilstand = np.tile(self.sist_u, (baner, 1))
        y0 = float(np.log(self.full.values[-1]))
        logg_bane = np.empty((baner, måneder))
        loper = np.full(baner, y0)

        for h in range(måneder):
            u = tilstand @ self.phi + sjokk[:, h]
            loper = loper + drift[:, 0] + self.sesong[mnd[h]] + u
            logg_bane[:, h] = loper
            tilstand = np.roll(tilstand, 1, axis=1)
            tilstand[:, 0] = u

        return logg_bane

    def aarlig_vekst(self, logg_bane: np.ndarray, aar: int) -> np.ndarray:
        """Gjennomsnittlig årlig vekst (CAGR) over `aar` år, for hver bane."""
        y0 = float(np.log(self.full.values[-1]))
        return np.expm1((logg_bane[:, 12 * aar - 1] - y0) / aar)

    def momentum(self, måneder: int) -> np.ndarray:
        """Hvor mye dagens avvik fra normalen løfter (eller senker) nivået,
        akkumulert måned for måned, hvis det ikke kommer nye sjokk."""
        tilstand = self.sist_u.copy()
        sum_ = 0.0
        ut = np.empty(måneder)
        for h in range(måneder):
            u = float(tilstand @ self.phi)
            sum_ += u
            ut[h] = sum_
            tilstand = np.roll(tilstand, 1)
            tilstand[0] = u
        return ut


def oppbygging(m: Modell, fordelinger: dict[int, np.ndarray]) -> dict[int, dict]:
    """Deler prognosen opp i trinn, slik at en leser kan følge regnestykket.

    Alle tall er gjennomsnittlig årlig vekst over horisonten.
    """
    mom = m.momentum(12 * max(fordelinger))
    ut = {}
    for aar, x in fordelinger.items():
        ut[aar] = {
            "data": float(np.expm1(12 * m.drift_data)),
            "anker": float(np.expm1(12 * m.drift)),
            "momentum": float(np.expm1(12 * m.drift + mom[12 * aar - 1] / aar)),
            "median": float(np.median(x)),
            "p10": float(np.percentile(x, 10)),
            "p90": float(np.percentile(x, 90)),
        }
    return ut


def folsomhet(indeks: pd.Series, baner: int | None = None) -> list[dict]:
    """Kjører modellen på nytt med andre rimelige valg i config.py.

    Svarer på spørsmålet «hvor mye henger anslaget på valgene våre?».
    """
    varianter = [
        ("Dagens oppsett", {}),
        ("Vindu 15 år", {"VINDU_AAR": 15}),
        ("Vindu 25 år", {"VINDU_AAR": 25}),
        ("Vindu 30 år", {"VINDU_AAR": 30}),
        ("Uten anker", {"ANKER_USIKKERHET": 0.0}),
        ("Sterkt anker (usikkerhet 0,3 pp)", {"ANKER_USIKKERHET": 0.003}),
        ("Realvekst 0,5 % i ankeret", {"REALVEKST_ANTATT": 0.005}),
        ("Realvekst 1,5 % i ankeret", {"REALVEKST_ANTATT": 0.015}),
        ("Tregere sesongtilpasning (halveringstid 10)", {"SESONG_HALVERINGSTID": 10}),
    ]
    maks = max(C.HORISONTER_AR)
    baner = baner or C.ANTALL_BANER
    ut = []
    for navn, endring in varianter:
        gammel = {k: getattr(C, k) for k in endring}
        try:
            for k, v in endring.items():
                setattr(C, k, v)
            m = Modell(indeks)
            b = m.simuler(12 * maks, baner, C.TILFELDIG_FRO)
        except Exception:
            continue
        finally:
            for k, v in gammel.items():
                setattr(C, k, v)
        rad = {"navn": navn, "drift": float(np.expm1(12 * m.drift))}
        for aar in C.HORISONTER_AR:
            p10, p50, p90 = np.percentile(m.aarlig_vekst(b, aar), [10, 50, 90])
            rad[aar] = (float(p10), float(p50), float(p90))
        ut.append(rad)
    return ut


# ===========================================================================
# 3. STATISTIKK
# ===========================================================================


def skjevhet(x: np.ndarray) -> float:
    z = (x - x.mean()) / x.std(ddof=0)
    return float((z**3).mean())


def kurtose(x: np.ndarray) -> float:
    z = (x - x.mean()) / x.std(ddof=0)
    return float((z**4).mean() - 3.0)


def beskriv_form(s: float) -> str:
    if s > 0.30:
        return "tydelig høyreskjev"
    if s > 0.10:
        return "svakt høyreskjev"
    if s < -0.30:
        return "tydelig venstreskjev"
    if s < -0.10:
        return "svakt venstreskjev"
    return "tilnærmet symmetrisk"


def oppsummer(fordelinger: dict[int, np.ndarray]) -> list[dict]:
    rader = []
    for aar, x in fordelinger.items():
        p = np.percentile(x, [5, 10, 25, 50, 75, 90, 95])
        s = skjevhet(x)
        rader.append(
            {
                "horisont_ar": aar,
                "median": float(p[3]),
                "gjennomsnitt": float(x.mean()),
                "p5": float(p[0]),
                "p10": float(p[1]),
                "p25": float(p[2]),
                "p75": float(p[4]),
                "p90": float(p[5]),
                "p95": float(p[6]),
                "bredde_80": float(p[5] - p[1]),
                "bredde_90": float(p[6] - p[0]),
                "skjevhet": s,
                "kurtose": kurtose(x),
                "form": beskriv_form(s),
                "sanns_over_null": float((x > 0).mean()),
            }
        )
    return rader


# ===========================================================================
# 4. GRAFER
# ===========================================================================


def _perioder_frem(sist: pd.Period, n: int) -> pd.PeriodIndex:
    return pd.period_range(sist + 1, periods=n, freq="M")


def graf_vifte_indeks(m: Modell, baner: np.ndarray, fil: str) -> None:
    hist = m.full[m.full.index >= m.full.index[-1] - 180]
    frem = _perioder_frem(m.full.index[-1], baner.shape[1])
    niva = np.exp(baner)

    fig, ax = plt.subplots(figsize=(9, 4.6))
    ax.plot(hist.index.to_timestamp(), hist.values, color=FARGE["historikk"], lw=1.6)

    x = frem.to_timestamp()
    for i, b in enumerate(sorted(C.BAND, reverse=True)):
        lo, hi = np.percentile(niva, [(100 - b) / 2, 100 - (100 - b) / 2], axis=0)
        ax.fill_between(x, lo, hi, color=FARGE["band"], alpha=0.16 + 0.14 * i, linewidth=0)
    ax.plot(x, np.percentile(niva, 50, axis=0), color=FARGE["median"], lw=1.8)

    ax.axvline(m.full.index[-1].to_timestamp(), color=FARGE["svak"], lw=0.8, ls=(0, (4, 3)))
    ax.set_ylabel("Indeks (2015 = 100)")
    ax.set_title("Indeksnivå med usikkerhetsvifte")
    ax.margins(x=0.01)
    fig.savefig(fil)
    plt.close(fig)


def graf_vifte_vekst(m: Modell, baner: np.ndarray, fil: str) -> None:
    """Tolvmånedersvekst, historisk og simulert."""
    y = np.log(m.full.values)
    hist_v = np.expm1(y[12:] - y[:-12])
    hist_i = m.full.index[12:]
    vis = hist_i >= m.full.index[-1] - 180

    y0 = np.log(m.full.values[-12:])
    utvidet = np.concatenate([np.tile(y0, (baner.shape[0], 1)), baner], axis=1)
    vekst = np.expm1(utvidet[:, 12:] - utvidet[:, :-12])
    frem = _perioder_frem(m.full.index[-1], baner.shape[1])

    fig, ax = plt.subplots(figsize=(9, 4.6))
    ax.axhline(0, color=FARGE["svak"], lw=0.8)
    ax.plot(hist_i[vis].to_timestamp(), hist_v[vis] * 100, color=FARGE["historikk"], lw=1.6)

    x = frem.to_timestamp()
    for i, b in enumerate(sorted(C.BAND, reverse=True)):
        lo, hi = np.percentile(vekst, [(100 - b) / 2, 100 - (100 - b) / 2], axis=0)
        ax.fill_between(
            x, lo * 100, hi * 100, color=FARGE["band"], alpha=0.16 + 0.14 * i, linewidth=0
        )
    ax.plot(x, np.percentile(vekst, 50, axis=0) * 100, color=FARGE["median"], lw=1.8)
    normal = np.expm1(12 * m.drift) * 100
    ax.axhline(normal, color=FARGE["anker"], lw=1.1, ls=(0, (5, 3)))
    ax.annotate(
        f"modellens normalvekst ({nf(normal)} %)",
        xy=(x[-1], normal),
        xytext=(-6, -14),
        textcoords="offset points",
        ha="right",
        fontsize=8.5,
        color=FARGE["anker"],
        bbox=dict(
            facecolor=FARGE["flate"],
            edgecolor="none",
            boxstyle="square,pad=0.2",
            alpha=0.85,
        ),
    )

    ax.axvline(m.full.index[-1].to_timestamp(), color=FARGE["svak"], lw=0.8, ls=(0, (4, 3)))
    ax.set_ylabel("Endring siste 12 md. (%)")
    ax.set_title("Tolvmånedersvekst med usikkerhetsvifte")
    ax.margins(x=0.01)
    fig.savefig(fil)
    plt.close(fig)


def graf_fordelinger(
    fordelinger: dict[int, np.ndarray], rader: list[dict], fil: str
) -> None:
    """Fordelingen av årlig vekst per horisont, med normalkurve til sammenligning.

    Den stiplede kurven er en normalfordeling med samme snitt og spredning.
    Ligger søylene systematisk til høyre for den, er utfallsrommet høyreskjevt:
    oppsiden er større enn nedsiden.
    """
    n = len(fordelinger)
    kol = 2
    rad = int(np.ceil(n / kol))
    fig, akser = plt.subplots(rad, kol, figsize=(9.2, 3.5 * rad))
    akser = np.atleast_1d(akser).ravel()

    for ax, (aar, x), info in zip(akser, fordelinger.items(), rader):
        xp = x * 100
        ax.hist(
            xp, bins=70, color=FARGE["band"], alpha=0.55, edgecolor="none", density=True
        )

        mu, sd = xp.mean(), xp.std()
        rutenett = np.linspace(xp.min(), xp.max(), 300)
        normal = np.exp(-0.5 * ((rutenett - mu) / sd) ** 2) / (sd * np.sqrt(2 * np.pi))
        ax.plot(rutenett, normal, color=FARGE["anker"], lw=1.2, ls=(0, (4, 2.5)))

        for q, stil in ((info["p10"], ":"), (info["median"], "-"), (info["p90"], ":")):
            ax.axvline(
                q * 100, color=FARGE["median"], lw=1.4 if stil == "-" else 1.0, ls=stil
            )

        ax.set_title(
            f"{aar} år   ·   {info['median']*100:.1f} %"
            f"   ({info['p10']*100:.1f} – {info['p90']*100:.1f})",
            fontsize=11,
        )
        ax.annotate(
            f"skjevhet {info['skjevhet']:+.2f} · {info['form']}",
            xy=(0.03, 0.95),
            xycoords="axes fraction",
            fontsize=8.5,
            color=FARGE["svak"],
            va="top",
            bbox=dict(
                facecolor=FARGE["flate"],
                edgecolor="none",
                boxstyle="square,pad=0.35",
                alpha=0.88,
            ),
        )
        ax.set_xlabel("Gjennomsnittlig årlig vekst (%)")
        ax.set_yticks([])

    for ax in akser[n:]:
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(fil)
    plt.close(fig)


def graf_bredde(rader: list[dict], m: Modell, fil: str) -> None:
    """Hvor bredt utfallsrommet er, horisont for horisont."""
    aar = [r["horisont_ar"] for r in rader]
    y = np.arange(len(aar))

    fig, ax = plt.subplots(figsize=(9, 0.62 * len(aar) + 1.5))
    for i, r in enumerate(rader):
        ax.plot(
            [r["p5"] * 100, r["p95"] * 100],
            [i, i],
            color=FARGE["band"],
            lw=6,
            solid_capstyle="butt",
            alpha=0.55,
        )
        ax.plot(
            [r["p10"] * 100, r["p90"] * 100],
            [i, i],
            color=FARGE["median"],
            lw=6,
            solid_capstyle="butt",
            alpha=0.85,
        )
        ax.plot(
            r["median"] * 100,
            i,
            "o",
            color=FARGE["flate"],
            markeredgecolor=FARGE["median"],
            markersize=7,
            markeredgewidth=1.6,
            zorder=5,
        )
        ax.annotate(
            f"{r['bredde_80']*100:.1f} pp",
            xy=(r["p95"] * 100, i),
            xytext=(10, 0),
            textcoords="offset points",
            va="center",
            fontsize=9,
            color=FARGE["svak"],
        )

    if m.anker is not None:
        ax.axvline(m.anker * 100, color=FARGE["anker"], lw=1.1, ls=(0, (5, 3)))
    ax.set_yticks(y, [f"{a} år" for a in aar])
    ax.invert_yaxis()
    ax.set_xlabel("Gjennomsnittlig årlig vekst (%)")
    ax.set_title("Utfallsrom per horisont  ·  mørkt felt = 80 %, lyst = 90 %")
    ax.grid(axis="y", visible=False)
    ax.margins(x=0.13, y=0.30)
    fig.savefig(fil)
    plt.close(fig)


def graf_oppbygging(trinn: dict[int, dict], fil: str) -> None:
    """Regnestykket bak medianen, trinn for trinn, ett panel per horisont."""
    aarene = list(trinn)
    kol = 2 if len(aarene) > 1 else 1
    rad = int(np.ceil(len(aarene) / kol))
    fig, akser = plt.subplots(rad, kol, figsize=(9.2, 2.9 * rad), sharex=True)
    akser = np.atleast_1d(akser).ravel()

    navn = [
        "Snitt i dataene",
        "+ anker",
        "+ dagens tempo",
        "= median",
    ]
    lo = min(t["p10"] for t in trinn.values()) * 100 - 0.3
    hi = max(t["p90"] for t in trinn.values()) * 100 + 0.9

    for ax, aar in zip(akser, aarene):
        t = trinn[aar]
        verdier = [t["data"], t["anker"], t["momentum"], t["median"]]
        for i, v in enumerate(verdier):
            v = v * 100
            if i > 0:
                forrige = verdier[i - 1] * 100
                ax.plot([forrige, forrige], [i - 1, i], color=FARGE["svak"], lw=0.8, ls=":")
                ax.plot(
                    [forrige, v],
                    [i, i],
                    color=FARGE["median"] if v >= forrige else FARGE["anker"],
                    lw=5,
                    solid_capstyle="butt",
                )
                tekst = f"{fortegn(v - forrige)}  →  {nf(v, 2)} %"
            else:
                tekst = f"{nf(v, 2)} %"
            ax.plot(
                v, i, "o", color=FARGE["blekk"], markersize=5.5,
                markeredgecolor=FARGE["flate"], markeredgewidth=1.2, zorder=5,
            )
            ax.annotate(
                tekst,
                xy=(max(v, verdier[i - 1] * 100 if i else v), i),
                xytext=(9, 0),
                textcoords="offset points",
                va="center",
                fontsize=9,
            )
        ax.plot(
            [t["p10"] * 100, t["p90"] * 100], [4, 4],
            color=FARGE["band"], lw=7, solid_capstyle="butt", alpha=0.6,
        )
        ax.plot(
            t["median"] * 100, 4, "o", color=FARGE["flate"],
            markeredgecolor=FARGE["median"], markersize=7, markeredgewidth=1.6, zorder=5,
        )
        ax.annotate(
            f"{nf(t['p10'] * 100)} – {nf(t['p90'] * 100)} %",
            xy=(t["p90"] * 100, 4), xytext=(9, 0), textcoords="offset points",
            va="center", fontsize=9, color=FARGE["svak"],
        )
        ax.set_yticks(range(5), navn + ["80 %-intervall"], fontsize=9)
        ax.tick_params(axis="y", colors=FARGE["blekk"])
        ax.set_ylim(4.6, -0.6)
        ax.set_xlim(lo, hi)
        ax.grid(axis="y", visible=False)
        ax.set_title("Neste år" if aar == 1 else f"Neste {aar} år", fontsize=11, loc="left")

    for ax in akser[len(aarene) :]:
        ax.axis("off")
    for ax in akser[-kol:]:
        ax.set_xlabel("Gjennomsnittlig årlig vekst (%)")
    fig.tight_layout()
    fig.savefig(fil)
    plt.close(fig)


def graf_sesong(m: Modell, fil: str) -> None:
    """Sesongmønsteret i dag mot for ti år siden."""
    mnd = m.vindu.index.month.values[1:]
    aar = m.vindu.index.year.values[1:]
    siste_aar = aar[-1]
    for_aar = siste_aar - 10

    def monster(a: int) -> np.ndarray | None:
        ut = np.full(12, np.nan)
        for k in range(1, 13):
            treff = np.where((mnd == k) & (aar <= a))[0]
            if len(treff):
                ut[k - 1] = m.sesong_hist[treff[-1]]
        return None if np.isnan(ut).any() else (ut - ut.mean())

    for_ = monster(for_aar)
    navn = ["jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des"]
    x = np.arange(12)
    normal = m.drift * 100

    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.axhline(0, color=FARGE["svak"], lw=0.8)
    ax.bar(x, (m.sesong * 100) + normal, width=0.6, color=FARGE["band"], label="mønsteret i dag")
    if for_ is not None:
        ax.plot(
            x, for_ * 100 + normal, "o", color=FARGE["anker"], markersize=6,
            label=f"mønsteret i {for_aar}",
        )
    ax.axhline(normal, color=FARGE["median"], lw=1.1, ls=(0, (5, 3)))
    ax.annotate(
        "jevn vekst hele året", xy=(11.4, normal), xytext=(0, 5),
        textcoords="offset points", ha="right", fontsize=8.5, color=FARGE["median"],
    )
    ax.set_xticks(x, navn)
    ax.set_ylabel("Normal vekst i måneden (%)")
    ax.set_title("Når på året kommer kostnadsveksten?")
    ax.grid(axis="x", visible=False)
    ax.legend(frameon=False, fontsize=9, loc="upper right")
    fig.savefig(fil)
    plt.close(fig)


def graf_tilbaketest(tb: pd.DataFrame, fil: str) -> None:
    fig, akser = plt.subplots(1, 2, figsize=(9.2, 3.9))

    aar = sorted(tb["horisont_ar"].unique())
    dekning = [100 * tb.loc[tb.horisont_ar == a, "innenfor80"].mean() for a in aar]
    akser[0].bar([str(a) for a in aar], dekning, color=FARGE["band"], width=0.55)
    akser[0].axhline(80, color=FARGE["anker"], lw=1.2, ls=(0, (5, 3)))
    for i, d in enumerate(dekning):
        akser[0].annotate(f"{d:.0f} %", xy=(i, d), xytext=(0, 4),
                          textcoords="offset points", ha="center", fontsize=9)
    akser[0].set_ylim(0, 108)
    akser[0].set_ylabel("Andel innenfor 80 %-intervallet")
    akser[0].set_xlabel("Horisont (år)")
    akser[0].set_title("Treffer intervallene?")

    bredde = 0.27
    p = np.arange(len(aar))
    serier = [
        ("feil_modell", "modellen", FARGE["median"]),
        ("feil_snitt", "bare snittet i vinduet", FARGE["band"]),
        ("feil_naiv", "siste års vekst fortsetter", FARGE["svak"]),
    ]
    for j, (kol, navn, farge) in enumerate(serier):
        v = [tb.loc[tb.horisont_ar == a, kol].abs().mean() * 100 for a in aar]
        akser[1].bar(p + (j - 1) * bredde, v, bredde, label=navn, color=farge)
    akser[1].set_xticks(p, [str(a) for a in aar])
    akser[1].set_ylabel("Gj.sn. bom (prosentpoeng per år)")
    akser[1].set_xlabel("Horisont (år)")
    akser[1].set_title("Bedre enn enkle regler?")
    akser[1].legend(frameon=False, fontsize=8.5)

    fig.tight_layout()
    fig.savefig(fil)
    plt.close(fig)


# ===========================================================================
# 5. TILBAKETEST
# ===========================================================================


def tilbaketest_baner(indeks: pd.Series) -> list[dict]:
    """Lager historiske prognoser som kan tegnes mot fasiten.

    For noen få utvalgte tidspunkter i fortiden stiller vi modellen opp med
    bind for øynene: den får bare se data til og med det tidspunktet, og lager
    en prognose fremover. Så legger vi den faktiske utviklingen oppå.

    Dette er den mest direkte måten å se om modellen er til å stole på. Den
    krever ingen statistikk for å leses.
    """
    aar = C.TILBAKETEST_VIS_AAR
    sist = indeks.index[-1]
    tidligst = indeks.index[0] + Modell.MIN_OBS + 12

    # Nyeste startpunkt der vi rekker å se hele horisonten, så bakover
    # med jevne mellomrom.
    nyeste = sist - 12 * aar
    punkter = [nyeste - 12 * aar * k for k in range(C.TILBAKETEST_VIS_ANTALL)]
    punkter = sorted(p for p in punkter if p >= tidligst)

    ut = []
    for n, punkt in enumerate(punkter):
        historikk = indeks[indeks.index <= punkt]
        try:
            m = Modell(historikk)
        except Exception:
            continue

        baner = np.exp(m.simuler(12 * aar, C.TILBAKETEST_BANER, C.TILFELDIG_FRO + 500 + n))
        frem = pd.period_range(punkt + 1, periods=12 * aar, freq="M")

        ut.append(
            {
                "punkt": punkt,
                "frem": frem,
                "median": np.percentile(baner, 50, axis=0),
                "band": {
                    b: np.percentile(baner, [(100 - b) / 2, 100 - (100 - b) / 2], axis=0)
                    for b in C.BAND
                },
            }
        )
    return ut


def graf_tilbaketest_bane(indeks: pd.Series, serier: list[dict], fil: str) -> None:
    """Faktisk indeks mot det modellen ville sagt, med usikkerhetsvifte."""
    if not serier:
        return

    n = len(serier)
    kol = 2 if n > 1 else 1
    rad = int(np.ceil(n / kol))
    fig, akser = plt.subplots(rad, kol, figsize=(9.2, 3.3 * rad))
    akser = np.atleast_1d(akser).ravel()

    for ax, d in zip(akser, serier):
        punkt, frem = d["punkt"], d["frem"]

        # litt historikk foran, hele prognoseperioden bak
        vis = indeks[(indeks.index >= punkt - 36) & (indeks.index <= frem[-1])]
        x = frem.to_timestamp()

        for i, b in enumerate(sorted(C.BAND, reverse=True)):
            lo, hi = d["band"][b]
            ax.fill_between(
                x, lo, hi, color=FARGE["band"], alpha=0.16 + 0.14 * i, linewidth=0
            )
        ax.plot(x, d["median"], color=FARGE["median"], lw=1.6, label="modellens median")
        ax.plot(
            vis.index.to_timestamp(),
            vis.values,
            color=FARGE["blekk"],
            lw=1.9,
            label="faktisk",
        )
        ax.axvline(punkt.to_timestamp(), color=FARGE["svak"], lw=0.9, ls=(0, (4, 3)))

        # traff den?
        mal = frem[-1]
        if mal in indeks.index:
            lo80, hi80 = d["band"][80]
            innenfor = lo80[-1] <= indeks[mal] <= hi80[-1]
            ax.plot(
                mal.to_timestamp(),
                indeks[mal],
                "o",
                color=FARGE["flate"],
                markeredgecolor=FARGE["blekk"] if innenfor else FARGE["anker"],
                markersize=7,
                markeredgewidth=1.8,
                zorder=6,
            )

        ax.set_title(
            f"Prognose laget {str(punkt).replace('-', 'M')}", fontsize=10.5, loc="left"
        )
        ax.margins(x=0.01)
        ax.tick_params(labelsize=8.5)

    akser[0].legend(frameon=False, fontsize=8.5, loc="upper left")
    for ax in akser[n:]:
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(fil)
    plt.close(fig)


def graf_tilbaketest_feil(tb: pd.DataFrame, fil: str) -> None:
    """Bommen over tid, så du ser om feilene klumper seg i enkeltepisoder."""
    if tb.empty:
        return
    t = tb.copy()
    t["dato"] = pd.PeriodIndex(t["startpunkt"], freq="M").to_timestamp()

    aarene = sorted(t["horisont_ar"].unique())
    fig, ax = plt.subplots(figsize=(9, 4.0))
    ax.axhline(0, color=FARGE["blekk"], lw=0.9)

    nyanser = ["#9CC0C9", "#6B9AA6", "#2E7080", "#0F4C5C"]
    for i, a in enumerate(aarene):
        del_ = t[t.horisont_ar == a].sort_values("dato")
        ax.plot(
            del_["dato"],
            del_["feil_modell"] * 100,
            lw=1.5,
            color=nyanser[i % len(nyanser)],
            label=f"{a} år",
        )

    ax.set_ylabel("Modellen minus fasit (pp per år)")
    ax.set_title("Når bommet modellen, og hvor mye?")
    ax.legend(frameon=False, fontsize=9, ncol=len(aarene))
    ax.margins(x=0.01)
    fig.savefig(fil)
    plt.close(fig)


def tilbaketest(indeks: pd.Series) -> pd.DataFrame:
    """Later som modellen sto på ulike tidspunkter i fortiden og sjekker fasiten.

    På hvert startpunkt får modellen bare se data som fantes da, og den
    bruker nøyaktig samme regler som i dag: samme vindu, og det ankeret som
    gjaldt på tidspunktet.
    """
    start = pd.Period(C.TILBAKETEST_FRA.replace("M", "-"), freq="M")

    rader = []
    punkter = [p for p in indeks.index if p >= start][:: C.TILBAKETEST_STEG]
    logg(f"  {len(punkter)} startpunkter å teste")

    for n, punkt in enumerate(punkter):
        historikk = indeks[indeks.index <= punkt]
        try:
            m = Modell(historikk)
        except Exception:
            continue

        maks = max(C.HORISONTER_AR)
        baner = m.simuler(12 * maks, C.TILBAKETEST_BANER, C.TILFELDIG_FRO + n)

        siste12 = np.expm1(np.log(historikk.values[-1]) - np.log(historikk.values[-13]))
        snitt = np.expm1(12 * m.drift_data)

        for aar in C.HORISONTER_AR:
            mal = punkt + 12 * aar
            if mal not in indeks.index:
                continue
            faktisk = np.expm1((np.log(indeks[mal]) - np.log(historikk.values[-1])) / aar)
            f = m.aarlig_vekst(baner, aar)
            p10, p50, p90 = np.percentile(f, [10, 50, 90])
            rader.append(
                {
                    "startpunkt": str(punkt),
                    "horisont_ar": aar,
                    "faktisk": faktisk,
                    "modell": p50,
                    "p10": p10,
                    "p90": p90,
                    "naiv": siste12,
                    "snitt": snitt,
                    "feil_modell": p50 - faktisk,
                    "feil_naiv": siste12 - faktisk,
                    "feil_snitt": snitt - faktisk,
                    "innenfor80": bool(p10 <= faktisk <= p90),
                }
            )

        if (n + 1) % max(1, len(punkter) // 8) == 0:
            logg(f"    {n + 1}/{len(punkter)} ...")

    return pd.DataFrame(rader)


def tilbaketest_perioder(tb: pd.DataFrame) -> list[dict]:
    """Dekning og skjevhet delt på når prognosen ble laget.

    Ett samlet dekningstall kan skjule at intervallene var for brede i én
    periode og for smale i en annen. Denne tabellen viser det.
    """
    aar = tb["startpunkt"].str[:4].astype(int)
    forste, siste = int(aar.min()), int(aar.max())
    grenser = list(range(forste, siste + 1, 10))
    ut = []
    for g in grenser:
        til = min(g + 9, siste)
        if siste - g < 4 and ut:  # for kort siste bolk: slå sammen med forrige
            ut[-1]["til"] = siste
            ut[-1]["maske"] = ut[-1]["maske"] | ((aar >= g) & (aar <= siste))
            continue
        ut.append({"fra": g, "til": til, "maske": (aar >= g) & (aar <= til)})
    for bolk in ut:
        del_ = tb[bolk.pop("maske")]
        bolk["horisonter"] = {
            int(a): {
                "n": int(len(d)),
                "dekning": float(d["innenfor80"].mean()),
                "skjevhet": float(d["feil_modell"].mean()),
            }
            for a, d in del_.groupby("horisont_ar")
        }
    return ut


# ===========================================================================
# 6. HISTORIKK
# ===========================================================================


def oppdater_historikk(indeks: pd.Series, rader: list[dict]) -> None:
    """Fører logg over hva modellen har sagt, én rad per datamåned og horisont.

    Kjøres modellen flere ganger på samme data, erstatter siste kjøring den
    forrige. Kolonnen modellversjon gjør at anslag fra ulike metoder ikke
    forveksles.
    """
    os.makedirs(DATA, exist_ok=True)
    fil = os.path.join(DATA, "resultater_historikk.csv")
    tidligere = pd.read_csv(fil, dtype={"modellversjon": str}) if os.path.exists(fil) else None
    if tidligere is not None and "modellversjon" not in tidligere.columns:
        tidligere["modellversjon"] = "1"

    ny = pd.DataFrame(
        [
            {
                "kjørt": datetime.now().strftime("%Y-%m-%d"),
                "siste_periode": str(indeks.index[-1]),
                "siste_indeks": float(indeks.iloc[-1]),
                "horisont_ar": r["horisont_ar"],
                "median": r["median"],
                "p10": r["p10"],
                "p90": r["p90"],
                "modellversjon": MODELLVERSJON,
            }
            for r in rader
        ]
    )

    samlet = ny if tidligere is None else pd.concat([tidligere, ny], ignore_index=True)
    samlet["modellversjon"] = samlet["modellversjon"].fillna("1").astype(str)
    samlet = samlet.drop_duplicates(
        subset=["siste_periode", "horisont_ar", "modellversjon"], keep="last"
    )
    samlet.to_csv(fil, index=False)


def endring_siden_forrige_maned(indeks: pd.Series, rader: list[dict]) -> dict[int, float]:
    """Hvor mye flyttet den nyeste måneden anslagene?

    Vi kjører dagens modell på nytt uten den siste observasjonen og ser på
    forskjellen. Da måler tallet bare effekten av nye data, ikke av at
    metoden eller innstillingene er endret siden sist.
    """
    try:
        m = Modell(indeks.iloc[:-1])
    except Exception:
        return {}
    baner = m.simuler(12 * max(C.HORISONTER_AR), C.ANTALL_BANER, C.TILFELDIG_FRO)
    return {
        r["horisont_ar"]: r["median"] - float(np.median(m.aarlig_vekst(baner, r["horisont_ar"])))
        for r in rader
    }


# ===========================================================================
# 7. RAPPORT
# ===========================================================================


STIL = """
:root{
  --blekk:#17232B; --dyp:#0F4C5C; --band:#6B9AA6; --bronse:#8A6A2F;
  --svak:#6C7A80; --flate:#FFFFFF; --papir:#F2F3F1; --strek:#DFE3E0;
}
*{box-sizing:border-box}
body{margin:0;background:var(--papir);color:var(--blekk);
     font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
     line-height:1.55;-webkit-font-smoothing:antialiased}
.ark{max-width:900px;margin:0 auto;padding:56px 26px 90px}
h1{font-family:Georgia,"Iowan Old Style",serif;font-weight:400;
   font-size:clamp(28px,4.2vw,40px);line-height:1.15;margin:0 0 6px}
.undertittel{color:var(--svak);font-size:16px;margin:0 0 6px}
.stempel{color:var(--svak);font-size:13.5px;margin:0}
h2{font-family:Georgia,serif;font-weight:400;font-size:21px;
   margin:54px 0 10px;padding-bottom:8px;border-bottom:1px solid var(--strek)}
p{max-width:68ch}
.tall{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));
      gap:1px;background:var(--strek);border:1px solid var(--strek);
      margin:34px 0 10px}
.rute{background:var(--flate);padding:20px 18px 18px}
.horisont{font-size:13px;color:var(--svak);margin-bottom:10px}
.stor{font-family:Georgia,serif;font-size:38px;line-height:1;color:var(--dyp)}
.stor span{font-size:19px;color:var(--svak)}
.spenn{font-size:13.5px;color:var(--svak);margin-top:9px}
.bar{position:relative;height:5px;background:#E7EBE9;margin-top:11px}
.bar i{position:absolute;top:0;height:5px;background:var(--band);display:block}
.bar b{position:absolute;top:-3px;width:2px;height:11px;background:var(--dyp)}
.delta{font-size:12.5px;margin-top:10px;color:var(--svak)}
.opp{color:#8A3A1F}.ned{color:#2F6B4F}
figure{margin:26px 0 0}
figure img{width:100%;display:block;background:var(--flate);border:1px solid var(--strek)}
figcaption{font-size:13.5px;color:var(--svak);margin-top:11px;max-width:68ch}
table{border-collapse:collapse;width:100%;font-size:14px;margin-top:20px;background:var(--flate)}
th,td{padding:9px 11px;border-bottom:1px solid var(--strek);text-align:right}
th:first-child,td:first-child{text-align:left}
th{font-weight:600;font-size:12.5px;color:var(--svak)}
.merk{background:var(--flate);border-left:3px solid var(--bronse);
      padding:15px 18px;margin:26px 0;font-size:14.5px}
h3{font-family:Georgia,serif;font-weight:400;font-size:17.5px;margin:34px 0 6px}
.ingress{font-size:17px;max-width:66ch;margin:26px 0 0}
.trinn{max-width:68ch;padding-left:22px}
.trinn li{margin-bottom:12px}
.n{color:var(--svak);font-size:12.5px}
.rulle{overflow-x:auto}
code{font-size:.92em;background:#E7EBE9;padding:1px 5px}
footer{margin-top:66px;padding-top:20px;border-top:1px solid var(--strek);
       color:var(--svak);font-size:13px}
a{color:var(--dyp)}
@media print{body{background:#fff}.ark{padding:0}figure{break-inside:avoid}}
"""


def nf(x: float, d: int = 1) -> str:
    """Norsk tallformat: komma som desimalskilletegn, ekte minustegn."""
    t = f"{abs(x):.{d}f}".replace(".", ",")
    return ("−" + t) if x < 0 and float(f"{abs(x):.{d}f}") != 0 else t


def fortegn(x: float, d: int = 2) -> str:
    """Som nf(), men alltid med fortegn foran."""
    return ("+" if x >= 0 else "−") + f"{abs(x):.{d}f}".replace(".", ",")


def _mnd(p: pd.Period) -> str:
    return str(p).replace("-", "M")


def lag_rapport(
    indeks, m, rader, trinn, kilder, endringer, tb, perioder, sens, serienavn, lokal, fil
):
    idag = datetime.now().strftime("%d.%m.%Y")
    siste = _mnd(indeks.index[-1])
    v12 = np.expm1(np.log(indeks.values[-1]) - np.log(indeks.values[-13])) * 100
    hor = [r["horisont_ar"] for r in rader]
    kort, lang = min(hor), max(hor)
    pst = lambda x, d=2: nf(x * 100, d)  # noqa: E731

    # -- rutene øverst ----------------------------------------------------
    ruter = []
    for r in rader:
        aar = r["horisont_ar"]
        lo, hi, md = r["p10"] * 100, r["p90"] * 100, r["median"] * 100
        gulv, tak = min(lo, 0) - 0.6, hi + 0.6
        bredde = max(tak - gulv, 0.1)
        v = 100 * (lo - gulv) / bredde
        b = 100 * (hi - lo) / bredde
        pos = 100 * (md - gulv) / bredde

        d = endringer.get(aar)
        if d is None:
            dhtml = ""
        elif abs(d) < 0.00005:
            dhtml = '<div class="delta">siste måned endret ikke anslaget</div>'
        else:
            k = "opp" if d > 0 else "ned"
            dhtml = (
                f'<div class="delta">effekt av siste måned '
                f'<span class="{k}">{fortegn(d * 100)} pp</span></div>'
            )

        ruter.append(f"""
      <div class="rute">
        <div class="horisont">{"Neste år" if aar == 1 else f"Neste {aar} år"}</div>
        <div class="stor">{nf(md)}<span> %</span></div>
        <div class="spenn">80 %: {nf(lo)} – {nf(hi)} %</div>
        <div class="bar"><i style="left:{v:.1f}%;width:{b:.1f}%"></i>
                         <b style="left:{pos:.1f}%"></b></div>
        {dhtml}
      </div>""")

    # -- kort fortalt -----------------------------------------------------
    tk, tl = trinn[kort], trinn[lang]
    normal = tk["anker"] * 100
    over = v12 - normal
    mom_k = (tk["momentum"] - tk["anker"]) * 100
    kort_fortalt = (
        f"Modellen venter at byggekostnadene for boligblokk stiger "
        f"<strong>{nf(tk['median'] * 100)} %</strong> det neste året, og "
        f"<strong>{nf(tl['median'] * 100)} %</strong> i året i snitt de neste {lang} årene. "
        f"Utgangspunktet er snittveksten de siste {C.VINDU_AAR} årene, {pst(tk['data'])} %. "
        f"De siste tolv månedene har veksten vært {nf(v12)} %, "
        f"{nf(abs(over))} prosentpoeng {'over' if over >= 0 else 'under'} det modellen "
        f"regner som normalt. "
    )
    if abs(mom_k) >= 0.05:
        kort_fortalt += (
            f"Noe av det avviket ventes å henge igjen, og det "
            f"{'løfter' if mom_k > 0 else 'senker'} ettårsanslaget med "
            f"{nf(abs(mom_k), 2)} prosentpoeng."
        )
    else:
        kort_fortalt += "Modellen venter ikke at avviket henger igjen i særlig grad."

    # -- trinnene ---------------------------------------------------------
    if m.anker is None:
        anker_tekst = (
            "Ankeret er slått av i innstillingene, så modellen bruker snittet i "
            "dataene direkte."
        )
        anker_rad = "<tr><td>Anker</td><td>slått av</td></tr>"
    else:
        anker_tekst = (
            f"Snittet veies sammen med et forhåndssyn på {pst(m.anker, 1)} %: "
            f"inflasjonsmålet på {pst(m.anker - C.REALVEKST_ANTATT, 1)} % pluss antatt "
            f"realvekst i byggekostnader på {pst(C.REALVEKST_ANTATT, 1)} %. "
            f"Snittet i dataene er beregnet med en usikkerhet på "
            f"±{nf(m.se_drift * 1200, 2)} prosentpoeng, og ankeret er satt med "
            f"±{pst(C.ANKER_USIKKERHET, 2)}. Det mest presise tallet får mest vekt: "
            f"{m.datavekt * 100:.0f} % på dataene og {100 - m.datavekt * 100:.0f} % på "
            f"ankeret. Resultatet, {pst(tk['anker'])} %, er det modellen regner som normal "
            f"vekst. Vekten er den samme på alle horisonter."
        )
        anker_rad = (
            f"<tr><td>Anker</td><td>{pst(m.anker)} % &plusmn; "
            f"{pst(C.ANKER_USIKKERHET)}</td></tr>"
        )

    skjev_k = (tk["median"] - tk["momentum"]) * 100
    trinn_tekst = f"""
  <ol class="trinn">
  <li><strong>Snittet i dataene.</strong> Fra {_mnd(m.vindu.index[0])} til {siste} steg
      indeksen i snitt {pst(tk['data'])} % i året. Modellen antar at de neste årene
      ligner på de siste {C.VINDU_AAR}. Dette er den viktigste forutsetningen i hele
      modellen, og det er et valg, ikke noe modellen har regnet seg frem til.</li>
  <li><strong>Ankeret.</strong> {anker_tekst}</li>
  <li><strong>Dagens tempo.</strong> De siste tolv månedene steg indeksen {nf(v12)} %,
      altså {nf(abs(over))} prosentpoeng {'over' if over >= 0 else 'under'} normalen.
      Historisk har slike avvik hengt igjen en stund før de dør ut. Modellen
      {'legger derfor til' if mom_k >= 0 else 'trekker derfor fra'}
      {nf(abs(mom_k), 2)} prosentpoeng på ettårsanslaget. Fordelt over {lang} år blir
      effekten {nf(abs((tl['momentum'] - tl['anker']) * 100), 2)} prosentpoeng per år.</li>
  <li><strong>Fra regnestykke til median.</strong> Til slutt simulerer modellen
      {C.ANTALL_BANER:,} mulige fremtider ved å trekke sammenhengende biter av de
      historiske avvikene. Historien inneholder flere store hopp oppover enn nedover
      (2021–22 er det tydeligste). Derfor er noen få simulerte fremtider svært
      dyre, mens flertallet havner litt under regnestykket i trinn 3. Medianen, det
      midterste utfallet, ligger {nf(abs(skjev_k), 2)} prosentpoeng
      {'under' if skjev_k < 0 else 'over'} for ettårsanslaget.</li>
  </ol>""".replace(",", " ", 0)

    trinn_tab = "".join(
        f"<tr><td>{'Neste år' if a == 1 else f'Neste {a} år'}</td>"
        f"<td>{pst(t['data'])}</td>"
        f"<td>{fortegn((t['anker'] - t['data']) * 100)}</td>"
        f"<td>{fortegn((t['momentum'] - t['anker']) * 100)}</td>"
        f"<td>{fortegn((t['median'] - t['momentum']) * 100)}</td>"
        f"<td><strong>{pst(t['median'])}</strong></td></tr>"
        for a, t in trinn.items()
    )

    kilde_tab = "".join(
        f"<tr><td>{a} år</td><td>{nf(k['total'] * 100, 2)}</td>"
        f"<td>{nf(k['sjokk'] * 100, 2)}</td><td>{nf(k['drift'] * 100, 2)}</td></tr>"
        for a, k in kilder.items()
    )

    trad = "".join(
        f"<tr><td>{r['horisont_ar']} år</td>"
        f"<td>{nf(r['median'] * 100, 2)}</td><td>{nf(r['gjennomsnitt'] * 100, 2)}</td>"
        f"<td>{nf(r['p5'] * 100, 2)}</td><td>{nf(r['p10'] * 100, 2)}</td>"
        f"<td>{nf(r['p90'] * 100, 2)}</td><td>{nf(r['p95'] * 100, 2)}</td>"
        f"<td>{nf(r['bredde_80'] * 100, 2)}</td>"
        f"<td>{fortegn(r['skjevhet'])}</td><td>{r['form']}</td></tr>"
        for r in rader
    )

    # -- tilbaketest ------------------------------------------------------
    if tb is not None and not tb.empty:
        aar_tb = sorted(tb["horisont_ar"].unique())
        forste, sist_start = tb["startpunkt"].min(), tb["startpunkt"].max()

        def rad_h(a):
            d = tb[tb.horisont_ar == a]
            uavh = max(1, int((pd.Period(d.startpunkt.max(), "M")
                               - pd.Period(d.startpunkt.min(), "M")).n / 12 / a))
            return (
                f"<tr><td>{a} år</td><td>{100 * d.innenfor80.mean():.0f} %</td>"
                f"<td>{nf((d.p90 - d.p10).mean() * 100)}</td>"
                f"<td><strong>{nf(d.feil_modell.abs().mean() * 100, 2)}</strong></td>"
                f"<td>{nf(d.feil_snitt.abs().mean() * 100, 2)}</td>"
                f"<td>{nf(d.feil_naiv.abs().mean() * 100, 2)}</td>"
                f"<td>{fortegn(d.feil_modell.mean() * 100)}</td>"
                f"<td>{len(d)}</td><td>ca. {uavh}</td></tr>"
            )

        tb_tab = "".join(rad_h(a) for a in aar_tb)

        def celle(x):
            if x is None:
                return "<td>–</td>"
            return f"<td>{100 * x['dekning']:.0f} % <span class=\"n\">({fortegn(x['skjevhet'] * 100, 1)})</span></td>"

        per_tab = "".join(
            f"<tr><td>{b['fra']}–{b['til']}</td>"
            + "".join(celle(b["horisonter"].get(a)) for a in aar_tb)
            + "</tr>"
            for b in perioder
        )
        per_hode = "".join(f"<th>{a} år</th>" for a in aar_tb)

        tb_html = f"""
  <h2>Ville modellen ha truffet?</h2>
  <p>Den beste prøven på en prognosemodell er å stille den opp i fortiden med
     bind for øynene. I panelene under får modellen bare se data til og med den
     stiplede streken, og lager så en prognose {C.TILBAKETEST_VIS_AAR} år frem.
     Den mørke linjen er hva som faktisk skjedde.</p>
  <figure><img src="tilbaketest_bane.png" alt="Historiske prognoser mot fasit">
  <figcaption>Mørk linje er faktisk indeks, blå linje er modellens median, og de
  blå feltene er usikkerhetsspennet. Ringen markerer hvor fasiten landet etter
  {C.TILBAKETEST_VIS_AAR} år: mørk ring betyr innenfor 80 %-intervallet, brun
  ring betyr utenfor.</figcaption></figure>

  <h3>Tallene bak</h3>
  <p>Samme prøve er gjort fra {tb['startpunkt'].nunique()} startpunkter, ett for hver
     måned fra {forste.replace('-', 'M')} til {sist_start.replace('-', 'M')}. På hvert
     startpunkt bruker modellen de samme reglene som i dag: de siste
     {C.VINDU_AAR} årene med data (eller alt som fantes, de første årene), og det
     inflasjonsmålet som gjaldt da.</p>
  <table>
  <thead><tr><th>Horisont</th><th>Innenfor 80 %</th><th>Bredde 80 %</th>
  <th>Bom, modellen</th><th>Bom, bare snittet</th><th>Bom, siste år fortsetter</th>
  <th>Skjevhet</th><th>Prøver</th><th>Uavhengige</th></tr></thead>
  <tbody>{tb_tab}</tbody></table>
  <p class="stempel">«Innenfor 80 %» bør ligge nær 80. Bom er gjennomsnittlig
  avstand mellom anslag og fasit, i prosentpoeng per år. Skjevhet er modellens
  anslag minus fasit i snitt: pluss betyr at modellen har ligget for høyt.
  «Bare snittet» er trinn 1 alene, «siste år fortsetter» er den enkleste
  tommelfingerregelen.</p>
  <div class="merk">
  Les tallene med måte. Startpunktene ligger én måned fra hverandre, så
  naboprøver deler nesten hele perioden. Kolonnen «Uavhengige» viser hvor mange
  ikke-overlappende perioder testen egentlig inneholder. For {lang}-årsanslaget er
  det en håndfull. Da kan dekningen havne langt fra 80 uten at noe er galt, og
  den kan ligge nær 80 ved en tilfeldighet.
  </div>

  <h3>Holder det i alle perioder?</h3>
  <p>Ett samlet dekningstall kan skjule at intervallene var for brede i én periode
     og for smale i en annen. Tabellen deler derfor testen etter når prognosen ble
     laget.</p>
  <table>
  <thead><tr><th>Prognose laget</th>{per_hode}</tr></thead>
  <tbody>{per_tab}</tbody></table>
  <p class="stempel">Andel innenfor 80 %-intervallet. Tallet i parentes er
  skjevheten i prosentpoeng per år. Store utslag i enkeltperioder er ventet:
  hver rute er i praksis én eller to konjunkturepisoder.</p>
  <figure><img src="tilbaketest_feil.png" alt="Bommen over tid">
  <figcaption>Over null betyr at modellen anslo for høyt. Klumper feilene seg
  rundt enkeltepisoder, er det konjunkturer modellen ikke kunne vite om. Prognoser
  laget i årene før 2021 ligger for lavt fordi ingen tidsseriemodell kunne
  forutse prishoppet i 2021–22.</figcaption></figure>
  <figure><img src="tilbaketest.png" alt="Tilbaketest oppsummert">
  <figcaption>Venstre: hvor ofte fasiten havnet innenfor intervallet, mot målet på
  80 prosent. Høyre: gjennomsnittlig bom for modellen og to enkle regler. Søylene
  kan ikke sammenlignes på tvers av horisonter. Et tiårssnitt svinger naturlig
  mindre enn veksten i ett enkelt år.</figcaption></figure>"""
    else:
        tb_html = ""

    # -- følsomhet --------------------------------------------------------
    if sens:
        sens_hode = "".join(f"<th>{a} år</th>" for a in hor)
        sens_tab = "".join(
            f"<tr><td>{'<strong>' + s['navn'] + '</strong>' if i == 0 else s['navn']}</td>"
            f"<td>{pst(s['drift'])}</td>"
            + "".join(f"<td>{pst(s[a][1])}</td>" for a in hor)
            + f"<td>{pst(s[lang][0], 1)} – {pst(s[lang][2], 1)}</td></tr>"
            for i, s in enumerate(sens)
        )
        lave = [s[lang][1] for s in sens]
        sens_html = f"""
  <h2>Hva om vi hadde valgt annerledes?</h2>
  <p>Modellen hviler på noen få valg i innstillingene. Tabellen viser hva
     anslagene hadde vært med andre rimelige valg. Spriker radene mye, er anslaget
     følsomt for valget, og usikkerheten er i realiteten større enn intervallene
     over viser.</p>
  <table>
  <thead><tr><th>Valg</th><th>Normalvekst</th>{sens_hode}<th>80 %, {lang} år</th></tr></thead>
  <tbody>{sens_tab}</tbody></table>
  <p class="stempel">Median for gjennomsnittlig årlig vekst, i prosent. Med disse
  alternativene varierer {lang}-årsanslaget fra {pst(min(lave), 1)} til
  {pst(max(lave), 1)} %.</p>"""
    else:
        sens_html = ""

    lokal_html = (
        '<p class="stempel">Denne kjøringen brukte det lagrede øyeblikksbildet av '
        "dataene og hentet ikke nye tall fra SSB.</p>"
        if lokal
        else ""
    )
    serie_tekst = f"{C.ARBEIDSTYPE_KODE} – {serienavn}" if serienavn else (
        f"{C.ARBEIDSTYPE_KODE} (navnet ble ikke hentet i denne kjøringen)"
    )

    html = f"""<!doctype html>
<html lang="no"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{C.RAPPORT_TITTEL}</title>
<style>{STIL}</style>
</head><body><div class="ark">

<h1>{C.RAPPORT_TITTEL}</h1>
<p class="undertittel">{C.RAPPORT_UNDERTITTEL}</p>
<p class="stempel">Oppdatert {idag} · siste observasjon {siste} ·
   indeks {nf(indeks.iloc[-1])} · siste 12 md. {fortegn(v12, 1)} %</p>
{lokal_html}

<div class="tall">{''.join(ruter)}</div>
<p class="stempel">Midttallet er medianen: halvparten av de simulerte utfallene
   ligger over, halvparten under. Stolpen viser 80 prosent-intervallet: ett av ti
   utfall havner under, ett av ti over. «Effekt av siste måned» er hvor mye den
   nyeste observasjonen alene flyttet anslaget.</p>

<p class="ingress">{kort_fortalt}</p>

<h2>Slik kommer modellen frem til tallet</h2>
<p>Prognosen bygges i fire trinn. Figuren viser hvor mye hvert trinn flytter
   anslaget, for hver horisont.</p>
<figure><img src="oppbygging.png" alt="Prognosen bygget opp trinn for trinn">
<figcaption>Blå strek flytter anslaget opp, brun strek flytter det ned. Nederst i
hvert panel står 80 %-intervallet rundt medianen.</figcaption></figure>
{trinn_tekst}
<table>
<thead><tr><th>Horisont</th><th>1. Snitt i dataene</th><th>2. Anker</th>
<th>3. Dagens tempo</th><th>4. Til median</th><th>Prognose</th></tr></thead>
<tbody>{trinn_tab}</tbody></table>
<p class="stempel">Prosent per år. Kolonne 2–4 er endringer i prosentpoeng.</p>

<h2>Slik ser usikkerheten ut</h2>
<p>De fire fordelingene under er de simulerte utfallene, ikke en teoretisk kurve.
   Den stiplede linjen er en normalfordeling med samme snitt og spredning, lagt
   oppå som målestokk. Stikker søylene lenger ut til høyre enn kurven, er
   oppsiden større enn nedsiden.</p>
<figure><img src="fordeling.png" alt="Fordeling av årlig vekst">
<figcaption>Heltrukken loddrett strek er medianen, prikkede streker er 10. og
90. persentil.</figcaption></figure>

<figure><img src="bredde.png" alt="Utfallsrom per horisont">
<figcaption>Hvor bredt utfallsrommet er, målt i prosentpoeng. At spennet smalner
med horisonten er riktig og ikke en feil: usikkerheten i <em>nivået</em> vokser,
men enkeltårs svingninger jevner seg ut når de deles på flere år.
{'Den stiplede loddrette linjen er ankeret på ' + pst(m.anker, 1) + ' prosent.' if m.anker is not None else ''}</figcaption></figure>

<h3>Hvor kommer usikkerheten fra?</h3>
<p>Spennet har to kilder. Den ene er sjokk underveis: konjunkturer, lønnsoppgjør
   og prishopp som ingen kan forutse. Den andre er at vi ikke vet hva normal vekst
   egentlig er. Tabellen viser hvor bredt 80 %-intervallet hadde vært med bare én
   kilde om gangen. Sjokkene betyr mest på alle horisonter.</p>
<table>
<thead><tr><th>Horisont</th><th>Begge kilder</th><th>Bare sjokk</th>
<th>Bare usikker normalvekst</th></tr></thead>
<tbody>{kilde_tab}</tbody></table>
<p class="stempel">Bredde på 80 %-intervallet i prosentpoeng. Kildene legges ikke
sammen krone for krone, så summen av de to er større enn totalen.</p>

<h3>Alle tall</h3>
<table>
<thead><tr><th>Horisont</th><th>Median</th><th>Snitt</th><th>P5</th><th>P10</th>
<th>P90</th><th>P95</th><th>Bredde 80</th><th>Skjevhet</th><th>Form</th></tr></thead>
<tbody>{trad}</tbody></table>
<p class="stempel">Alle tall i prosent per år. Skjevhet er null for en perfekt
symmetrisk fordeling.</p>

<h2>Bane</h2>
<figure><img src="vifte_indeks.png" alt="Indeks med vifte"></figure>
<figure><img src="vifte_vekst.png" alt="Tolvmånedersvekst med vifte">
<figcaption>Samme simuleringer sett som årlig vekstrate. Medianbanen går mot
modellens normalvekst på {pst(tk['anker'], 1)} prosent etter hvert som dagens
konjunktursituasjon mister betydning.</figcaption></figure>
<figure><img src="sesong.png" alt="Sesongmønster">
<figcaption>Veksten er ikke jevnt fordelt over året. Søylene viser hvor mye
indeksen normalt stiger i hver måned nå, punktene hvordan det var for ti år siden.
Mønsteret oppdateres gradvis, slik at nyere år teller mest. Sesongen påvirker
banen gjennom året, men ikke årsanslagene: over tolv måneder summerer den seg til
null.</figcaption></figure>
{tb_html}
{sens_html}

<h2>Om modellen</h2>
<div class="merk">
Indeksen måler kostnadene for entreprenørens innsatsfaktorer. Den fanger ikke
endret produktivitet og ikke endringer i entreprenørens fortjenestemargin.
Skal du bruke den som anslag for faktisk byggepris, må du legge på et eget
marginledd.
</div>
<p>Dette er en ren tidsseriemodell. Den kjenner bare indeksens egen historie, ikke
renter, kronekurs, råvarepriser eller lønnsoppgjør. Den kan derfor ikke forutse
vendepunkter. Det den kan, er å si hva som er normalt, hvor mye dagens tempo pleier
å henge igjen, og hvor store avvik historien tilsier at man må regne med.</p>
<p>Tre forutsetninger bærer anslagene:</p>
<ul>
<li>De neste årene ligner på de siste {C.VINDU_AAR}. Et varig skifte i
    kostnadsveksten fanges først opp etter hvert som nye år kommer inn i vinduet.</li>
<li>Fremtidige sjokk ligner på dem vi har sett i vinduet, i størrelse og hyppighet.</li>
<li>Det valgte ankeret er et rimelig forhåndssyn. Vekten står i tabellen under.</li>
</ul>
<table>
<tbody>
<tr><td>Serie</td><td>SSB tabell {C.TABELL}, kode {serie_tekst}</td></tr>
<tr><td>Estimeringsvindu</td><td>{_mnd(m.vindu.index[0])} til {siste} ({C.VINDU_AAR} år, rullerende)</td></tr>
<tr><td>Observasjoner</td><td>{len(m.vindu)}</td></tr>
<tr><td>Snitt i dataene</td><td>{pst(tk['data'])} % per år &plusmn; {nf(m.se_drift * 1200, 2)}</td></tr>
{anker_rad}
<tr><td>Vekt på dataene</td><td>{m.datavekt * 100:.0f} %</td></tr>
<tr><td>Normalvekst brukt i modellen</td><td>{pst(tk['anker'])} % per år &plusmn; {nf(m.drift_sd * 1200, 2)}</td></tr>
<tr><td>Sesongmønster</td><td>glidende, halveringstid {C.SESONG_HALVERINGSTID} år</td></tr>
<tr><td>Sjokk</td><td>blokker på {C.BLOKK_LENGDE} måneder fra vinduet</td></tr>
<tr><td>Simulerte baner</td><td>{C.ANTALL_BANER:,}</td></tr>
<tr><td>Modellversjon</td><td>{MODELLVERSJON}</td></tr>
</tbody></table>
<p class="stempel">Samme data og samme innstillinger gir alltid nøyaktig samme
tall. Rådataene for hver måned ligger i mappen <code>data/</code>, og alle
innstillinger står i <code>config.py</code>.</p>

<footer>
Kilde: Statistisk sentralbyrå, tabell {C.TABELL}, lisensiert CC BY 4.0.
Beregningene er egne og står ikke for SSBs regning.
Siden bygges automatisk hver måned. {C.KONTAKT}
</footer>
</div></body></html>"""

    html = html.replace(f"{C.ANTALL_BANER:,}", f"{C.ANTALL_BANER:,}".replace(",", "&nbsp;"))
    html = html.replace(" %", "&nbsp;%")
    html = html.replace("<table>", '<div class="rulle"><table>').replace("</table>", "</table></div>")
    with open(fil, "w", encoding="utf-8") as f:
        f.write(html)


# ===========================================================================
# 8. HOVEDPROGRAM
# ===========================================================================


def main(argv: list[str] | None = None) -> int:
    tolker = argparse.ArgumentParser(description="Byggekostnadsmodell for boligblokk")
    tolker.add_argument(
        "--lokal",
        action="store_true",
        help="bruk data/indeks_siste.csv i stedet for å hente fra SSB",
    )
    tolker.add_argument(
        "--uten-tilbaketest", action="store_true", help="hopp over tilbaketesten"
    )
    valg = tolker.parse_args(argv)

    os.makedirs(UT, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)

    logg("=" * 64)
    logg("BYGGEKOSTNADSMODELL  ·  " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    logg("=" * 64)

    logg("\n[1/5] Henter data")
    navnefil = os.path.join(DATA, "serienavn.txt")
    if valg.lokal:
        indeks = les_lokal_indeks()
        kontroller_serie(indeks)
        serienavn = ""
        if os.path.exists(navnefil):
            with open(navnefil, encoding="utf-8") as f:
                serienavn = f.read().strip()
    else:
        serienavn = skriv_ut_kodeliste()
        indeks = hent_indeks()
        kontroller_serie(indeks)
        lagre_oyeblikksbilde(indeks)
        if serienavn:
            with open(navnefil, "w", encoding="utf-8") as f:
                f.write(serienavn + "\n")

    logg("\n[2/5] Estimerer modell")
    m = Modell(indeks)
    logg(f"  Vindu:            {m.vindu.index[0]} til {m.vindu.index[-1]}")
    logg(f"  Snitt i dataene:  {np.expm1(m.drift_data * 12) * 100:.2f} % per år")
    if m.anker is not None:
        logg(f"  Anker:            {m.anker * 100:.2f} % per år")
    logg(
        f"  Normalvekst:      {np.expm1(m.drift * 12) * 100:.2f} % per år "
        f"(dataene veier {m.datavekt * 100:.0f} %)"
    )

    logg("\n[3/5] Simulerer")
    maks = max(C.HORISONTER_AR)
    baner = m.simuler(12 * maks, C.ANTALL_BANER, C.TILFELDIG_FRO)
    fordelinger = {aar: m.aarlig_vekst(baner, aar) for aar in sorted(C.HORISONTER_AR)}
    rader = oppsummer(fordelinger)
    for r in rader:
        logg(
            f"  {r['horisont_ar']:>2} år: {r['median'] * 100:5.2f} %   "
            f"80 %-intervall {r['p10'] * 100:5.2f} – {r['p90'] * 100:5.2f}   "
            f"({r['form']})"
        )
    trinn = oppbygging(m, fordelinger)

    # usikkerhetskilder: samme simulering uten usikkerhet om normalveksten
    bare_sjokk = m.simuler(12 * maks, C.ANTALL_BANER, C.TILFELDIG_FRO, uten_driftusikkerhet=True)
    kilder = {}
    for r in rader:
        aar = r["horisont_ar"]
        p10, p90 = np.percentile(m.aarlig_vekst(bare_sjokk, aar), [10, 90])
        kilder[aar] = {
            "total": r["bredde_80"],
            "sjokk": float(p90 - p10),
            "drift": float(2 * 1.2816 * m.drift_sd * 12),
        }

    endringer = endring_siden_forrige_maned(indeks, rader)
    logg("  Regner følsomhet for valgene i config.py")
    sens = folsomhet(indeks)

    logg("\n[4/5] Lager grafer")
    graf_oppbygging(trinn, f"{UT}/oppbygging.png")
    graf_vifte_indeks(m, baner, f"{UT}/vifte_indeks.png")
    graf_vifte_vekst(m, baner, f"{UT}/vifte_vekst.png")
    graf_fordelinger(fordelinger, rader, f"{UT}/fordeling.png")
    graf_bredde(rader, m, f"{UT}/bredde.png")
    graf_sesong(m, f"{UT}/sesong.png")

    tb, perioder = None, []
    if C.KJOR_TILBAKETEST and not valg.uten_tilbaketest:
        logg("\n  Tilbaketest")
        serier = tilbaketest_baner(indeks)
        graf_tilbaketest_bane(indeks, serier, f"{UT}/tilbaketest_bane.png")

        tb = tilbaketest(indeks)
        if not tb.empty:
            tb.to_csv(f"{DATA}/tilbaketest.csv", index=False)
            perioder = tilbaketest_perioder(tb)
            graf_tilbaketest_feil(tb, f"{UT}/tilbaketest_feil.png")
            graf_tilbaketest(tb, f"{UT}/tilbaketest.png")
            for a, d in tb.groupby("horisont_ar"):
                logg(f"  Dekning 80 %-intervall, {a:>2} år: {100 * d['innenfor80'].mean():.0f} %")
    elif os.path.exists(f"{DATA}/tilbaketest.csv"):
        # tilbaketesten er hoppet over: vis forrige resultat i stedet for ingenting
        tb = pd.read_csv(f"{DATA}/tilbaketest.csv")
        if "p10" in tb.columns:
            perioder = tilbaketest_perioder(tb)
        else:
            tb = None

    logg("\n[5/5] Skriver rapport")
    oppdater_historikk(indeks, rader)
    lag_rapport(
        indeks, m, rader, trinn, kilder, endringer, tb, perioder, sens,
        serienavn, valg.lokal, f"{UT}/index.html",
    )

    pd.DataFrame(rader).assign(
        siste_periode=str(indeks.index[-1]),
        kjørt=datetime.now().strftime("%Y-%m-%d"),
        modellversjon=MODELLVERSJON,
    ).to_csv(f"{UT}/resultater.csv", index=False)

    logg(f"\n  Ferdig. Se {UT}/index.html")
    logg("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
