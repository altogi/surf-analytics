# /// script
# requires-python = ">=3.10"
# dependencies = ["fitdecode", "pandas", "numpy"]
# ///
"""Surf session analysis from Garmin "Open Water Swimming" FIT exports.

Usage (from this folder):
    uv run surf.py            # process every export in this folder, write data/ and report.html
    uv run surf.py --debug    # also print every speed burst with its classification

Drop new Garmin exports (the .zip from "Export File", or a .fit) into this folder and re-run.

How it works
------------
Every second of the session gets a GPS position, a speed and a stroke count.
Surfing leaves a very recognisable signature in that data:

  * sitting in the lineup  -> speed ~0, no strokes
  * paddling (out / around)-> 0.4-1.2 m/s, steady strokes (20-35 /min)
  * riding a wave          -> a short burst well above paddling speed, heading to shore,
                              and the strokes stop (you're standing up)
  * catch attempt          -> a sprint of strokes that never turns into a ride

Caveat: in Open Water mode the watch filters GPS for swimming speeds, so the speeds below
understate real board speed (rides typically show as 1.5-3 m/s peaks). They are still very
consistent between sessions, so they're fine for comparing yourself over time.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import zipfile

import fitdecode
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RAW, DATA = os.path.join(HERE, "raw"), os.path.join(HERE, "data")

# ---- tunable thresholds -------------------------------------------------------------------
BURST_SPEED = 1.2        # m/s: a burst = contiguous seconds at or above this speed
RIDE_PEAK = 1.6          # m/s: a ride must reach at least this smoothed speed
RIDE_MIN_SECONDS = 5     # s at or above BURST_SPEED
RIDE_MIN_DIST = 8        # m covered during the burst
RIDE_MAX_STROKE_RATE = 12  # strokes/min while at speed (standing up => strokes stop)
SHORE_CONE_DEG = 80      # a ride must head within this angle of the shoreward direction
SIT_SPEED = 0.30         # m/s (30 s rolling mean) below this with no strokes => sitting
SIT_WINDOW = 30          # s
STROKE_FREE_WINDOW = 4   # s: a second is "stroke-free" if no stroke is counted within +/- this window/2
MAX_PLAUSIBLE_SPEED = 8  # m/s: faster GPS steps are glitches (usually at satellite lock)
# ------------------------------------------------------------------------------------------

SEMI = 180 / 2**31
R_EARTH = 6371000.0


def unpack_exports() -> list[str]:
    """Extract every *.zip export into raw/ and return the list of FIT files."""
    os.makedirs(RAW, exist_ok=True)
    for z in glob.glob(os.path.join(HERE, "*.zip")):
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".fit") and not os.path.exists(os.path.join(RAW, name)):
                    zf.extract(name, RAW)
    for f in glob.glob(os.path.join(HERE, "*.fit")):  # loose .fit files dropped in the folder
        dst = os.path.join(RAW, os.path.basename(f))
        if not os.path.exists(dst):
            os.replace(f, dst)
    return sorted(glob.glob(os.path.join(RAW, "*.fit")) + glob.glob(os.path.join(RAW, "*.FIT")))


def read_fit(path: str) -> tuple[pd.DataFrame, dict]:
    rows, meta = [], {}
    with fitdecode.FitReader(path) as fr:
        for m in fr:
            if not isinstance(m, fitdecode.FitDataMessage):
                continue
            if m.name == "record":
                d = {f.name: f.value for f in m.fields}
                lat, lon = d.get("position_lat"), d.get("position_long")
                rows.append(dict(
                    time=d["timestamp"],
                    lat=lat * SEMI if lat is not None else np.nan,
                    lon=lon * SEMI if lon is not None else np.nan,
                    garmin_speed=d.get("enhanced_speed"),
                    distance=d.get("distance"),
                    heart_rate=d.get("heart_rate"),
                    stroke_rate=d.get("cadence"),
                    total_strokes=d.get("total_cycles"),
                    temperature=d.get("temperature"),
                ))
            elif m.name == "session":
                d = {f.name: f.value for f in m.fields}
                meta.update(sport=str(d.get("sub_sport")), start=d.get("start_time"),
                            calories=d.get("total_calories"), avg_hr=d.get("avg_heart_rate"),
                            max_hr=d.get("max_heart_rate"))
    df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    return df, meta


def add_kinematics(df: pd.DataFrame) -> pd.DataFrame:
    df[["lat", "lon"]] = df[["lat", "lon"]].interpolate(limit=10, limit_direction="both")
    lat0 = np.radians(df.lat.mean())
    df["x"] = np.radians(df.lon) * math.cos(lat0) * R_EARTH  # local metres east
    df["y"] = np.radians(df.lat) * R_EARTH                   # local metres north
    dt = df.time.diff().dt.total_seconds()
    step = np.hypot(df.x.diff(), df.y.diff())
    raw = (step / dt).where((dt > 0) & (step / dt <= MAX_PLAUSIBLE_SPEED))  # drop GPS jumps
    step = step.where(raw.notna(), 0)
    df["speed"] = raw.rolling(3, center=True, min_periods=1).median().fillna(df.garmin_speed).fillna(0)
    df["step_m"] = step.fillna(0)
    strokes = df.total_strokes.ffill().fillna(0)
    df["strokes"] = strokes.diff().clip(lower=0).fillna(0)  # strokes taken in this second
    df["elapsed_s"] = (df.time - df.time.iloc[0]).dt.total_seconds()
    return df


def bearing(dx: float, dy: float) -> float:
    return (math.degrees(math.atan2(dx, dy)) + 360) % 360


def angdiff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def find_bursts(df: pd.DataFrame) -> list[dict]:
    fast = (df.speed >= BURST_SPEED).to_numpy()
    stroke_free = (df.strokes.rolling(STROKE_FREE_WINDOW + 1, center=True, min_periods=1).sum() == 0).to_numpy()
    bursts, i, n = [], 0, len(df)
    while i < n:
        if not fast[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and (fast[j + 1] or (j + 2 < n and fast[j + 2])):  # bridge 1-s dropouts
            j += 1
        # The ride is the longest stroke-free stretch of the burst (standing up => strokes stop).
        # Strokes before it are the paddle-in; strokes after it at speed are prone/whitewater.
        free = stroke_free[i:j + 1]
        best, run_start, k0, k1 = 0, None, None, None
        for k, f in enumerate(np.r_[free, False]):
            if f and run_start is None:
                run_start = k
            elif not f and run_start is not None:
                if k - run_start > best:
                    best, k0, k1 = k - run_start, i + run_start, i + k - 1
                run_start = None
        whole = df.iloc[i:j + 1]
        if k0 is None:  # never stopped stroking: score the whole burst as an attempt
            k0, k1 = i, j
        core = df.iloc[k0:k1 + 1]
        dx, dy = core.x.iloc[-1] - core.x.iloc[0], core.y.iloc[-1] - core.y.iloc[0]
        bursts.append(dict(
            i0=k0, i1=k1, start=core.time.iloc[0], elapsed_min=core.elapsed_s.iloc[0] / 60,
            seconds=len(core), peak_speed=core.speed.max(), avg_speed=core.speed.mean(),
            distance_m=float(core.step_m.iloc[1:].sum()),
            stroke_rate=float(core.strokes.sum() / len(core) * 60),
            burst_seconds=len(whole), burst_peak=whole.speed.max(),
            heading=bearing(dx, dy), straight_m=math.hypot(dx, dy),
            lat=core.lat.iloc[0], lon=core.lon.iloc[0], lat_end=core.lat.iloc[-1], lon_end=core.lon.iloc[-1],
        ))
        i = j + 1
    return bursts


def shore_bearing(bursts: list[dict]) -> float | None:
    """Rides go toward the beach, so the dominant direction of fast, stroke-free bursts is shoreward."""
    cand = [b for b in bursts if b["peak_speed"] >= RIDE_PEAK and b["stroke_rate"] <= RIDE_MAX_STROKE_RATE
            and b["straight_m"] >= RIDE_MIN_DIST]
    if not cand:
        return None
    sx = sum(b["straight_m"] * math.sin(math.radians(b["heading"])) for b in cand)
    cy = sum(b["straight_m"] * math.cos(math.radians(b["heading"])) for b in cand)
    return bearing(sx, cy)


def classify(bursts: list[dict], shore: float | None) -> None:
    for b in bursts:
        to_shore = shore is None or angdiff(b["heading"], shore) <= SHORE_CONE_DEG
        b["to_shore"] = to_shore
        reasons = []
        if b["peak_speed"] < RIDE_PEAK: reasons.append("slow")
        if b["seconds"] < RIDE_MIN_SECONDS: reasons.append("short")
        if b["distance_m"] < RIDE_MIN_DIST: reasons.append("little distance")
        if b["stroke_rate"] > RIDE_MAX_STROKE_RATE: reasons.append("still stroking")
        if not to_shore: reasons.append("not toward shore")
        if not reasons:
            b["kind"] = "ride"
        elif to_shore and b["stroke_rate"] > RIDE_MAX_STROKE_RATE:
            b["kind"] = "attempt"       # sprinting for a wave without standing up / riding it
        elif to_shore and set(reasons) <= {"short", "little distance", "slow"}:
            b["kind"] = "short"          # caught briefly, fell or kicked out early
        else:
            b["kind"] = "other"
        b["why_not"] = ", ".join(reasons)


def label_states(df: pd.DataFrame, bursts: list[dict]) -> pd.DataFrame:
    stroking = df.strokes.rolling(SIT_WINDOW, center=True, min_periods=1).sum() > 0
    slow = df.speed.rolling(SIT_WINDOW, center=True, min_periods=1).mean() < SIT_SPEED
    state = np.where(slow & ~stroking, "lineup", "paddling").astype(object)
    wave_id = np.zeros(len(df), dtype=int)
    k = 0
    for b in bursts:
        if b["kind"] == "ride":
            k += 1
            b["wave"] = k
            state[b["i0"]:b["i1"] + 1] = "riding"
            wave_id[b["i0"]:b["i1"] + 1] = k
    # paddling right after a ride until you next sit in the lineup = paddle-out
    after_ride = False
    for i, s in enumerate(state):
        if s == "riding":
            after_ride = True
        elif s == "lineup":
            after_ride = False
        elif after_ride:
            state[i] = "paddle_out"
    df["state"], df["wave"] = state, wave_id
    return df


def summarise(aid: str, df: pd.DataFrame, meta: dict, bursts: list[dict], shore: float | None) -> dict:
    rides = [b for b in bursts if b["kind"] == "ride"]
    secs = df.groupby("state").size().to_dict()
    dist = df.groupby("state").step_m.sum().to_dict()
    hours = df.elapsed_s.iloc[-1] / 3600
    return dict(
        activity_id=aid,
        date=df.time.iloc[0].strftime("%Y-%m-%d"),
        start_utc=df.time.iloc[0].strftime("%H:%M"),
        duration_min=round(df.elapsed_s.iloc[-1] / 60, 1),
        waves=len(rides),
        waves_per_hour=round(len(rides) / hours, 1) if hours else 0,
        short_rides=sum(b["kind"] == "short" for b in bursts),
        attempts=sum(b["kind"] == "attempt" for b in bursts),
        top_speed_ms=round(max((b["peak_speed"] for b in rides), default=0), 2),
        avg_ride_peak_ms=round(np.mean([b["peak_speed"] for b in rides]), 2) if rides else 0,
        longest_ride_m=round(max((b["distance_m"] for b in rides), default=0), 1),
        longest_ride_s=max((b["seconds"] for b in rides), default=0),
        total_ride_m=round(sum(b["distance_m"] for b in rides), 1),
        riding_min=round(secs.get("riding", 0) / 60, 1),
        paddle_out_min=round(secs.get("paddle_out", 0) / 60, 1),
        paddling_min=round(secs.get("paddling", 0) / 60, 1),
        lineup_min=round(secs.get("lineup", 0) / 60, 1),
        paddle_m=round(dist.get("paddling", 0) + dist.get("paddle_out", 0)),
        total_strokes=int(df.strokes.sum()),
        avg_hr=meta.get("avg_hr"), max_hr=meta.get("max_hr"), calories=meta.get("calories"),
        shore_bearing_deg=round(shore) if shore is not None else None,
        center_lat=round(df.lat.mean(), 5), center_lon=round(df.lon.mean(), 5),
    )


def process(path: str, debug: bool) -> tuple[dict, pd.DataFrame, list[dict]]:
    aid = os.path.basename(path).split("_")[0].split(".")[0]
    df, meta = read_fit(path)
    df = add_kinematics(df)
    bursts = find_bursts(df)
    shore = shore_bearing(bursts)
    classify(bursts, shore)
    df = label_states(df, bursts)
    s = summarise(aid, df, meta, bursts, shore)
    os.makedirs(DATA, exist_ok=True)
    cols = ["time", "elapsed_s", "lat", "lon", "speed", "garmin_speed", "heart_rate", "stroke_rate",
            "strokes", "distance", "temperature", "state", "wave"]
    df[cols].to_csv(os.path.join(DATA, f"{aid}_track.csv"), index=False)
    bdf = pd.DataFrame(bursts).drop(columns=["i0", "i1"], errors="ignore")
    if len(bdf):
        bdf.to_csv(os.path.join(DATA, f"{aid}_bursts.csv"), index=False)
    if debug:
        print(f"\n{s['date']}  {aid}  shore bearing {s['shore_bearing_deg']}°")
        for b in bursts:
            print(f"  {b['elapsed_min']:6.1f}min  {b['kind']:8s} peak {b['peak_speed']:.2f}  {b['seconds']:3d}s "
                  f"{b['distance_m']:5.1f}m (burst {b['burst_seconds']}s) strokes {b['stroke_rate']:4.0f}/min  hdg {b['heading']:3.0f}  {b['why_not']}")
    return s, df, bursts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--debug", action="store_true", help="print every speed burst and its classification")
    args = ap.parse_args()
    files = unpack_exports()
    if not files:
        raise SystemExit("No Garmin exports found. Drop the .zip (Export File) into this folder.")
    sessions, report = [], []
    for f in files:
        s, df, bursts = process(f, args.debug)
        sessions.append(s)
        report.append(dict(summary=s, track=df_for_report(df), bursts=bursts_for_report(bursts)))
    out = pd.DataFrame(sessions).sort_values("date")
    out.to_csv(os.path.join(HERE, "sessions.csv"), index=False)
    write_report(sorted(report, key=lambda r: r["summary"]["date"]))
    show = ["date", "duration_min", "waves", "waves_per_hour", "short_rides", "attempts", "top_speed_ms",
            "longest_ride_m", "longest_ride_s", "riding_min", "paddle_out_min", "lineup_min", "paddle_m"]
    print("\n" + out[show].to_string(index=False))
    print(f"\nWrote sessions.csv, data/*_track.csv, data/*_bursts.csv and index.html ({len(files)} sessions)")


def df_for_report(df: pd.DataFrame) -> dict:
    return dict(
        t=df.elapsed_s.round().astype(int).tolist(),
        lat=df.lat.round(6).tolist(), lon=df.lon.round(6).tolist(),
        v=df.speed.round(2).tolist(), hr=df.heart_rate.fillna(0).astype(int).tolist(),
        sr=df.stroke_rate.fillna(0).astype(int).tolist(), st=df.state.tolist(), w=df.wave.tolist(),
    )


def bursts_for_report(bursts: list[dict]) -> list[dict]:
    keep = ["kind", "wave", "elapsed_min", "seconds", "peak_speed", "avg_speed", "distance_m",
            "burst_seconds", "burst_peak", "stroke_rate", "heading", "lat", "lon", "lat_end", "lon_end", "why_not"]
    out = []
    for b in bursts:
        if b["kind"] in ("ride", "short", "attempt"):
            out.append({k: (round(v, 6) if isinstance(v, float) else v) for k, v in b.items() if k in keep})
    return out


def write_report(sessions: list[dict]) -> None:
    tpl = open(os.path.join(HERE, "report_template.html"), encoding="utf-8").read()
    html = tpl.replace("/*__DATA__*/null", json.dumps(sessions, default=str))
    with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)


if __name__ == "__main__":
    main()
