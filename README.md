# Anløpsrisiko vind, sjø og ising

Dashboard for Hurtigrutens anløp: vind mot skipenes vindgrenser (UniSea IMS NO-0040, NO-0041 og NO-0044), bølger på åpne havstrekninger (NO-2068) og fare for ising, 11 døgn fram. Siden bygges automatisk hver time av GitHub Actions og publiseres med GitHub Pages.

## Datakilder

AIS fra Kystverket via BarentsWatch, målt vind fra portwind.no, værvarsel og bølger fra MET Norway (Locationforecast og Oceanforecast, lisens CC BY 4.0), og ECMWF-ensemble og ECMWF WAM via Open-Meteo for dag 4–11.

## Oppsett (én gang)

1. Gå til Settings → Pages og velg «GitHub Actions» under Source.
2. Lag en AIS-klient hos BarentsWatch (developer.barentswatch.no, «Min side», ny klient av typen AIS). Legg klient-ID og passord inn under Settings → Secrets and variables → Actions som `BW_CLIENT_ID` og `BW_CLIENT_SECRET`.
3. Kjør første oppdatering under Actions → «Oppdater dashboard» → «Run workflow». Adressen til siden står i resultatet.

Uten BarentsWatch-nøkkel virker alt annet, men skipene vises ved siste havn etter ruteplanen i stedet for faktisk AIS-posisjon.

Open-Meteo er gratis bare for ikke-kommersiell bruk. Ved kommersiell bruk legges en nøkkel inn som `OPEN_METEO_API_KEY`, så brukes betalingsløsningen automatisk.

## Vedlikehold

All oppsettinformasjon ligger i `config/`:

`ships.json` har skip, MMSI, vindgrensedokument og et ankerpunkt i ruteplanen (`ref_dir` og `ref_start`: retning og dato for avgang fra Bergen (N) eller Kirkenes (S) på en kjent rundtur). Skip ute av drift får `out_of_service` med dato og tekst. `timetable.json` er ruteplanen, `limits.json` vindgrensene per havn og retning (N, NØ, Ø, SØ, S, SV, V, NV), `stretches.json` havstrekningene og `portwind_stations.json` vindmålerne per havn.

Endringer i disse filene slår inn ved neste timelige kjøring.

GitHub stopper planlagte kjøringer i offentlige prosjekter etter 60 dager uten aktivitet i prosjektet. Du får e-post før det skjer, og kan starte dem igjen med ett klikk.
