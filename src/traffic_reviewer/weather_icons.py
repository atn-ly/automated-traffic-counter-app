from __future__ import annotations

from html import escape

from traffic_reviewer.weather import condition_from_weather_code


def weather_icon_kind(weather_code: int) -> str:
    code = int(weather_code)
    if code == 0:
        return "sunny"
    if code in {1, 2}:
        return "partly-cloudy"
    if code == 3:
        return "cloudy"
    if code in {45, 48}:
        return "foggy"
    if code in {51, 53, 55, 56, 57}:
        return "drizzle"
    if code in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "rainy"
    if code in {71, 73, 75, 77, 85, 86}:
        return "snowy"
    if code in {95, 96, 99}:
        return "thunderstorm"
    return "unknown"


def weather_icon_svg(
    weather_code: int,
    size: int = 24,
    *,
    x: float | None = None,
    y: float | None = None,
    css_class: str = "weather-icon",
) -> str:
    """Return a small self-contained SVG picture for every supported condition."""
    kind = weather_icon_kind(weather_code)
    label = condition_from_weather_code(int(weather_code))
    position = ""
    if x is not None:
        position += f' x="{float(x):.1f}"'
    if y is not None:
        position += f' y="{float(y):.1f}"'
    common = (
        f'<svg{position} width="{int(size)}" height="{int(size)}" viewBox="0 0 48 48" '
        f'class="{escape(css_class)}" role="img" aria-label="{escape(label)}" '
        'xmlns="http://www.w3.org/2000/svg">'
        f"<title>{escape(label)}</title>"
    )
    cloud = (
        '<path d="M13 31h23a7 7 0 0 0 0-14 11 11 0 0 0-20.5-3.2A8.6 8.6 0 0 0 13 31Z" '
        'fill="#cbd5e1" stroke="#64748b" stroke-width="2" stroke-linejoin="round"/>'
    )
    sun = (
        '<g stroke="#f59e0b" stroke-width="2.4" stroke-linecap="round">'
        '<path d="M24 3v5M24 40v5M3 24h5M40 24h5M9.2 9.2l3.6 3.6M35.2 35.2l3.6 3.6'
        'M38.8 9.2l-3.6 3.6M12.8 35.2l-3.6 3.6"/>'
        '</g><circle cx="24" cy="24" r="10" fill="#fbbf24" stroke="#f59e0b" '
        'stroke-width="2"/>'
    )
    if kind == "sunny":
        body = sun
    elif kind == "partly-cloudy":
        body = (
            '<circle cx="17" cy="17" r="8" fill="#fbbf24" stroke="#f59e0b" '
            'stroke-width="2"/>'
            '<g stroke="#f59e0b" stroke-width="2" stroke-linecap="round">'
            '<path d="M17 4v4M17 26v4M4 17h4M26 17h4M7.8 7.8l3 3M23.2 23.2l3 3"/>'
            f"{cloud}"
        )
    elif kind == "cloudy":
        body = cloud
    elif kind == "foggy":
        body = (
            f"{cloud}"
            '<g stroke="#94a3b8" stroke-width="2.5" stroke-linecap="round">'
            '<path d="M9 36h30M13 42h22"/></g>'
        )
    elif kind == "drizzle":
        body = (
            f"{cloud}"
            '<g stroke="#38bdf8" stroke-width="2" stroke-linecap="round">'
            '<path d="M17 35l-1 3M25 35l-1 3M33 35l-1 3"/></g>'
        )
    elif kind == "rainy":
        body = (
            f"{cloud}"
            '<g stroke="#0284c7" stroke-width="2.8" stroke-linecap="round">'
            '<path d="M17 35l-2 6M26 35l-2 6M35 35l-2 6"/></g>'
        )
    elif kind == "snowy":
        body = (
            f"{cloud}"
            '<g stroke="#38bdf8" stroke-width="1.8" stroke-linecap="round">'
            '<path d="M17 35v8M13.5 37l7 4M20.5 37l-7 4M32 35v8M28.5 37l7 4M35.5 37l-7 4"/>'
            "</g>"
        )
    elif kind == "thunderstorm":
        body = (
            f"{cloud}"
            '<path d="M26 32h7l-5 6h5l-10 9 3-7h-5Z" fill="#facc15" '
            'stroke="#ca8a04" stroke-width="1.5" stroke-linejoin="round"/>'
        )
    else:
        body = (
            '<circle cx="24" cy="24" r="18" fill="#e2e8f0" stroke="#64748b" '
            'stroke-width="2"/><text x="24" y="31" text-anchor="middle" '
            'font-family="Segoe UI,Arial,sans-serif" font-size="22" font-weight="700" '
            'fill="#475569">?</text>'
        )
    return f"{common}{body}</svg>"
