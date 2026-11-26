"""Bygger dashboardet «Anløpsrisiko vind, sjø og ising».

Henter AIS (BarentsWatch), målt vind (PortWind), værvarsel og bølger (MET Norway),
ensemble-trend og bølger lenger fram (ECMWF via Open-Meteo), beregner risiko per anløp
og skriver site/index.html, site/state.json og site/meta.json.

Kjør:  python src/update.py
Miljøvariabler (alle valgfrie, men AIS krever BW_*):
  BW_CLIENT_ID, BW_CLIENT_SECRET   BarentsWatch AIS-klient
  MET_USER_AGENT                   identifikasjon mot api.met.no (påkrevd av MET)
  OPEN_METEO_API_KEY               nøkkel for kommersiell bruk av Open-Meteo
  SITE_URL                         publisert adresse, brukes til å hente forrige tilstand
"""
import datetime as dt
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config"
ASSETS = ROOT / "assets"
SITE = ROOT / "site"
OSLO = ZoneInfo("Europe/Oslo")
UTC = dt.timezone.utc
SEC = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
SEC16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
WD = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]

UA = os.environ.get("MET_USER_AGENT") or "anlopsrisiko-dashboard/1.0 (github actions)"
OM_KEY = os.environ.get("OPEN_METEO_API_KEY", "").strip()
S = requests.Session()
S.headers["User-Agent"] = UA
WARN = []


def load(name):
    return json.loads((CFG / name).read_text(encoding="utf-8"))


PORTS = load("ports.json")
LIMS = load("limits.json")
TT = load("timetable.json")
STR = load("stretches.json")
PWS = load("portwind_stations.json")
SST0 = load("sst_fallback.json")
SHIPS = load("ships.json")
MP = json.loads((ASSETS / "mp.json").read_text(encoding="utf-8"))
AFTER = {"N": {v["N_after"]: k for k, v in STR.items()}, "S": {v["S_after"]: k for k, v in STR.items()}}

NOW = dt.datetime.now(UTC).replace(second=0, microsecond=0)
NOWMS = int(NOW.timestamp() * 1000)
DET_END = NOW + dt.timedelta(hours=72)
END = NOW + dt.timedelta(days=11)
H = 3600_000


def ms(t):
    return int(t.timestamp() * 1000)


def sector(deg):
    return int(round((deg % 360) / 45)) % 8


def sec16(deg):
    return SEC16[int(round((deg % 360) / 22.5)) % 16]


def ppr(v, ta, tw):
    return v * (-1.7 - ta) / (1 + 0.3 * (tw + 1.7))


def ice_lvl(p):
    return 3 if p >= 53.3 else 2 if p >= 22.4 else 1 if p > 0 else 0


def r1(x):
    return None if x is None else round(float(x), 1)


def get_json(url, params=None, tries=3, **kw):
    for k in range(tries):
        try:
            r = S.get(url, params=params, timeout=60, **kw)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(3 * (k + 1))
                continue
            r.raise_for_status()
        except requests.RequestException:
            if k == tries - 1:
                raise
            time.sleep(3 * (k + 1))
    raise RuntimeError(f"Ingen svar fra {url}")


# ---------------------------------------------------------------- ruteplan
def voyage(lst, start):
    """lst: [[havn, ankomst, avgang]] lokal tid; start: dato for første avgang. Gir UTC-ms."""
    day, prev, out = 0, None, []
    for c, a, d in lst:
        r = []
        for x in (a, d):
            if x is None:
                r.append(None)
                continue
            m = int(x[:2]) * 60 + int(x[3:])
            if prev is not None and m < prev:
                day += 1
            prev = m
            local = dt.datetime.combine(start + dt.timedelta(days=day), dt.time(int(x[:2]), int(x[3:])), OSLO)
            r.append(ms(local))
        out.append((c, r[0], r[1]))
    return out


