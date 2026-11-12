"""
Innstillinger for byggekostnadsmodellen.

Dette er den eneste filen du normalt trenger å endre.
Alt er kommentert. Endre tall, lagre, og modellen bruker de nye verdiene
neste gang den kjører.

Rapporten viser hver måned hvor mye anslagene flytter seg hvis de viktigste
valgene her hadde vært annerledes (avsnittet «Hva om vi hadde valgt
annerledes?»). Se der før du endrer noe.
"""

# ---------------------------------------------------------------------------
# 1. DATAKILDE (SSB Statistikkbanken, PxWebApi v2)
# ---------------------------------------------------------------------------

# Tabell 08655 = Byggekostnadsindeks for boligblokk, 2015=100, fra 1978M01.
TABELL = "08655"

# Koden for serien vi modellerer. "20" er koden SSB selv bruker i sitt
# API-eksempel for denne tabellen (samlet indeks).
#
# Hver kjøring skriver ut HELE kodelisten i loggen, under overskriften
# "TILGJENGELIGE KODER", og navnet på den valgte serien står i tabellen
# nederst i rapporten. Sjekk at det er serien du vil ha.
ARBEIDSTYPE_KODE = "20"

# Første måned vi henter. Serien starter 1978M01.
DATA_FRA = "1978M01"


# ---------------------------------------------------------------------------
# 2. ESTIMERINGSVINDU
# ---------------------------------------------------------------------------

# Modellen lærer av de siste VINDU_AAR årene, og bare dem.
#
# Hvorfor ikke hele historikken fra 1978? Fordi 1980-tallet var et annet
# pengepolitisk regime, med kostnadsvekst rundt 7 prosent. Hvorfor ikke
# kortere? Fordi et kort vindu lar én høy- eller lavkonjunktur bestemme hele
# anslaget.
#
# Vinduet ruller: hver måned kommer en ny måned inn og den eldste går ut.
# Tilbaketesten bruker nøyaktig samme regel, så treffsikkerheten du ser i
# rapporten gjelder modellen slik den faktisk brukes.
#
# 20 år ga best treff i tilbaketesten (prøvd: 15, 18, 20, 22, 25, 30 og hele
# historikken). Forskjellen mellom 18 og 25 er liten; 15 er klart dårligere.
VINDU_AAR = 20


# ---------------------------------------------------------------------------
# 3. HORISONTER
# ---------------------------------------------------------------------------

# Antall år frem vi rapporterer gjennomsnittlig årlig vekst (CAGR) for.
HORISONTER_AR = [1, 3, 5, 10]


# ---------------------------------------------------------------------------
# 4. SESONGMØNSTER
# ---------------------------------------------------------------------------

# Indeksen hopper i bestemte måneder, særlig i mai (lønnsoppgjør) og januar
# (prisjusteringer). Mønsteret har endret seg kraftig: mai-hoppet var rundt
# 0,5 prosent før 2020 og 3 prosent i 2026.
#
# Modellen lar derfor mønsteret følge med i tiden. Sesongen for et gitt år er
# et vektet snitt av samme måned i årene rundt, der nære år teller mest.
# Halveringstiden sier hvor fort vekten faller: med 5 teller et år som ligger
# fem år unna halvparten så mye som året selv.
#
# Lavere tall = mønsteret tilpasser seg raskere, men blir mer støyete.
SESONG_HALVERINGSTID = 5


# ---------------------------------------------------------------------------
# 5. LANGSIKTIG ANKER
# ---------------------------------------------------------------------------

