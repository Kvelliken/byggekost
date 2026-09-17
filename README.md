# Byggekostnadsmodell — boligblokk

Estimerer forventet gjennomsnittlig årlig vekst i SSBs byggekostnadsindeks for
boligblokk (tabell 08655), for 1, 3, 5 og 10 år frem, med usikkerhetsspenn.

Kjører automatisk den 16. hver måned og publiserer resultatet som en nettside.

**Resultatside:** https://Kvelliken.github.io/byggekost/
*(bytt ut DITTBRUKERNAVN etter at du har satt opp GitHub Pages)*

## Filer

| Fil | Hva den gjør |
|---|---|
| `config.py` | Alle innstillinger. Den eneste filen du normalt endrer. |
| `modell.py` | Henter data, estimerer, simulerer, lager grafer og rapport. |
| `requirements.txt` | Pakkene som trengs. |
| `.github/workflows/manedlig.yml` | Timeplanen for den automatiske kjøringen. |
| `docs/` | Nettsiden som publiseres. Skrives av modellen — ikke rediger. |
| `data/` | Daterte øyeblikksbilder av rådata og historikk over anslagene. |

## Kjøre den selv

```bash
pip install -r requirements.txt
python modell.py
```

Åpne så `docs/index.html` i nettleseren.

## Om modellen

Månedlig vekst modelleres som langsiktig drift + sesongmønster + en treg
avviksprosess. Usikkerheten kommer fra to kilder: sjokk trukket i
sammenhengende blokker fra faktiske historiske avvik, og usikkerhet om
hva den langsiktige driften egentlig er. Den første dominerer på kort
sikt, den andre på lang.

Indeksen måler entreprenørens innsatsfaktorkostnader. Den fanger ikke
produktivitetsendringer eller endringer i fortjenestemarginer.

Kilde: Statistisk sentralbyrå, tabell 08655, CC BY 4.0.
Beregningene er egne og står ikke for SSBs regning.
