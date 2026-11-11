# Figures 3 and 4 for the Art Walk capstone report (ggplot2 defaults)
# Inputs (same folder): camday.csv, camhour.csv
library(ggplot2)

day_lab <- c("2026-07-10" = "Friday", "2026-07-11" = "Saturday", "2026-07-12" = "Sunday")
side_of <- function(cam) ifelse(cam <= 5, "West", ifelse(cam <= 7, "Central", "East"))

# ---------- Figure 3: crossings per camera-hour by side and day ----------
d <- read.csv("camday.csv")
d$day  <- factor(day_lab[d$date], levels = day_lab)
d$side <- factor(side_of(d$camera), levels = c("West", "Central", "East"))
pooled <- aggregate(cbind(total, hours) ~ side + day, data = d, FUN = sum)
pooled$rate <- pooled$total / pooled$hours
print(pooled)

ggplot(pooled, aes(x = side, y = rate, fill = day)) +
  geom_col(position = "dodge") +
  labs(x = "District side", y = "Crossings per camera-hour", fill = "Day")
ggsave("figure3.png", width = 6.5, height = 4, dpi = 300)

# ---------- Figure 4: hourly rate ratios relative to 11:00 (model M4) ----------
h <- read.csv("camhour.csv")
h$day <- factor(day_lab[h$date], levels = day_lab)
full_days <- d[d$hours > 6.9, c("date", "camera")]
m4 <- merge(subset(h, hour >= 11 & hour <= 16), full_days)
m4$cd <- factor(paste(m4$camera, m4$day))

terms <- c()
for (dy in levels(m4$day)) for (hr in 12:16) {
  nm <- paste0("h", hr, "_", substr(dy, 1, 3))
  m4[[nm]] <- as.integer(m4$day == dy & m4$hour == hr)
  terms <- c(terms, nm)
}
fit <- glm(as.formula(paste("count ~ cd +", paste(terms, collapse = " + "))),
           family = quasipoisson, data = m4)
cat("phi =", round(summary(fit)$dispersion, 1), "\n")

est <- coef(fit)[terms]; se <- sqrt(diag(vcov(fit)))[terms]
rr <- data.frame(rr = exp(est), lo = exp(est - 1.96 * se), hi = exp(est + 1.96 * se),
                 hour = as.integer(sub("h(\\d+)_.*", "\\1", terms)),
                 day = factor(day_lab[match(sub(".*_", "", terms), substr(day_lab, 1, 3))], levels = day_lab))
rr <- rbind(data.frame(rr = 1, lo = 1, hi = 1, hour = 11, day = factor(day_lab, levels = day_lab)), rr)
print(rr[order(rr$day, rr$hour), ], digits = 3)

ggplot(rr, aes(x = hour, y = rr, colour = day)) +
  geom_hline(yintercept = 1, linetype = "dashed") +
  geom_line(position = position_dodge(width = 0.3)) +
  geom_pointrange(aes(ymin = lo, ymax = hi), position = position_dodge(width = 0.3)) +
  labs(x = "Hour", y = "Rate ratio relative to 11:00", colour = "Day")
ggsave("figure4.png", width = 6.5, height = 4, dpi = 300)