# Ankeret er et forhåndssyn på langsiktig kostnadsvekst:
#     Norges Banks inflasjonsmål + antatt realvekst i byggekostnader.
#
# Modellen veier ankeret sammen med snittveksten i dataene. Det mest presise
# av de to får mest vekt. Vekten er den samme på alle horisonter, og den står
# i rapporten («Vekt på dataene»). Med standardinnstillingene veier ankeret
# rundt 10-15 prosent. Det flytter altså anslaget litt, ikke mye.
#
# Inflasjonsmålet har endret seg, og tilbaketesten bruker det målet som gjaldt
# på hvert tidspunkt. Før mars 2001 fantes det ikke noe mål, og da bruker
# modellen bare dataene.
INFLASJONSMAL = [
    ("2001M03", 0.025),
    ("2018M03", 0.020),
]

# Antatt realvekst i byggekostnader utover konsumprisveksten, per år.
# Juster hvis du har et annet syn.
REALVEKST_ANTATT = 0.010

# Hvor sikker er du på ankeret? Dette er standardavviket rundt ankeret, i
# årlige prosentpoeng. 0.010 betyr "jeg er rimelig sikker på at den sanne
# langsiktige veksten ligger innenfor ett prosentpoeng fra ankeret".
#
# Lavere tall gir ankeret mer vekt. I tilbaketesten ga mer vekt på ankeret
# (0.005 og 0.003) litt dårligere treff, ikke bedre. Setter du 0, slås
# ankeret helt av.
ANKER_USIKKERHET = 0.010


# ---------------------------------------------------------------------------
# 6. SIMULERING
# ---------------------------------------------------------------------------

# Antall simulerte baner. 10 000 gir stabile persentiler og tar noen sekunder.
ANTALL_BANER = 10000

# Lengden på blokkene i blokk-bootstrappen, i måneder.
# Vi trekker sammenhengende biter av historiske avvik i stedet for
# enkeltmåneder, slik at simuleringene arver den virkelige seriens egenskaper:
# at sjokk henger igjen, og at urolige perioder kommer i klynger (som 2021-22).
BLOKK_LENGDE = 18

# Fast startverdi for tilfeldighetsgeneratoren, slik at to kjøringer på samme
# data gir nøyaktig samme svar. Uten dette ville tallene flakke litt hver måned
# uten at noe faktisk hadde endret seg.
TILFELDIG_FRO = 20260101

# Persentilene som vises i viftediagrammene.
BAND = [50, 80, 95]

# Hvor hardt modellen straffes for å lese for mye ut av de siste månedene.
# 0 = ingen straff (anslagene hopper rundt hver måned), 1 = kraftig demping.
# 0.30 er en fornuftig middelvei. Tilbaketesten skiller knapt mellom 0.1 og 1.
RIDGE = 0.30


# ---------------------------------------------------------------------------
# 7. TILBAKETESTING
# ---------------------------------------------------------------------------

# Slår tilbaketesten av eller på. Den tar 1-3 minutter.
KJOR_TILBAKETEST = True

# Første startpunkt for tilbaketesten. Modellen later som om den står her,
# lager prognose, og sammenligner med hva som faktisk skjedde.
TILBAKETEST_FRA = "1995M01"

# Hvor ofte vi setter et nytt startpunkt (i måneder). 1 = hver eneste måned.
#
# Tettere sampling gir et roligere anslag på treffsikkerheten, men tilfører
# ikke ny informasjon: to nabovinduer deler elleve av tolv måneder.
TILBAKETEST_STEG = 1

# Færre baner i tilbaketesten for å holde kjøretiden nede.
TILBAKETEST_BANER = 1500

# Hvor mange år frem de historiske prognosene i hovedgrafen skal vises.
TILBAKETEST_VIS_AAR = 5

# Hvor mange slike historiske prognoser som tegnes (ett panel hver).
TILBAKETEST_VIS_ANTALL = 4


# ---------------------------------------------------------------------------
# 8. RAPPORT
# ---------------------------------------------------------------------------

RAPPORT_TITTEL = "Byggekostnadsindeks boligblokk"
RAPPORT_UNDERTITTEL = "Prognose for gjennomsnittlig årlig kostnadsvekst"

# Vises nederst på siden.
KONTAKT = ""
