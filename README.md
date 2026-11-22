# Surf analysis

Turns Garmin **Open Water Swimming** recordings of surf sessions into surf stats.

## Adding a new session

1. On connect.garmin.com (web, not the phone app) open the activity → ⚙️ → **Export File**.
2. Drop the `.zip` into this folder.
3. Run:

```bash
cd ~/Downloads/surf_analysis && uv run surf.py
```

Then open `index.html` (published at GitHub Pages after you push). Add `--debug` to see every speed burst and why it was or wasn't counted as a wave.

## Outputs

| File | What it is |
|---|---|
| `index.html` | Maps, speed charts, wave list and trends across sessions |
| `sessions.csv` | One row per session: waves attempted / caught, catch rate, time waiting, distance paddled, max speed, wave style counts |
| `data/<id>_track.csv` | Every second: time, lat, lon, speed, heart rate, stroke rate, state, wave number |
| `data/<id>_bursts.csv` | Every fast burst with its classification (ride / short / attempt / other) |
| `raw/` | The unzipped FIT files |

## How waves are detected

- **Burst**: speed ≥ 1.2 m/s.
- **Ride**: the longest *stroke-free* part of a burst (you stop stroking when you stand up), reaching ≥ 1.6 m/s, lasting ≥ 5 s and covering ≥ 8 m, heading toward shore.
- **Shore direction** is inferred per session from the direction of the fast, stroke-free bursts, so it works at any break.
- **Short glide**: stroke-free and toward shore but too slow or short to count (small wave, fell early, whitewater push).
- **Attempt**: a fast sprint that never stopped stroking.
- **Lineup**: under 0.3 m/s average over 30 s with no strokes. **Paddle out**: paddling after a ride until you next sit in the lineup.

Thresholds are at the top of `surf.py`.

## Session questions

- **Attempted**: rides + short glides + sprints (every go at a wave). **Caught**: rides.
- **Waiting**: time sitting in the lineup. **Paddled**: distance while paddling (including paddling out). **Max speed**: top speed on a ride.

## Wave style

Each ride's speed (smoothed over 5 s) is compared across the ride:

- **Quick dive**: peak in the first 40% and the last third ≥10% (of peak) slower than the first, so you got ahead of the wave.
- **Late pop-up**: peak in the last 40% and the last third ≥10% faster, so you popped up late and the wave caught you after.
- **Balanced**: everything else.

Thresholds (`STYLE_*`) are at the top of `surf.py`.

**Speed caveat:** Open Water mode filters GPS for swimming speeds, so speeds understate real board speed. They're consistent between sessions, though, so trends are meaningful.
