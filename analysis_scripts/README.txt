OSBA Art Walk capstone analysis
================================
Python (models M1-M6)
  01_extract_counts.py  Reads the Camera Comparison HTML reports and writes camday.csv and camhour.csv.
                        python 01_extract_counts.py "path/to/Art Walk Reports"
                        (camday.csv and camhour.csv are included, so this step is optional.)
  02_models.py          Fits models M1-M6 and prints the numbers used in Sections 6-7.
                        python 02_models.py
  Requirements: Python 3.10+, pandas, numpy, scipy, statsmodels

R (Figures 3 and 4)
  figures.R             Draws Figure 3 (crossings per camera-hour by side and day) and
                        Figure 4 (hourly rate ratios from model M4).
                        Rscript figures.R
  Requirements: R 4.x, ggplot2

Data
  camday.csv   One row per camera-day: date, camera, Enter, Exit, total crossings, recorded hours.
  camhour.csv  One row per camera-day-hour: date, camera, hour, crossings.

Models
  M1  Count accuracy A and count ratio theta vs the manual audit, with a clip bootstrap CI
  M2  Negative binomial: total ~ camera + day, offset log(recorded hours)
  M3  Negative binomial: total ~ side + day, offset log(recorded hours)
  M4  Quasi-Poisson hourly profile: count ~ camera-day + hour x day, with an F-test vs a common profile
  M5  Enter share by camera, with binomial standard errors
  M6  Survey proportions with Wilson 95% CIs
