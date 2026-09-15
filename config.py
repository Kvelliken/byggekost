"""
Innstillinger for byggekostnadsmodellen.

Dette er den eneste filen du normålt trenger å endre.
Alt er kommentert. Endre tall, lagre, og modellen bruker de nye verdiene
neste gang den kjører.
"""

# ---------------------------------------------------------------------------
# 1. DATAKILDE (SSB Statistikkbanken, PxWebApi v2)
# ---------------------------------------------------------------------------

# Tabell 08655 = Byggekostnadsindeks for boligblokk, 2015=100, fra 1978M01.
TABELL = "08655"

# Koden for serien vi modellerer. "20" er koden SSB selv bruker i sitt
# API-eksempel for denne tabellen (samlet indeks).
#
# Første gang du kjører modellen skriver den ut HELE kodelisten i loggen,
# under overskriften "TILGJENGELIGE KODER". Sjekk der at koden nedenfor
# peker på den serien du vil ha, og endre hvis ikke.
ARBEIDSTYPE_KODE = "20"

# Første måned vi henter. Serien starter 1978M01.
DATA_FRA = "1978M01"


# ---------------------------------------------------------------------------
# 2. ESTIMERINGSVINDU
# ---------------------------------------------------------------------------

# Hvilken periode modellen laerer av.
#
# Hvorfor ikke hele historikken fra 1978? Fordi 1978-1990 var et helt annet
# pengepolitisk regime, med inflasjon rundt 8-10 prosent. Tar du med de årene
# får du en langsiktig drift som ikke har noe med dagens Norge a gjøre.
#
# 1999 er valgt fordi inflasjonsmalet kom i 2001 og disinflasjonen var
# fullført for det. Vil du teste følsomheten: prøv "1990M01" og "2010M01"
# og se hvor mye anslagene flytter seg.
ESTIMERING_FRA = "1999M01"


# ---------------------------------------------------------------------------
# 3. HORISONTER
# ---------------------------------------------------------------------------

# Antall år frem vi rapporterer gjennomsnittlig årlig vekst (CAGR) for.
HORISONTER_AR = [1, 3, 5, 10]


# ---------------------------------------------------------------------------
# 4. LANGSIKTIG ANKER
# ---------------------------------------------------------------------------

# På 10 års sikt har ingen tidsseriemodell reell informasjon. Det eneste
# ærlige anslaget er "inflasjonsmal + historisk realvekst i byggekostnader".
# Modellen blander derfor det den finner i dataene med dette ankeret, og
# vektlegger ankeret mer jo lenger frem vi ser.
#
# ANKER_AARLIG = Norges Banks inflasjonsmal (2,0 %) + antatt realvekst i
# byggekostnader (ca. 1,0 prosentpoeng). Juster hvis du har et annet syn.
ANKER_AARLIG = 0.030

# Hvor sikker er du på ankeret? Dette er standardavviket rundt ANKER_AARLIG,
# i årlige prosentpoeng. 0.010 betyr "jeg er rimelig sikker på at den sanne
# langsiktige driften ligger mellom 2 og 4 prosent".
#
# Dette tallet styrer i praksis bredden på 10-årsintervallet. Setter du det
# lavt får du falsk presisjon. Ikke ga under 0.006.
ANKER_USIKKERHET = 0.010


# ---------------------------------------------------------------------------
# 5. SIMULERING
# ---------------------------------------------------------------------------

# Antall simulerte baner. 10 000 gir stabile persentiler og tar noen sekunder.
ANTALL_BANER = 10000

# Lengden på blokkene i blokk-bootstrappen, i måneder.
# Vi trekker sammenhengende biter av historiske avvik i stedet for enkeltmaneder,
# slik at simuleringene arver den virkelige seriens egenskaper: at sjokk henger
# igjen, og at urolige perioder kommer i klynger (som 2021-2022).
BLOKK_LENGDE = 18

# Fast startverdi for tilfeldighetsgeneratoren, slik at to kjøringer på samme
# data gir nøyaktig samme svar. Uten dette ville tallene flakke litt hver måned
# uten at noe faktisk hadde endret seg.
TILFELDIG_FRO = 20260101

# Persentilene som vises i viftediagrammene.
BAND = [50, 80, 95]

# Hvor hardt modellen straffes for å lese for mye ut av de siste månedene.
# 0 = ingen straff (anslagene hopper rundt hver måned), 1 = kraftig demping
# (modellen ignorerer nåsituasjonen og gir deg bare langtidssnittet).
# 0.30 er en fornuftig middelvei. Endre bare hvis du har testet effekten.
RIDGE = 0.30


# ---------------------------------------------------------------------------
# 6. TILBAKETESTING
# ---------------------------------------------------------------------------

# Slar tilbaketesten av eller på. Den tar 1-3 minutter.
KJOR_TILBAKETEST = True

# Første startpunkt for tilbaketesten. Modellen later som om den står her,
# lager prognose, og sammenligner med hva som faktisk skjedde.
TILBAKETEST_FRA = "1995M01"

# Hvor ofte vi setter et nytt startpunkt (i måneder). 6 = to ganger i året.
TILBAKETEST_STEG = 6

# Færre baner i tilbaketesten for å holde kjøretiden nede.
TILBAKETEST_BANER = 1500


# ---------------------------------------------------------------------------
# 7. RAPPORT
# ---------------------------------------------------------------------------

RAPPORT_TITTEL = "Byggekostnadsindeks boligblokk"
RAPPORT_UNDERTITTEL = "Prognose for gjennomsnittlig årlig kostnadsvekst"

# Vises nederst på siden.
KONTAKT = ""
