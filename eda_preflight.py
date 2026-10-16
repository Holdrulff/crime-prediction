"""Read-only audit of SSP-SP XLSX data for the report's exploratory section."""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import pathlib
import sys
import zipfile

from lxml import etree


NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
BASE = dt.datetime(1899, 12, 30)


def shared_strings(archive):
    values = []
    if "xl/sharedStrings.xml" not in archive.namelist():
        return values
    with archive.open("xl/sharedStrings.xml") as stream:
        for _, element in etree.iterparse(stream, events=("end",), tag=NS + "si"):
            values.append("".join(node.text or "" for node in element.iter(NS + "t")))
            element.clear()
            while element.getprevious() is not None:
                del element.getparent()[0]
    return values


def rows(archive, sheet, strings, with_row_numbers=False):
    with archive.open(f"xl/worksheets/sheet{sheet}.xml") as stream:
        for _, element in etree.iterparse(stream, events=("end",), tag=NS + "row"):
            record = {}
            for cell in element.iterchildren(NS + "c"):
                value = ("".join(cell.itertext()) if cell.get("t") == "inlineStr"
                         else cell.findtext(NS + "v"))
                if value is None:
                    continue
                column = "".join(ch for ch in cell.get("r", "") if ch.isalpha())
                record[column] = strings[int(value)] if cell.get("t") == "s" else value
            yield (int(element.get("r")), record) if with_row_numbers else record
            element.clear()
            while element.getprevious() is not None:
                del element.getparent()[0]


def date_from_excel(value):
    try:
        return (BASE + dt.timedelta(days=float(value))).date()
    except (TypeError, ValueError, OverflowError):
        return None


def audit(path, limit):
    result = {
        "file": path.name,
        "headers": None,
        "data_dictionary": [],
        "rows": 0,
        "municipality_registration_all": collections.Counter(),
        "municipality_occurrence_all": collections.Counter(),
        "city_registration_outside": collections.Counter(),
        "robbery_occurrence_year": collections.Counter(),
        "nature_city": collections.Counter(),
        "rubric_city": collections.Counter(),
        "crime_city": 0,
        "robbery_city": 0,
        "robbery_nature": collections.Counter(),
        "robbery_rubric": collections.Counter(),
        "robbery_month": collections.Counter(),
        "robbery_weekday": collections.Counter(),
        "robbery_neighborhood": collections.Counter(),
        "robbery_day": collections.Counter(),
        "robbery_bairro_day": collections.Counter(),
        "robbery_missing": collections.Counter(),
        "robbery_date_min": None,
        "robbery_date_max": None,
        "sample_robbery": [],
    }
    with zipfile.ZipFile(path) as archive:
        strings = shared_strings(archive)
        for row in rows(archive, 1, strings):
            result["data_dictionary"].append(row)
            if len(result["data_dictionary"]) >= 40:
                break
        for sheet in (2, 3):
            if f"xl/worksheets/sheet{sheet}.xml" not in archive.namelist():
                continue
            for number, row in enumerate(rows(archive, sheet, strings)):
                if number == 0:
                    result["headers"] = row
                    columns = {name: column for column, name in row.items()}
                    occurrence_city_column = next(
                        column for column, name in row.items()
                        if name.startswith("NOME_MUNICIPIO_CIRCUN")
                    )
                    continue
                result["rows"] += 1
                registration_city = (row.get(columns.get("NOME_MUNICIPIO", columns.get("CIDADE"))) or "").strip().upper()
                city = (row.get(occurrence_city_column) or "").strip().upper()
                result["municipality_registration_all"][registration_city] += 1
                result["municipality_occurrence_all"][city] += 1
                if city not in {"S.PAULO", "SÃO PAULO", "SAO PAULO"}:
                    continue
                if registration_city not in {"S.PAULO", "SÃO PAULO", "SAO PAULO"}:
                    result["city_registration_outside"][registration_city] += 1
                nature = (row.get(columns["NATUREZA_APURADA"]) or "").strip().upper()
                rubric = (row.get(columns["RUBRICA"]) or "").strip().upper()
                result["crime_city"] += 1
                result["nature_city"][nature] += 1
                result["rubric_city"][rubric] += 1
                if not nature.startswith("ROUBO"):
                    continue
                result["robbery_city"] += 1
                result["robbery_nature"][nature] += 1
                result["robbery_rubric"][rubric] += 1
                if len(result["sample_robbery"]) < 5:
                    sample_columns = ("DATA_OCORRENCIA_BO", "BAIRRO", "LATITUDE", "LONGITUDE", "RUBRICA", "NATUREZA_APURADA")
                    result["sample_robbery"].append({k: row.get(columns[k]) for k in sample_columns})
                date = date_from_excel(row.get(columns["DATA_OCORRENCIA_BO"]))
                if date is None:
                    result["robbery_missing"]["date"] += 1
                else:
                    result["robbery_month"][date.strftime("%Y-%m")] += 1
                    result["robbery_occurrence_year"][date.year] += 1
                    result["robbery_weekday"][date.weekday()] += 1
                    result["robbery_day"][date.isoformat()] += 1
                    if result["robbery_date_min"] is None or date < result["robbery_date_min"]:
                        result["robbery_date_min"] = date
                    if result["robbery_date_max"] is None or date > result["robbery_date_max"]:
                        result["robbery_date_max"] = date
                bairro = (row.get(columns["BAIRRO"]) or "").strip().upper()
                if not bairro or bairro == "NULL":
                    result["robbery_missing"]["bairro"] += 1
                else:
                    result["robbery_neighborhood"][bairro] += 1
                    if date is not None:
                        result["robbery_bairro_day"][(bairro, date.isoformat())] += 1
                try:
                    latitude = float(row.get(columns["LATITUDE"], ""))
                    longitude = float(row.get(columns["LONGITUDE"], ""))
                    if latitude == 0 or longitude == 0:
                        result["robbery_missing"]["zero_coords"] += 1
                    elif not (-24.1 < latitude < -23.2 and -47.1 < longitude < -46.2):
                        result["robbery_missing"]["coords_outside_bbox"] += 1
                except ValueError:
                    result["robbery_missing"]["coords_missing"] += 1
                if limit and result["rows"] >= limit:
                    break
            if limit and result["rows"] >= limit:
                break
    result["robbery_date_min"] = str(result["robbery_date_min"])
    result["robbery_date_max"] = str(result["robbery_date_max"])
    for key in ("municipality_registration_all", "municipality_occurrence_all", "city_registration_outside", "nature_city", "rubric_city", "robbery_nature", "robbery_rubric", "robbery_month", "robbery_weekday", "robbery_neighborhood", "robbery_day", "robbery_occurrence_year", "robbery_missing"):
        result[key] = dict(result[key])
    result["robbery_bairro_day"] = {
        "observed_pairs": len(result["robbery_bairro_day"]),
        "count_distribution": dict(collections.Counter(result["robbery_bairro_day"].values())),
    }
    return result


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=pathlib.Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--output-dir", type=pathlib.Path)
    args = parser.parse_args()
    for source in args.files:
        result = audit(source, args.limit)
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        if args.output_dir:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            destination = args.output_dir / f"{source.stem}.json"
            destination.write_text(encoded, encoding="utf-8")
            print(f"{source.name}: {result['rows']} registros; {result['robbery_city']} roubos na capital", flush=True)
        else:
            print(encoded, flush=True)
