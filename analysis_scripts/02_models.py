"""Capstone models M1-M6 for the OSBA Art Walk pilot (July 10-12, 2026).

Inputs (made by 01_extract_counts.py from the Art Walk Camera Comparison HTML reports):
  camday.csv  - one row per camera-day: date, camera, enter, exit, total, hours (recorded hours)
  camhour.csv - one row per camera-day-hour: date, camera, hour, count
Audit counts are typed in from Audit.xlsx (sheet "Original").
Run:  python 02_models.py
"""
import warnings
import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf
from scipy import stats
from statsmodels.stats.proportion import proportion_confint

warnings.filterwarnings("ignore")
np.random.seed(1)
DAY = {"2026-07-10": "Fri", "2026-07-11": "Sat", "2026-07-12": "Sun"}

# ---------------- M1: counting accuracy against manual audit ----------------
print("=== M1: accuracy (10 audit clips, Saturday July 11) ===")
man_e = np.array([62, 86, 3, 9, 6, 27, 78, 68, 97, 226])
man_x = np.array([55, 57, 15, 3, 8, 32, 45, 53, 129, 204])
builds = {
    "Original": ([28, 18, 2, 5, 1, 14, 39, 33, 97, 171], [31, 7, 5, 1, 2, 14, 12, 19, 83, 64]),
    "v0.40":    ([61, 89, 3, 10, 5, 19, 58, 46, 93, 231], [48, 49, 12, 3, 6, 15, 44, 39, 109, 245]),
    "v0.45":    ([61, 91, 3, 10, 6, 26, 79, 57, 94, 229], [47, 49, 13, 3, 11, 26, 60, 61, 110, 249]),
}

def accuracy(auto, manual):
    """A = max(0, 1 - sum|auto - manual| / sum manual)"""
    return max(0.0, 1 - np.abs(auto - manual).sum() / manual.sum())

for name, (e, x) in builds.items():
    e, x = np.array(e), np.array(x)
    auto, manual = np.concatenate([e, x]), np.concatenate([man_e, man_x])
    # bootstrap: resample clips (keeping each clip's Enter and Exit together)
    boot = []
    for _ in range(5000):
        i = np.random.randint(0, 10, 10)
        boot.append(accuracy(np.concatenate([e[i], x[i]]), np.concatenate([man_e[i], man_x[i]])))
    lo, hi = np.percentile(boot, [2.5, 97.5])
    print(f"{name:9s} Enter {accuracy(e, man_e):.3f}  Exit {accuracy(x, man_x):.3f}  "
          f"Combined {accuracy(auto, manual):.3f} (95% CI {lo:.3f}-{hi:.3f})  "
          f"theta Enter {e.sum()/man_e.sum():.2f}  Exit {x.sum()/man_x.sum():.2f}  "
          f"Combined {auto.sum()/manual.sum():.2f}")

# ---------------- camera-day data ----------------
d = pd.read_csv("camday.csv")
d["day"] = d.date.map(DAY)
d["side"] = np.where(d.camera <= 5, "West", np.where(d.camera <= 7, "Central", "East"))
d["rate"] = d.total / d.hours
d["logT"] = np.log(d.hours)
print("\nCrossings per recorded camera-hour:")
print(d.pivot(index="camera", columns="day", values="rate").round(1))

# ---------------- M2: negative binomial, camera + day, offset log(hours) ----------------
print("\n=== M2: camera-day negative binomial ===")
pois = smf.glm("total ~ C(camera) + C(day)", d, offset=d.logT, family=sm.families.Poisson()).fit()
print(f"Poisson Pearson dispersion = {pois.pearson_chi2 / pois.df_resid:.1f}  (>>1 means overdispersed)")
m2 = smf.negativebinomial("total ~ C(camera) + C(day)", d, offset=d.logT).fit(disp=0)
print(m2.summary().tables[1])
for k in ["C(day)[T.Sat]", "C(day)[T.Sun]"]:
    rr = np.exp([m2.params[k], *m2.conf_int().loc[k]])
    print(f"{k}: rate ratio {rr[0]:.2f} (95% CI {rr[1]:.2f}-{rr[2]:.2f})")

# ---------------- M3: negative binomial, side + day ----------------
print("\n=== M3: side model ===")
m3 = smf.negativebinomial('total ~ C(side, Treatment("West")) + C(day)', d, offset=d.logT).fit(disp=0)
print(m3.summary().tables[1])
for k in m3.params.index:
    if "side" in k or "day" in k:
        rr = np.exp([m3.params[k], *m3.conf_int().loc[k]])
        print(f"{k}: rate ratio {rr[0]:.2f} (95% CI {rr[1]:.2f}-{rr[2]:.2f})")
print("Pooled rate by side and day:")
print(d.groupby(["day", "side"]).apply(lambda g: g.total.sum() / g.hours.sum()).unstack().round(1))

# ---------------- M4: hourly profile, quasi-Poisson ----------------
print("\n=== M4: hourly profile (11:00-16:59, camera-days with >= 6.9 recorded hours) ===")
h = pd.read_csv("camhour.csv")
h["day"] = h.date.map(DAY)
full_days = d[d.hours > 6.9][["date", "camera"]]
hh = h[(h.hour >= 11) & (h.hour <= 16)].merge(full_days)
hh["cd"] = hh.camera.astype(str) + hh.day
print(f"n = {len(hh)} hourly counts from {hh.cd.nunique()} camera-days")
m4 = smf.glm("count ~ C(cd) + C(hour):C(day)", hh, family=sm.families.Poisson()).fit(scale="X2")
m4_common = smf.glm("count ~ C(cd) + C(hour)", hh, family=sm.families.Poisson()).fit(scale="X2")
print(f"quasi-Poisson dispersion phi = {m4.scale:.1f}")
for k in m4.params.index:
    if "hour" in k:
        rr = np.exp([m4.params[k], *m4.conf_int().loc[k]])
        print(f"{k}: {rr[0]:.2f} (95% CI {rr[1]:.2f}-{rr[2]:.2f})")
df_num = m4_common.df_resid - m4.df_resid
F = ((m4_common.deviance - m4.deviance) / df_num) / m4.scale
print(f"Day-specific vs common hour profile: F = {F:.2f} on ({df_num:.0f}, {m4.df_resid:.0f}) df, "
      f"p = {stats.f.sf(F, df_num, m4.df_resid):.2g}")

# ---------------- M5: direction share ----------------
print("\n=== M5: Enter share by camera (all days) ===")
g = d.groupby("camera")[["enter", "exit"]].sum()
g["n"] = g.enter + g.exit
g["enter_share"] = g.enter / g.n
g["se"] = np.sqrt(g.enter_share * (1 - g.enter_share) / g.n)  # binomial standard error
print(g.round(3))

# ---------------- stability of camera ranking ----------------
print("\nSpearman correlation of camera rates between days:")
print(d.pivot(index="camera", columns="day", values="rate").corr(method="spearman").round(2))

# ---------------- M6: survey proportions (Wilson 95% CI), n = 109 ----------------
print("\n=== M6: survey proportions ===")
for label, k in [("Arrived by car", 69), ("Walked", 24), ("Transit", 15), ("Cycled", 5),
                 ("Edmonton FSA", 87), ("1+ other district activity", 104),
                 ("3+ other activities", 39), ("Spend with vendors and businesses", 72)]:
    lo, hi = proportion_confint(k, 109, method="wilson")
    print(f"{label:35s} {k:3d}/109 = {k/109:.1%}  (95% CI {lo:.1%}-{hi:.1%})")