def schedule():
    calls = []
    for s in SHIPS:
        if not s.get("ref_dir") or not s.get("ref_start"):
            continue
        oos = s.get("out_of_service")
        if oos and dt.date.fromisoformat(oos["until"]) >= NOW.astimezone(OSLO).date():
            continue
        d0, st = s["ref_dir"], dt.date.fromisoformat(s["ref_start"])
        # gå tilbake hele 11-døgns rundturer til før nå
        while st > (NOW.astimezone(OSLO).date() - dt.timedelta(days=12)):
            st -= dt.timedelta(days=11)
        legs, d = [], d0
        while st <= END.astimezone(OSLO).date() + dt.timedelta(days=1):
            legs.append((d, st))
            st = st + dt.timedelta(days=6 if d == "N" else 5)
            d = "S" if d == "N" else "N"
        for d, st in legs:
            v = voyage(TT[d], st)
            for k, (c, a, dd) in enumerate(v):
                t0, t1 = a or dd, dd or a
                if t1 >= NOWMS and t0 <= ms(END):
                    prev_dep = v[k - 1][2] if k else None
                    calls.append(dict(ship=s["name"], doc=s["doc"], dir=d, c=c, ta=a, td=dd, t0=t0, t1=t1,
                                      det=t0 <= ms(DET_END), prev_dep=prev_dep))
    return calls


# ---------------------------------------------------------------- MET
def met_location(code):
    _, lat, lon = PORTS[code]
    j = get_json("https://api.met.no/weatherapi/locationforecast/2.0/complete",
                 {"lat": round(lat, 4), "lon": round(lon, 4)})
    out = []
    for t in j["properties"]["timeseries"]:
        d = t["data"]["instant"]["details"]
        out.append(dict(t=ms(dt.datetime.fromisoformat(t["time"].replace("Z", "+00:00"))), ws=d.get("wind_speed"),
                        wd=d.get("wind_from_direction"), gu=d.get("wind_speed_of_gust"),
                        p90=d.get("wind_speed_percentile_90"), ta=d.get("air_temperature")))
    return out


def met_ocean(lat, lon):
    j = get_json("https://api.met.no/weatherapi/oceanforecast/2.0/complete", {"lat": round(lat, 4), "lon": round(lon, 4)})
    out = []
    for t in j["properties"]["timeseries"]:
        d = t["data"]["instant"]["details"]
        out.append(dict(t=ms(dt.datetime.fromisoformat(t["time"].replace("Z", "+00:00"))),
                        hs=d.get("sea_surface_wave_height"), wd=d.get("sea_surface_wave_from_direction"),
                        sst=d.get("sea_water_temperature")))
    return out


# ---------------------------------------------------------------- Open-Meteo
def om_url(kind):
    host = {"ens": "ensemble-api.open-meteo.com/v1/ensemble", "marine": "marine-api.open-meteo.com/v1/marine"}[kind]
    return ("https://customer-" + host) if OM_KEY else ("https://" + host)


def om_multi(kind, coords, params):
    p = dict(params, latitude=",".join(f"{c[0]:.3f}" for c in coords), longitude=",".join(f"{c[1]:.3f}" for c in coords),
             timezone="GMT")
    if OM_KEY:
        p["apikey"] = OM_KEY
    j = get_json(om_url(kind), p)
    return j if isinstance(j, list) else [j]


def om_times(h):
    return [ms(dt.datetime.fromisoformat(x).replace(tzinfo=UTC)) for x in h["time"]]


# ---------------------------------------------------------------- beregninger
def det_wind(call, fc):
    a = call["t0"] - H
    b = call["t1"] + (0 if call["ta"] and call["td"] else H)
    pts = [p for p in fc if a <= p["t"] <= b and p["ws"] is not None and p["wd"] is not None]
    if not pts:
        mid = (a + b) / 2
        pts = [min((p for p in fc if p["ws"] is not None), key=lambda p: abs(p["t"] - mid))]
    lim_tab = LIMS[call["doc"]][call["c"]]
    best = None
    for p in pts:
        si = sector(p["wd"])
        lim = lim_tab[si]
        r = p["ws"] / lim
        g = (p["gu"] / lim) if p["gu"] is not None else -1
        q = (p["p90"] / lim) if p["p90"] is not None else -1
        lvl = 0
        if r >= .70 or g >= 1.0:
            lvl = 1
        if r >= .85 or q >= 1.0:
            lvl = 2
        if r >= 1.0:
            lvl = 3
        score = lvl * 10 + r
        if best is None or score > best[0]:
            best = (score, lvl, p, SEC[si], lim)
    _, lvl, p, sec, lim = best
    return lvl, r1(p["ws"]), r1(p["gu"]), r1(p["p90"] if p["p90"] is not None else p["ws"]), sec, lim, pts


