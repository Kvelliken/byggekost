# Byggekostnadsmodell — boligblokk

Estimerer forventet gjennomsnittlig årlig vekst i SSBs byggekostnadsindeks for
boligblokk (tabell 08655), for 1, 3, 5 og 10 år frem, med usikkerhetsspenn.

Kjører automatisk den 16. hver måned og publiserer resultatet som en nettside.

**Resultatside:** https://Kvelliken.github.io/byggekost/

Rapporten viser hvordan hvert tall er blitt til, hvor ofte modellen ville ha
truffet historisk, og hvor mye anslagene avhenger av valgene i `config.py`.

## Slik regner modellen

Prognosen bygges i fire trinn. Rapporten viser tallene for hvert trinn.

1. **Snittet i dataene.** Snittveksten de siste 20 årene. Vinduet ruller, så
   hver måned kommer en ny måned inn og den eldste går ut.
2. **Ankeret.** Snittet veies sammen med et forhåndssyn: inflasjonsmålet pluss
   antatt realvekst i byggekostnader (3,0 % i dag). Det mest presise av de to
   får mest vekt. Ankeret veier rundt 10–15 %, likt på alle horisonter.
3. **Dagens tempo.** Ligger veksten over eller under normalen nå, pleier noe
   av avviket å henge igjen. Det flytter ettårsanslaget, og nesten ikke
   tiårsanslaget.
4. **Simulering.** 10 000 mulige fremtider trekkes fra de historiske avvikene,
   i sammenhengende biter på 18 måneder. Medianen og spennet leses av der.

Sesongmønsteret (store hopp i mai og januar) får endre seg over tid, slik at
nyere år teller mest.

## Hva modellen ikke kan

- Den kjenner bare indeksens egen historie. Renter, kronekurs, råvarepriser og
  lønnsoppgjør er ikke med, og den kan ikke forutse vendepunkter.
- Den antar at de neste årene ligner på de siste 20.
- Indeksen måler entreprenørens innsatsfaktorkostnader. Den fanger ikke
  produktivitetsendringer eller endringer i fortjenestemarginer.

## Hvor godt treffer den?

Tilbaketesten stiller modellen opp på hvert månedsskifte siden 1995, med bare
de dataene som fantes da og samme regler som i dag. Resultatet står i rapporten,
delt på horisont og på periode. Kort oppsummert har 80 %-intervallene truffet
omtrent så ofte som de skal på ett til fem års sikt, men prognoser laget rett
før prishoppet i 2021–22 bommet klart. Midtanslaget er bare litt bedre enn
snittveksten alene. Det er spennet som er modellens viktigste bidrag.

## Filer

| Fil | Hva den gjør |
|---|---|
| `config.py` | Alle innstillinger. Den eneste filen du normalt endrer. |
| `modell.py` | Henter data, estimerer, simulerer, tester, lager grafer og rapport. |
| `tests/` | Automatiske tester. Kjøres før hver månedskjøring. |
| `requirements.txt` | Pakkene som trengs. |
| `.github/workflows/manedlig.yml` | Timeplanen for den automatiske kjøringen. |
| `docs/` | Nettsiden som publiseres. Skrives av modellen — ikke rediger. |
| `data/` | Rådata per måned, tilbaketesten og logg over anslagene. |

I `data/` heter øyeblikksbildene `indeks_ÅÅÅÅ-MM.csv` etter siste måned de
inneholder. `resultater_historikk.csv` har én rad per datamåned, horisont og
modellversjon.

## Kjøre den selv

```bash
pip install -r requirements.txt
```

```bash
python modell.py
```

Åpne så `docs/index.html` i nettleseren.

Uten nett, eller for å prøve en endring i `config.py` på de sist lagrede tallene:

```bash
python modell.py --lokal
```

Kjør testene med:

```bash
pip install pytest
```

```bash
python -m pytest -q
```

## Endre metoden

Endrer du noe som påvirker tallene (ikke bare utseendet), øk `MODELLVERSJON`
øverst i `modell.py`. Da blandes ikke gamle og nye anslag i historikken.

Kilde: Statistisk sentralbyrå, tabell 08655, CC BY 4.0.
Beregningene er egne og står ikke for SSBs regning.
