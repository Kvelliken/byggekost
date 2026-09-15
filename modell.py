"""
Byggekostnadsmodell for boligblokk(SSB tabell 08655).

Kjøres med:  python modell.py

Gjør fire ting:
  1. Henter indeksen fra SSB og lagrer et datert øyeblikksbilde
  2. Estimerer en modell for månedlig kostnadsvekst
  3. Simulerer 10 000 mulige fremtider og regner ut fordelingen av
     gjennomsnittlig årlig vekst for 1, 3, 5 og 10 år
  4. Skriver grafer og en HTML-side til mappen docs/

Ingen andre filer enn config.py trenger a endres for vanlig bruk.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from datetime import date, datetime

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

UT = "docs"
DATA = "data"


def logg(melding: str) -> None:
    print(melding, flush=True)


# ===========================================================================
# 1. DATA
# ===========================================================================


BASIS = "https://data.ssb.no/api/pxwebapi/v2/tables"


def skriv_ut_kodeliste() -> None:
    """Henter metadata og viser hvilke koder tabellen har.

    Dette er ren dokumentasjon i loggen. Bruk den til a bekrefte at
    ARBEIDSTYPE_KODE i config.py peker på riktig serie.
    """
    url = f"{BASIS}/{C.TABELL}/metadata?lang=no&outputFormat=json-stat2"
    try:
        svar = requests.get(url, timeout=60)
        svar.raise_for_status()
        meta = svar.json()
    except Exception as feil:  # metadata er hyggelig, men ikke kritisk
        logg(f"  (klarte ikke hente kodeliste: {feil})")
        return

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
            logg(f"      {k:<10} {etiketter.get(k ,'')}")
    logg("")


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
            logg(f"  Forsok {forsok} feilet({feil}) - prøver igjen")
    else:
        raise RuntimeError(f"Fikk ikke tak i data fra SSB: {siste_feil}")

    perioder = list(rad["dimension"]["Tid"]["category"]["index"])
    verdier = rad["value"]
    if len(perioder) != len(verdier):
        raise RuntimeError("Uventet svarformat fra SSB - antall perioder stemmer ikke")

    par = [(p, v) for p, v in zip(perioder, verdier) if v is not None]
    stempler = pd.PeriodIndex([p.replace("M", "-") for p, _ in par], freq="M")
    serie = pd.Series([v for _, v in par], index=stempler, dtype=float).sort_index()

    logg(f"  Fikk {len(serie)} måneder: {serie.index[0 ]} til {serie.index[-1 ]}")
    logg(f"  Siste verdi: {serie.iloc[-1 ]:.1f}")
    return serie


def lagre_oyeblikksbilde(serie: pd.Series) -> None:
    """Lagrer rådata datert, slik at enhver kjøring kan rekonstrueres senere."""
    os.makedirs(DATA, exist_ok=True)
    fil = os.path.join(DATA, "indeks_siste.csv")
    ramme = pd.DataFrame({"periode": serie.index.astype(str), "indeks": serie.values})
    ramme.to_csv(fil, index=False)

    arkiv = os.path.join(DATA, f"indeks_{date.today():%Y-%m}.csv")
    ramme.to_csv(arkiv, index=False)
    logg(f"  Lagret {fil} og {arkiv}")


# ===========================================================================
# 2. MODELL
# ===========================================================================


class Modell:
    """Månedlig logvekst = langsiktig drift + sesongmønster + treg avviksprosess.

    Sesongmønsteret fanger at lønnsdelen av indeksen hopper i april/mai når
    tariffoppgjøret slår inn, og ellers ligger flatt.

    Avviksprosessen(en AR-prosess) fanger at når veksten først har lagt seg
    over eller under normalen, blir den værende der en stund. Det er denne
    som gjør at ettårsprognosen tar hensyn til hvor vi står i dag, i stedet
    for å hoppe rett til langtidssnittet.
    """

    MIN_OBS = 120  # minst ti år før vi tror på et estimat

    def __init__(self, indeks: pd.Series, lags: int = 12, est_fra: str | None = None):
        self.full = indeks
        self.lags = lags

        start = pd.Period((est_fra or C.ESTIMERING_FRA).replace("M", "-"), freq="M")
        vindu = indeks[indeks.index >= start]
        if len(vindu) < self.MIN_OBS:
            # Vanlig i tilbaketesten: startpunktet ligger for nær
            # estimeringsvinduets begynnelse. Da bruker vi alt vi har.
            vindu = indeks
        if len(vindu) < self.MIN_OBS:
            raise RuntimeError("For lite data til å estimere")
        self.vindu = vindu

        y = np.log(vindu.values)
        d = np.diff(y)  # månedlig logvekst
        mnd = vindu.index.month.values[1:]  # måned for hver d

        # -- sesong -------------------------------------------------------
        self.drift_data = float(d.mean())
        sesong = np.zeros(12)
        for m in range(1, 13):
            sesong[m - 1] = d[mnd == m].mean() - self.drift_data
        sesong -= sesong.mean()  # summerer til null
        self.sesong = sesong

        # -- avvik og AR-prosess ------------------------------------------
        u = d - self.drift_data - sesong[mnd - 1]
        self.u = u

        X = np.column_stack([u[lags - i - 1 : len(u) - i - 1] for i in range(lags)])
        yv = u[lags:]

        # Ridge-straff. Uten den blir de 12 koeffisientene så støyete at en
        # enkelt uvanlig måned kan kaste ettårsprognosen flere tideler.
        # Straffen krymper dem mot null og gjør anslagene stabile fra måned
        # til måned - som er hele poenget når du skal følge modellen over tid.
        XtX = X.T @ X
        lam = C.RIDGE * np.trace(XtX) / lags
        koef = np.linalg.solve(XtX + lam * np.eye(lags), X.T @ yv)
        self.phi = self._stabiliser(koef)
        self.resid = yv - X @ self.phi
        self.sist_u = u[-lags:][::-1].copy()  # nyeste først

        # -- usikkerhet om driften ----------------------------------------
        self.se_drift = self._se_drift(d)
        pri_m = np.log1p(C.ANKER_AARLIG) / 12.0
        pri_sd = C.ANKER_USIKKERHET / 12.0
        p_d, p_p = 1.0 / self.se_drift**2, 1.0 / pri_sd**2
        self.drift = (p_d * self.drift_data + p_p * pri_m) / (p_d + p_p)
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
        """Standardfeil for gjennomsnittsdriften, via blokk-bootstrap.

        Vanlig standardfeil undervurderer usikkerheten grovt her, fordi
        månedene ikke er uavhengige. Blokker tar høyde for det.
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

    def simuler(self, måneder: int, baner: int, fro: int) -> np.ndarray:
        """Returnerer en matrise(baner x måneder) med simulert logindeks."""
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

        # egen langsiktig drift per bane
        drift = rng.normal(self.drift, self.drift_sd, size=(baner, 1))

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
        """Gjennomsnittlig årlig vekst(CAGR) over `år` år, for hver bane."""
        y0 = float(np.log(self.full.values[-1]))
        return np.expm1((logg_bane[:, 12 * aar - 1] - y0) / aar)


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
    return "tilnaermet symmetrisk"


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
    ax.set_ylabel("Indeks(2015 = 100)")
    ax.set_title("Indeksniva med usikkerhetsvifte")
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
    ax.axhline(C.ANKER_AARLIG * 100, color=FARGE["anker"], lw=1.1, ls=(0, (5, 3)))
    ax.annotate(
        "langsiktig anker",
        xy=(x[0], C.ANKER_AARLIG * 100),
        xytext=(8, -14),
        textcoords="offset points",
        ha="left",
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
        ax.set_xlabel("Gjennomsnittlig årlig vekst(%)")
        ax.set_yticks([])

    for ax in akser[n:]:
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(fil)
    plt.close(fig)


def graf_bredde(rader: list[dict], fil: str) -> None:
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

    ax.axvline(C.ANKER_AARLIG * 100, color=FARGE["anker"], lw=1.1, ls=(0, (5, 3)))
    ax.set_yticks(y, [f"{a} år" for a in aar])
    ax.invert_yaxis()
    ax.set_xlabel("Gjennomsnittlig årlig vekst(%)")
    ax.set_title("Utfallsrom per horisont  ·  morkt felt = 80 %, lyst = 90 %")
    ax.grid(axis="y", visible=False)
    ax.margins(x=0.13, y=0.30)
    fig.savefig(fil)
    plt.close(fig)


def graf_tilbaketest(tb: pd.DataFrame, fil: str) -> None:
    fig, akser = plt.subplots(1, 2, figsize=(9.2, 3.9))

    aar = sorted(tb["horisont_ar"].unique())
    dekning = [100 * tb.loc[tb.horisont_ar == a, "innenfor80"].mean() for a in aar]
    akser[0].bar([str(a) for a in aar], dekning, color=FARGE["band"], width=0.55)
    akser[0].axhline(80, color=FARGE["anker"], lw=1.2, ls=(0, (5, 3)))
    akser[0].set_ylim(0, 100)
    akser[0].set_ylabel("Andel innenfor 80 %-intervallet")
    akser[0].set_xlabel("Horisont(år)")
    akser[0].set_title("Treffer intervallene?")

    bredde = 0.38
    p = np.arange(len(aar))
    mod = [tb.loc[tb.horisont_ar == a, "feil_modell"].abs().mean() * 100 for a in aar]
    naiv = [tb.loc[tb.horisont_ar == a, "feil_naiv"].abs().mean() * 100 for a in aar]
    akser[1].bar(p - bredde / 2, mod, bredde, label="modell", color=FARGE["median"])
    akser[1].bar(p + bredde / 2, naiv, bredde, label="naiv", color=FARGE["svak"])
    akser[1].set_xticks(p, [str(a) for a in aar])
    akser[1].set_ylabel("Gj.sn. absoluttfeil(pp)")
    akser[1].set_xlabel("Horisont(år)")
    akser[1].set_title("Bedre enn a videreforo siste års vekst?")
    akser[1].legend(frameon=False, fontsize=9)

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
    tidligst = indeks.index[0] + Modell.MIN_OBS

    # Nyeste startpunkt der vi rekker å se hele horisonten, så bakover
    # med jevne mellomrom.
    nyeste = sist - 12 * aar
    punkter = [nyeste - 12 * aar * k for k in range(C.TILBAKETEST_VIS_ANTALL)]
    punkter = sorted(p for p in punkter if p >= tidligst)

    ut = []
    for n, punkt in enumerate(punkter):
        historikk = indeks[indeks.index <= punkt]
        try:
            m = Modell(historikk, est_fra=C.ESTIMERING_FRA)
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
    """Later som modellen sto på ulike tidspunkter i fortiden og sjekker fasiten."""
    start = pd.Period(C.TILBAKETEST_FRA.replace("M", "-"), freq="M")

    rader = []

    punkter = [p for p in indeks.index if p >= start][:: C.TILBAKETEST_STEG]
    logg(f"  {len(punkter)} startpunkter å teste")

    for n, punkt in enumerate(punkter):
        historikk = indeks[indeks.index <= punkt]
        if len(historikk) < 120:
            continue

        # Modellen får bare se data som fantes på dette tidspunktet.
        try:
            m = Modell(historikk, est_fra=C.ESTIMERING_FRA)
        except Exception:
            continue

        maks = max(C.HORISONTER_AR)
        baner = m.simuler(12 * maks, C.TILBAKETEST_BANER, C.TILFELDIG_FRO + n)

        siste12 = np.expm1(np.log(historikk.values[-1]) - np.log(historikk.values[-13]))

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
                    "naiv": siste12,
                    "feil_modell": p50 - faktisk,
                    "feil_naiv": siste12 - faktisk,
                    "innenfor80": bool(p10 <= faktisk <= p90),
                }
            )

        if (n + 1) % max(1, len(punkter) // 8) == 0:
            logg(f"    {n + 1}/{len(punkter)} ...")

    return pd.DataFrame(rader)


# ===========================================================================
# 6. HISTORIKK
# ===========================================================================


def oppdater_historikk(indeks: pd.Series, rader: list[dict]) -> pd.DataFrame | None:
    os.makedirs(DATA, exist_ok=True)
    fil = os.path.join(DATA, "resultater_historikk.csv")
    tidligere = pd.read_csv(fil) if os.path.exists(fil) else None

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
            }
            for r in rader
        ]
    )

    samlet = ny if tidligere is None else pd.concat([tidligere, ny], ignore_index=True)
    samlet = samlet.drop_duplicates(subset=["kjørt", "horisont_ar"], keep="last")
    samlet.to_csv(fil, index=False)
    return tidligere