def det_ice(pts, tw):
    best = None
    for p in pts:
        if p["ta"] is None or p["ws"] is None:
            continue
        v = ppr(p["ws"], p["ta"], tw)
        if best is None or v > best[0]:
            best = (v, p)
    if not best or best[0] <= 0:
        return None
    v, p = best
    return ["D", ice_lvl(v), r1(v), r1(p["ta"]), r1(p["ws"])]


def wave_for(call, oc_met, oc_ec):
    name = AFTER[call["dir"]].get(call["c"])
    if not name or call["ta"] is None:
        return None
    a = call["prev_dep"] if call["prev_dep"] else call["ta"] - 3 * H
    b = call["ta"]
    best, src = None, set()
    m = oc_met.get(name) or []
    mlast = m[-1]["t"] if m else -1
    for p in m:
        if a - H <= p["t"] <= b and p["hs"] is not None:
            src.add("M")
            if best is None or p["hs"] > best[0]:
                best = (p["hs"], p["wd"])
    if b > mlast:
        e = oc_ec.get(name) or []
        for p in e:
            if max(a - H, mlast) < p["t"] <= b and p["hs"] is not None:
                src.add("E")
                if best is None or p["hs"] > best[0]:
                    best = (p["hs"], p["wd"])
    if best is None:
        return None
    s = "ME" if src == {"M", "E"} else src.pop()
    return [name, r1(best[0]), SEC[sector(best[1])] if best[1] is not None else "N", s]


