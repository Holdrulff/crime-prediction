"""Generate three compact report figures from the read-only SSP-SP audit."""

from __future__ import annotations

import collections
import datetime as dt
import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).parent / "preflight"
OUTPUT = Path(__file__).parent / "output" / "pdf"
OUTPUT.mkdir(parents=True, exist_ok=True)
DATA = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(ROOT.glob("SPDadosCriminais_*.json"))]
BLUE = colors.HexColor("#2463a6")
GRAY = colors.HexColor("#46515d")
pdfmetrics.registerFont(TTFont("Arial", "C:/Windows/Fonts/arial.ttf"))
pdfmetrics.registerFont(TTFont("Arial-Bold", "C:/Windows/Fonts/arialbd.ttf"))
REGULAR, BOLD = "Arial", "Arial-Bold"


def page(name, title, subtitle):
    out = canvas.Canvas(str(OUTPUT / name), pagesize=(720, 400))
    out.setTitle(title)
    out.setFont(BOLD, 17)
    out.setFillColor(colors.HexColor("#18222c"))
    out.drawString(48, 350, title)
    out.setFont(REGULAR, 10)
    out.setFillColor(GRAY)
    out.drawString(48, 331, subtitle)
    return out


def finish(out, footnote):
    out.setFillColor(GRAY)
    out.setFont(REGULAR, 8)
    out.drawString(48, 26, footnote)
    out.save()


def annual():
    totals = collections.Counter()
    for data in DATA:
        totals.update({int(year): count for year, count in data["robbery_occurrence_year"].items()})
    years = list(range(2022, 2026))
    out = page(
        "roubos_por_ano.pdf",
        "Registros de roubo por ano da ocorrência",
        "Município de São Paulo; data da ocorrência; arquivos SSP-SP 2022-2026",
    )
    x0, y0, width, height = 86, 88, 580, 215
    maximum = 180000
    for step in range(0, maximum + 1, 30000):
        y = y0 + height * step / maximum
        out.setStrokeColor(colors.HexColor("#e4e9ef"))
        out.line(x0, y, x0 + width, y)
        out.setFillColor(GRAY)
        out.setFont(REGULAR, 8)
        out.drawRightString(x0 - 9, y - 3, f"{step // 1000} mil")
    for index, year in enumerate(years):
        value = totals[year]
        x = x0 + 35 + index * 145
        bar_height = height * value / maximum
        out.setFillColor(BLUE)
        out.rect(x, y0, 70, bar_height, stroke=0, fill=1)
        out.setFillColor(colors.HexColor("#18222c"))
        out.setFont(BOLD, 10)
        out.drawCentredString(x + 35, y0 + bar_height + 8, f"{value:,}".replace(",", "."))
        out.setFont(REGULAR, 10)
        out.drawCentredString(x + 35, y0 - 20, str(year))
    finish(out, "2025 é preliminar: o arquivo de 2026 disponível cobre apenas o primeiro semestre.")


def weekdays():
    days = collections.Counter()
    for data in DATA:
        days.update({dt.date.fromisoformat(day): count for day, count in data["robbery_day"].items() if day.startswith("2025-")})
    totals, counts = collections.Counter(), collections.Counter()
    for day, value in days.items():
        totals[day.weekday()] += value
        counts[day.weekday()] += 1
    averages = [totals[index] / counts[index] for index in range(7)]
    names = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
    out = page(
        "roubos_por_dia_semana.pdf",
        "Média diária de roubos por dia da semana",
        "Ocorrências em 2025; município de São Paulo; arquivos SSP-SP 2025-2026",
    )
    x0, y0, width, height = 78, 88, 590, 215
    maximum = 400
    for step in range(0, maximum + 1, 100):
        y = y0 + height * step / maximum
        out.setStrokeColor(colors.HexColor("#e4e9ef"))
        out.line(x0, y, x0 + width, y)
        out.setFillColor(GRAY)
        out.setFont(REGULAR, 8)
        out.drawRightString(x0 - 9, y - 3, str(step))
    for index, value in enumerate(averages):
        x = x0 + 16 + index * 83
        bar_height = height * value / maximum
        out.setFillColor(BLUE)
        out.rect(x, y0, 46, bar_height, stroke=0, fill=1)
        out.setFillColor(colors.HexColor("#18222c"))
        out.setFont(BOLD, 9)
        out.drawCentredString(x + 23, y0 + bar_height + 7, f"{value:.1f}")
        out.setFont(REGULAR, 10)
        out.drawCentredString(x + 23, y0 - 20, names[index])
    finish(out, "Cada barra divide a contagem pelo número de dias daquele tipo no ano.")


def neighborhoods():
    source = next(data for data in DATA if "2025" in data["file"])
    leaders = collections.Counter(source["robbery_neighborhood"]).most_common(10)
    out = page(
        "roubos_bairros_2025.pdf",
        "Bairros com mais registros de roubo",
        "Arquivo SSP-SP 2025; nomes de bairro como aparecem na fonte, sem padronização",
    )
    maximum = 3500
    x0, y0, width = 222, 70, 395
    for index, (name, value) in enumerate(leaders):
        y = 290 - index * 24
        out.setFont(REGULAR, 9)
        out.setFillColor(colors.HexColor("#18222c"))
        display_name = {"JD S�O LUIS": "JD SÃO LUIS"}.get(name, name)
        out.drawRightString(x0 - 8, y + 4, display_name)
        out.setFillColor(BLUE)
        out.rect(x0, y, width * value / maximum, 15, stroke=0, fill=1)
        out.setFillColor(colors.HexColor("#18222c"))
        out.drawString(x0 + width * value / maximum + 6, y + 4, f"{value:,}".replace(",", "."))
    finish(out, "Contagens absolutas: não ajustadas por população, área ou subnotificação.")


if __name__ == "__main__":
    annual()
    weekdays()
    neighborhoods()
    print("Figuras geradas em", OUTPUT)