def finn_endringer(tidligere: pd.DataFrame | None, rader: list[dict]) -> dict[int, float]:
    if tidligere is None or tidligere.empty:
        return {}
    forrige = tidligere[tidligere["kjørt"] == tidligere["kjørt"].max()]
    ut = {}
    for r in rader:
        treff = forrige[forrige["horisont_ar"] == r["horisont_ar"]]
        if not treff.empty:
            ut[r["horisont_ar"]] = r["median"] - float(treff["median"].iloc[0])
    return ut


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
footer{margin-top:66px;padding-top:20px;border-top:1px solid var(--strek);
       color:var(--svak);font-size:13px}
a{color:var(--dyp)}
@media print{body{background:#fff}.ark{padding:0}figure{break-inside:avoid}}
"""


def nf(x: float, d: int = 1) -> str:
    """Norsk tallformat: komma som desimalskilletegn."""
    return f"{x:.{d}f}".replace(".", ",")


def nf(x: float, d: int = 1) -> str:
    """Norsk tallformat: komma som desimalskilletegn."""
    t = f"{abs(x):.{d}f}".replace(".", ",")
    return ("−" + t) if x < 0 else t


def fortegn(x: float, d: int = 2) -> str:
    """Som nf(), men alltid med fortegn foran."""
    return ("+" if x >= 0 else "−") + f"{abs(x):.{d}f}".replace(".", ",")


def lag_rapport(indeks, m, rader, endringer, tb, fil):
    idag = datetime.now().strftime("%d.%m.%Y")
    siste = str(indeks.index[-1]).replace("-", "M")
    v12 = np.expm1(np.log(indeks.values[-1]) - np.log(indeks.values[-13])) * 100

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
            dhtml = '<div class="delta">første kjøring</div>'
        elif abs(d) < 0.00005:
            dhtml = '<div class="delta">uendret siden forrige måned</div>'
        else:
            k = "opp" if d > 0 else "ned"
            dhtml = (
                f'<div class="delta">siden forrige måned '
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

    trad = "".join(
        f"<tr><td>{r['horisont_ar']} år</td>"
        f"<td>{nf(r['median'] * 100, 2)}</td><td>{nf(r['gjennomsnitt'] * 100, 2)}</td>"
        f"<td>{nf(r['p5'] * 100, 2)}</td><td>{nf(r['p10'] * 100, 2)}</td>"
        f"<td>{nf(r['p90'] * 100, 2)}</td><td>{nf(r['p95'] * 100, 2)}</td>"
        f"<td>{nf(r['bredde_80'] * 100, 2)}</td>"
        f"<td>{fortegn(r['skjevhet'])}</td><td>{r['form']}</td></tr>"
        for r in rader
    )

    if tb is not None and not tb.empty:
        d80 = 100 * tb["innenfor80"].mean()
        vinn = 100 * (tb["feil_modell"].abs() < tb["feil_naiv"].abs()).mean()
        antall_aar = len(indeks) // 12
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

  <h2>Tallene bak</h2>
  <p>Modellen er kjørt på nytt fra {tb['startpunkt'].nunique()} startpunkter
     gjennom historien. Det faktiske utfallet havnet innenfor 80 %-intervallet i
     <strong>{d80:.0f} %</strong> av tilfellene, og modellen slo den naive regelen
     &laquo;siste års vekst fortsetter&raquo; i <strong>{vinn:.0f} %</strong>.</p>
  <div class="merk">
  Ikke les for mye ut av disse prosentene. Startpunktene ligger én måned fra
  hverandre, så nabovinduer deler nesten hele perioden og teller i praksis som
  én observasjon. Det bindende taket er hvor mange ikke-overlappende år serien
  inneholder — rundt {antall_aar} for ettårsanslaget, og bare en håndfull for
  tiårsanslaget. Et dekningstall på 89 i stedet for 80 ligger godt innenfor det
  tilfeldigheter kan forklare.
  </div>
  <figure><img src="tilbaketest_feil.png" alt="Bommen over tid">
  <figcaption>Over null betyr at modellen anslo for høyt. Klumper feilene seg
  rundt enkeltepisoder, er det konjunkturer modellen ikke kunne vite om. Ligger
  de skjevt over lange perioder, er det noe mer systematisk.</figcaption></figure>
  <figure><img src="tilbaketest.png" alt="Tilbaketest oppsummert">
  <figcaption>Venstre: hvor ofte fasiten havnet innenfor intervallet, mot målet på
  80 prosent. Høyre: gjennomsnittlig bomskudd, modell mot naiv regel. Søylene kan
  ikke sammenlignes på tvers av horisonter — en tiårssnittvekst svinger naturlig
  mindre enn en ettårsvekst, så målskiven er større.</figcaption></figure>"""
    else:
        tb_html = ""

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

<div class="tall">{''.join(ruter)}</div>
<p class="stempel">Midttallet er medianen. Stolpen viser 80 prosent-intervallet:
   ett av ti utfall havner under, ett av ti over.</p>

<h2>Slik ser usikkerheten ut</h2>
<p>De fire fordelingene under er de simulerte utfallene, ikke en teoretisk kurve.
   Den stiplede linjen er en normalfordeling med samme snitt og spredning, lagt
   oppa som målestokk. Ligger søylene systematisk til høyre for den, betyr det at
   oppsiden er større enn nedsiden.</p>
<figure><img src="fordeling.png" alt="Fordeling av årlig vekst">
<figcaption>Heltrukken loddrett strek er medianen, prikkede streker er 10. og
90. persentil.</figcaption></figure>

<figure><img src="bredde.png" alt="Utfallsrom per horisont">
<figcaption>Hvor bredt utfallsrommet er, målt i prosentpoeng. At spennet smalner
med horisonten er riktig og ikke en feil: usikkerheten i <em>nivaet</em> vokser,
men enkeltars svingninger jevner seg ut nar de deles på flere år. Den stiplede
loddrette linjen er det langsiktige ankeret på {nf(C.ANKER_AARLIG * 100)} prosent.</figcaption></figure>

<h2>Tall</h2>
<table>
<thead><tr><th>Horisont</th><th>Median</th><th>Snitt</th><th>P5</th><th>P10</th>
<th>P90</th><th>P95</th><th>Bredde 80</th><th>Skjevhet</th><th>Form</th></tr></thead>
<tbody>{trad}</tbody></table>
<p class="stempel">Alle tall i prosent per år. Skjevhet er null for en perfekt
symmetrisk fordeling.</p>

<h2>Bane</h2>
<figure><img src="vifte_indeks.png" alt="Indeks med vifte"></figure>
<figure><img src="vifte_vekst.png" alt="Tolvmånedersvekst med vifte">
<figcaption>Samme simuleringer sett som årlig vekstrate. Legg merke til hvordan
banene trekker mot det langsiktige ankeret etter hvert som dagens
konjunktursituasjon mister betydning.</figcaption></figure>
{tb_html}

<h2>Om modellen</h2>
<div class="merk">
Indeksen måler kostnadene for entreprenørens innsatsfaktorer. Den fanger ikke
endret produktivitet og ikke endringer i entreprenørens fortjenestemargin.
Skal du bruke den som anslag for faktisk byggepris, må du legge på et eget
marginledd.
</div>
<p>Månedlig vekst modelleres som langsiktig drift pluss et sesongmønster pluss
en treg avviksprosess. Sesongmonsteret fanger at lønnsdelen hopper nar
tariffoppgjøret slar inn. Avviksprosessen gjør at prognosen tar utgangspunkt
i hvor vi faktisk står i dag.</p>
<p>Usikkerheten har to kilder. Sjokk trekkes i sammenhengende blokker på
{C.BLOKK_LENGDE} måneder fra faktiske historiske avvik, slik at simuleringene
arver at urolige perioder kommer i klynger. I tillegg får hver bane sin egen
langsiktige drift, fordi vi ikke vet hva den sanne driften er. Den første
kilden dominerer på kort sikt, den andre på lang.</p>
<table>
<tbody>
<tr><td>Estimeringsvindu</td><td>{C.ESTIMERING_FRA} til {siste}</td></tr>
<tr><td>Observasjoner</td><td>{len(m.vindu)}</td></tr>
<tr><td>Drift i dataene</td><td>{nf(np.expm1(m.drift_data * 12) * 100, 2)} % per år</td></tr>
<tr><td>Anker</td><td>{nf(C.ANKER_AARLIG * 100, 2)} % &plusmn; {nf(C.ANKER_USIKKERHET * 100, 2)}</td></tr>
<tr><td>Drift brukt i modellen</td><td>{nf(np.expm1(m.drift * 12) * 100, 2)} % per år</td></tr>
<tr><td>Vekt på dataene</td><td>{m.datavekt *100:.0f} %</td></tr>
<tr><td>Simulerte baner</td><td>{C.ANTALL_BANER:,}</td></tr>
</tbody></table>

<footer>
Kilde: Statistisk sentralbyrå, tabell {C.TABELL}, lisensiert CC BY 4.0.
Beregningene er egne og står ikke for SSBs regning.
Siden bygges automatisk hver måned. {C.KONTAKT}
</footer>
</div></body></html>"""

    with open(fil, "w", encoding="utf-8") as f:
        f.write(html)


# ===========================================================================
# 8. HOVEDPROGRAM
# ===========================================================================


def main() -> int:
    os.makedirs(UT, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)

    logg("=" * 64)
    logg("BYGGEKOSTNADSMODELL  ·  " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    logg("=" * 64)

    logg("\n[1/5] Henter data")
    skriv_ut_kodeliste()
    indeks = hent_indeks()
    lagre_oyeblikksbilde(indeks)

    logg("\n[2/5] Estimerer modell")
    m = Modell(indeks)
    logg(f"  Drift i dataene:  {np.expm1(m.drift_data * 12) * 100:.2f} % per år")
    logg(f"  Anker:            {C.ANKER_AARLIG *100:.2f} % per år")
    logg(
        f"  Brukt drift:      {np.expm1(m.drift *12)*100:.2f} % per år "
        f"(dataene veier {m.datavekt *100:.0f} %)"
    )

    logg("\n[3/5] Simulerer")
    maks = max(C.HORISONTER_AR)
    baner = m.simuler(12 * maks, C.ANTALL_BANER, C.TILFELDIG_FRO)
    fordelinger = {aar: m.aarlig_vekst(baner, aar) for aar in sorted(C.HORISONTER_AR)}
    rader = oppsummer(fordelinger)
    for r in rader:
        logg(
            f"  {r['horisont_ar']:>2} år: {r['median']*100:5.2f} %   "
            f"80 %-intervall {r['p10']*100:5.2f} – {r['p90']*100:5.2f}   "
            f"({r['form']})"
        )

    logg("\n[4/5] Lager grafer")
    graf_vifte_indeks(m, baner, f"{UT}/vifte_indeks.png")
    graf_vifte_vekst(m, baner, f"{UT}/vifte_vekst.png")
    graf_fordelinger(fordelinger, rader, f"{UT}/fordeling.png")
    graf_bredde(rader, f"{UT}/bredde.png")

    tb = None
    if C.KJOR_TILBAKETEST:
        logg("\n  Tilbaketest")
        serier = tilbaketest_baner(indeks)
        graf_tilbaketest_bane(indeks, serier, f"{UT}/tilbaketest_bane.png")

        tb = tilbaketest(indeks)
        if not tb.empty:
            tb.to_csv(f"{DATA}/tilbaketest.csv", index=False)
            graf_tilbaketest_feil(tb, f"{UT}/tilbaketest_feil.png")
            graf_tilbaketest(tb, f"{UT}/tilbaketest.png")
            logg(f"  Dekning 80 %-intervall: {100 *tb['innenfor80'].mean():.0f} %")

    logg("\n[5/5] Skriver rapport")
    tidligere = oppdater_historikk(indeks, rader)
    endringer = finn_endringer(tidligere, rader)
    lag_rapport(indeks, m, rader, endringer, tb, f"{UT}/index.html")

    pd.DataFrame(rader).assign(
        siste_periode=str(indeks.index[-1]),
        kjørt=datetime.now().strftime("%Y-%m-%d"),
    ).to_csv(f"{UT}/resultater.csv", index=False)

    logg(f"\n  Ferdig. Se {UT}/index.html")
    logg("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