def trend(call, ens, tw):
    E = ens[call["c"]]
    a, b = call["t0"] - 6 * H, call["t1"] + 6 * H
    idx = [k for k, t in enumerate(E["t"]) if a <= t <= b]
    lim_tab = LIMS[call["doc"]][call["c"]]
    res, ice = [], []
    ia, ib = call["t0"] - 2 * H, call["t1"] + H
    for mem in E["m"]:
        ws, wd, ta = mem
        best = None
        for k in idx:
            if ws[k] is None or wd[k] is None:
                continue
            si = sector(wd[k])
            r = ws[k] / lim_tab[si]
            if best is None or r > best[0]:
                best = (r, ws[k], si)
        if best:
            res.append(best)
        pm, tmin = None, None
        for k in idx:
            if not (ia <= E["t"][k] <= ib) or ws[k] is None or ta[k] is None:
                continue
            v = ppr(ws[k], ta[k], tw)
            pm = v if pm is None else max(pm, v)
            tmin = ta[k] if tmin is None else min(tmin, ta[k])
        if pm is not None:
            ice.append((pm, tmin))
    if not res:
        return None, None
    n = len(res)
    p100 = round(100 * sum(r[0] >= 1 for r in res) / n)
    p85 = round(100 * sum(r[0] >= .85 for r in res) / n)
    w = sorted(r[1] for r in res)
    cnt = [0] * 8
    for r in res:
        cnt[r[2]] += 1
    si = cnt.index(max(cnt))
    dot = 2 if (p100 >= 25 or p85 >= 40) else 1 if p85 >= 15 else 0
    wind = [dot, r1(w[n // 2]), r1(w[int(n * .9)]), p100, p85, SEC[si], lim_tab[si]]
    icer = None
    if ice:
        m = len(ice)
        pl = round(100 * sum(x[0] > 0 for x in ice) / m)
        pmod = round(100 * sum(x[0] >= 22.4 for x in ice) / m)
        ph = round(100 * sum(x[0] >= 53.3 for x in ice) / m)
        if pl > 0:
            s_ = sorted(x[0] for x in ice)
            tm = sorted(x[1] for x in ice)
            lvl = 3 if ph >= 20 else 2 if pmod >= 20 else 1 if pl >= 50 else 0
            icer = ["T", lvl, pl, pmod, r1(s_[m // 2]), r1(s_[int(m * .9)]), r1(tm[m // 2])]
    return wind, icer


# ---------------------------------------------------------------- AIS
def ais(prev):
    cid, sec_ = os.environ.get("BW_CLIENT_ID"), os.environ.get("BW_CLIENT_SECRET")
    if not cid or not sec_:
        WARN.append("BW_CLIENT_ID/BW_CLIENT_SECRET mangler: bruker forrige AIS-posisjon.")
        return prev
    tok = requests.post("https://id.barentswatch.no/connect/token", timeout=60, data=dict(
        client_id=cid, client_secret=sec_, scope="ais", grant_type="client_credentials")).json()["access_token"]
    hdr = {"Authorization": f"Bearer {tok}"}
    want = {s["mmsi"]: s["name"] for s in SHIPS}
    data = None
    try:
        r = S.post("https://live.ais.barentswatch.no/v1/latest/combined", headers=hdr, timeout=60,
                   json={"mmsi": list(want)})
        if r.status_code == 200:
            data = r.json()
    except requests.RequestException:
        pass
    if not data:
        data = S.get("https://live.ais.barentswatch.no/v1/latest/combined", headers=hdr, timeout=120).json()
    out = dict(prev.get("ships", {})) if prev else {}
    newest = None
    for v in data:
        name = want.get(v.get("mmsi"))
        if not name or v.get("latitude") is None:
            continue
        t = v.get("msgtime")
        out[name] = dict(lat=round(v["latitude"], 5), lon=round(v["longitude"], 5),
                         cog=v.get("courseOverGround"), sog=v.get("speedOverGround") or 0, t=t)
        if t:
            tt = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
            newest = tt if newest is None or tt > newest else newest
    missing = [n for n in want.values() if n not in out]
    if missing:
        WARN.append("Ingen AIS for: " + ", ".join(missing))
    return dict(t=(newest or NOW).astimezone(UTC).isoformat().replace("+00:00", "Z"), ships=out)


# ---------------------------------------------------------------- PortWind
def portwind(prev):
    import socketio  # python-socketio
    cur, mx = {}, {}
    sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)

    def take(obj, into):
        if not isinstance(obj, dict):
            return
        for sid, v in obj.items():
            d = v.get("data") if isinstance(v, dict) else None
            if isinstance(d, dict) and d.get("VH_mps") not in (None, "", "empty"):
                into[sid] = d

    sio.on("table1sCache", lambda o: take(o, cur))
    sio.on("table1s", lambda o: take(o, cur))
    sio.on("max", lambda o: take(o, mx))
    try:
        sio.connect("https://portwind.no", socketio_path="socket.io", transports=["websocket", "polling"],
                    headers={"User-Agent": UA}, wait_timeout=20)
        sio.sleep(40)
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass
    if not cur:
        WARN.append("PortWind ga ingen data: bruker forrige måling.")
        return prev
    pprev = {s["id"]: s for L in (prev or {}).get("ports", {}).values() for s in L}
    ports, newest = {}, None
    for code, lst in PWS.items():
        for sid, name in lst:
            d = cur.get(sid)
            if not d:
                continue
            t = dt.datetime.fromisoformat(d["TmStamp"])
            if NOW - t > dt.timedelta(hours=2):
                continue
            newest = t if newest is None or t > newest else newest
            ws, dg = float(d["VH_mps"]), float(d["VR_grader"])
            e = dict(id=sid, name=name, dir=sec16(dg), ws=round(ws, 1))
            m = mx.get(sid)
            p = pprev.get(sid)
            if m:
                e.update(mdir=sec16(float(m["VR_grader"])), max=round(float(m["VH_mps"]), 1),
                         mt=dt.datetime.fromisoformat(m["TmStamp"]).astimezone(OSLO).strftime("%H:%M:%S"))
            elif p and p.get("max") is not None and prev and \
                    NOW - dt.datetime.fromisoformat(prev["t"].replace("Z", "+00:00")) < dt.timedelta(minutes=70):
                e.update(mdir=p["mdir"], max=p["max"], mt=p["mt"])
            else:
                e.update(mdir=e["dir"], max=e["ws"], mt=t.astimezone(OSLO).strftime("%H:%M:%S"))
            if e["max"] < e["ws"]:
                e.update(mdir=e["dir"], max=e["ws"], mt=t.astimezone(OSLO).strftime("%H:%M:%S"))
            ports.setdefault(code, []).append(e)
    return dict(t=(newest or NOW).astimezone(UTC).isoformat().replace("+00:00", "Z"), source="PortWind (portwind.no)",
                ports=ports)


# ---------------------------------------------------------------- overskrift
def headline(rows):
    """Kort oppsummering av det viktigste i perioden."""
    items = []
    for r in rows:
        t = r[3] or r[4]
        day = WD[dt.datetime.fromtimestamp(t / 1000, OSLO).weekday()]
        name = PORTS[r[2]][0]
        if r[5] == "D" and r[6] >= 2:
            items.append((10 + r[6], t, f"{'vind over grensen' if r[6] == 3 else 'sterk vind'} i {name} {day}"))
        if r[5] == "T" and r[6] == 2:
            items.append((9, t, f"mulig uvær ved {name} {day}"))
        w = r[-2]
        if w and w[1] >= 4:
            items.append((12 if w[1] >= 6 else 11, t, f"{'svært grov' if w[1] >= 6 else 'grov'} sjø ved {w[0]} {day}"))
        ic = r[-1]
        if ic and ic[1] >= 2:
            items.append((10 + ic[1] - 1, t, f"{'kraftig' if ic[1] >= 3 else 'moderat'} ising ved {name} {day}"))
    if not items:
        return "Ingen anløp med forhøyet risiko i perioden"
    items.sort(key=lambda x: (-x[0], x[1]))
    seen, out = set(), []
    for _, _, txt in items:
        key = txt.rsplit(" ", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(txt)
        if len(out) == 3:
            break
    s = ", ".join(out[:-1]) + (" og " if len(out) > 1 else "") + out[-1]
    return s[0].upper() + s[1:]


# ---------------------------------------------------------------- hovedløp
def prev_state():
    url = os.environ.get("SITE_URL", "").rstrip("/")
    if url:
        try:
            return S.get(url + "/state.json", timeout=30).json()
        except Exception:
            pass
    p = SITE / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def main():
    prev = prev_state()
    calls = schedule()
    print(f"{len(calls)} anløp, {sum(c['det'] for c in calls)} detaljert", flush=True)

    # AIS og PortWind (feil her stopper ikke resten)
    try:
        pos = ais(prev.get("positions") or {})
    except Exception as e:
        traceback.print_exc()
        WARN.append(f"AIS feilet: {e}")
        pos = prev.get("positions") or {}
    try:
        pw = portwind(prev.get("portwind") or {})
    except Exception as e:
        traceback.print_exc()
        WARN.append(f"PortWind feilet: {e}")
        pw = prev.get("portwind") or {}

    # skip uten AIS: plasser ved siste havn etter ruteplanen (eller fast posisjon fra ships.json)
    pos = dict(pos or {})
    pos.setdefault("ships", {})
    for s in SHIPS:
        if s["name"] in pos["ships"]:
            continue
        past = [c for c in calls if c["ship"] == s["name"] and c["t0"] <= NOWMS]
        nxt = [c for c in calls if c["ship"] == s["name"]]
        cc = max(past, key=lambda c: c["t0"])["c"] if past else (min(nxt, key=lambda c: c["t0"])["c"] if nxt else None)
        if s.get("fallback_pos"):
            lat, lon = s["fallback_pos"]
        elif cc:
            lat, lon = PORTS[cc][1], PORTS[cc][2]
        else:
            continue
        pos["ships"][s["name"]] = dict(lat=lat, lon=lon, cog=None, sog=0, t=None, estimated=True)
        WARN.append(f"{s['name']}: ingen AIS, vist ved {'fast posisjon' if s.get('fallback_pos') else PORTS[cc][0]}.")

    # MET per havn
    need = sorted({c["c"] for c in calls if c["det"]})
    fc = {}
    for c in need:
        fc[c] = met_location(c)
    # sjøtemperatur nær havnene og bølger på strekningene
    sst = {}
    for c in PORTS:
        try:
            o = met_ocean(MP[c][2], MP[c][3])
            sst[c] = next((p["sst"] for p in o if p["sst"] is not None), SST0.get(c, 6.0))
        except Exception:
            sst[c] = SST0.get(c, 6.0)
    oc_met = {}
    for name, v in STR.items():
        try:
            oc_met[name] = met_ocean(v["lat"], v["lon"])
        except Exception as e:
            WARN.append(f"MET bølger {name}: {e}")
    names = list(STR)
    oc_ec = {}
    try:
        mj = om_multi("marine", [(STR[n]["lat"], STR[n]["lon"]) for n in names],
                      {"hourly": "wave_height,wave_direction", "models": "ecmwf_wam025", "forecast_days": 12})
        for n, j in zip(names, mj):
            ts = om_times(j["hourly"])
            oc_ec[n] = [dict(t=t, hs=h, wd=d) for t, h, d in zip(ts, j["hourly"]["wave_height"], j["hourly"]["wave_direction"])]
    except Exception as e:
        WARN.append(f"ECMWF-bølger feilet: {e}")

    # ensemble for trend
    ens = {}
    codes = sorted({c["c"] for c in calls if not c["det"]})
    for k in range(0, len(codes), 10):
        part = codes[k:k + 10]
        js = om_multi("ens", [(PORTS[c][1], PORTS[c][2]) for c in part],
                      {"hourly": "wind_speed_10m,wind_direction_10m,temperature_2m", "models": "ecmwf_ifs025",
                       "forecast_days": 12, "wind_speed_unit": "ms"})
        for c, j in zip(part, js):
            h = j["hourly"]
            keys = [x for x in h if x.startswith("wind_speed_10m")]
            mem = []
            for ks in keys:
                suf = ks[len("wind_speed_10m"):]
                mem.append((h[ks], h.get("wind_direction_10m" + suf), h.get("temperature_2m" + suf)))
            ens[c] = dict(t=om_times(h), m=[m for m in mem if m[1] is not None and m[2] is not None])

    rows = []
    for c in calls:
        w = wave_for(c, oc_met, oc_ec)
        base = [c["ship"], c["dir"], c["c"], c["ta"], c["td"]]
        if c["det"]:
            lvl, ws, gu, p90, sec, lim, pts = det_wind(c, fc[c["c"]])
            rows.append(base + ["D", lvl, ws, gu, p90, sec, lim, w, det_ice(pts, sst[c["c"]])])
        else:
            if c["c"] not in ens:
                continue
            wind, icer = trend(c, ens, sst[c["c"]])
            if wind is None:
                continue
            rows.append(base + ["T"] + wind + [w, icer])

    # skriv side
    gen = NOWMS
    gtxt = NOW.astimezone(OSLO).strftime("%d.%m. kl. %H:%M")
    ships = (pos or {}).get("ships", {})
    aisjs = "{" + ",".join(
        f'{json.dumps(n, ensure_ascii=False)}:{{lat:{v["lat"]},lon:{v["lon"]},cog:{"null" if v.get("cog") is None or (v.get("sog") or 0) < 1 else v["cog"]},sog:{v.get("sog") or 0}}}'
        for n, v in ships.items()) + "}"
    aist = (pos or {}).get("t")
    off = {s["name"]: s["out_of_service"]["text"] for s in SHIPS
           if s.get("out_of_service") and dt.date.fromisoformat(s["out_of_service"]["until"]) >= NOW.astimezone(OSLO).date()}
    m = json.loads((ASSETS / "map.json").read_text(encoding="utf-8"))
    t = (ASSETS / "template.html").read_text(encoding="utf-8")
    rep = {
        "__GENTXT__": json.dumps(gtxt, ensure_ascii=False), "__GEN__": str(gen),
        "__OFF__": json.dumps(off, ensure_ascii=False), "__AIST__": f'Date.parse({json.dumps(aist)})' if aist else str(gen),
        "__AIS__": aisjs, "__PWD__": json.dumps(pw or {"t": None, "ports": {}}, ensure_ascii=False, separators=(",", ":")),
        "__HEAD__": json.dumps(headline(rows), ensure_ascii=False),
        "__MP__": json.dumps(MP, ensure_ascii=False, separators=(",", ":")),
        "__DATA__": json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        "__MW__": str(m["W"]), "__MH__": str(m["H"]),
    }
    for k in ["__GENTXT__", "__GEN__", "__OFF__", "__AIST__", "__AIS__", "__PWD__", "__HEAD__", "__MP__", "__DATA__",
              "__MW__", "__MH__"]:
        t = t.replace(k, rep[k])
    if ships and all(v.get("estimated") for v in ships.values()):
        t = t.replace('AIS fra BarentsWatch ${fdate(AIST)} kl. ${hm(AIST)} (${age < 120 ? age + " min" : Math.round(age/60) + " t"} siden)',
                      "Ingen AIS ennå (BarentsWatch-nøkkel mangler): skipene er vist ved havnen etter ruteplanen")
    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(t, encoding="utf-8")
    (SITE / "state.json").write_text(json.dumps(dict(positions=pos, portwind=pw), ensure_ascii=False), encoding="utf-8")
    (SITE / "meta.json").write_text(json.dumps(dict(gen=gen, txt=gtxt, rows=len(rows), warnings=WARN), ensure_ascii=False),
                                    encoding="utf-8")
    (SITE / ".nojekyll").write_text("")
    print(f"Ferdig: {len(rows)} rader. {gtxt}", flush=True)
    for w in WARN:
        print("ADVARSEL:", w, flush=True)


if __name__ == "__main__":
    sys.exit(main())
