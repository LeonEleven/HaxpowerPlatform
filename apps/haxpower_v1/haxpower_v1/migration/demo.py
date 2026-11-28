"""Deterministic synthetic spreadsheets for local engineering and demos."""

import argparse
from collections import defaultdict
from pathlib import Path

from . import formats, xlsx

BRANDS = ("Aster Simulated", "Boreal Simulated", "Citrine Simulated")
UNIT = ("PC", "SET")


def _table(headers, rows):
    return [headers, *([row.get(key) for key in headers] for row in rows)]


def _write(path, sheets):
    with path.open("xb") as stream:
        xlsx.write(stream, sheets)


def _synthetic(*, second=False, small=False):
    config = formats.CONFIG
    legacy = defaultdict(list)
    standard = defaultdict(list)
    standard["Instructions"].append({"version": formats.STANDARD})
    definitions = {row["code"]: row for row in config["attributes"]}
    for code in ("SF", "D28", "D128"):
        legacy[formats.LEGACY_CONFIG].append(dict(id=code, company_id="DEMO", config_code=code,
                                                  config_name="Synthetic old code"))
    for category in config["categories"]:
        code = category["code"]
        legacy[formats.LEGACY_CONFIG].append(dict(id=code, company_id="DEMO", config_code=code,
                                                  config_name=category["display_name"], parent_id=code))
        standard["Categories"].append(dict(key=code, action="reference", target_code=code))
    for code in definitions:
        standard["Attributes"].append(dict(key=code, action="reference", target_code=code))
    for index, brand in enumerate(BRANDS, 1):
        standard["Brands"].append(dict(key=f"BR{index}", action="create", code=f"DEMO-BR{index}", display_name=brand))
    if small:
        for code in ("UNBRANDED", "UNKNOWN-BRAND"):
            standard["Brands"].append(dict(key=code, action="reference", target_code=code))
    options = set()
    option_keys = {}
    value_count = 0
    for index in range(1, (7 if small else 261) + (1 if second else 0)):
        category = config["categories"][(index - 1) % len(config["categories"])]
        code = category["code"]
        key = f"S{index:04d}"
        term = "D28" if index % 2 else "D128"
        historical = f"SF{code}{term}B{index:04d}"
        if index == 1 or index % 3 == 0:
            historical = f"DEMO-{code}-S{index:04d}" + ("<legacy>" if index == 1 else "")
        display = f"{category['display_name']} 合成件 Engine sample {index:04d}"
        if second and index == 2:
            display += " Rev B"
        uom = UNIT[index % 2]
        row = dict(key=key, action="update" if second and index == 2 else "create",
                   category=code, display_name=display, stock_uom=uom)
        standard["Specifications"].append(row)
        standard["Historical Identifiers"].append(dict(key=key, target_type="Specifications", target=key,
                                                       source="SYNTHETIC", value=historical))
        legacy[formats.LEGACY_SPEC].append(dict(id=key, company_id="DEMO", material_spec_code=historical,
                                               material_spec_name=display, material_spec_unit=uom, status="ACTIVE"))
        attrs = [*config["common_attributes"], *category["attributes"]]
        pairs = [(name, 1) for name in attrs]
        if index <= 140:
            pairs.extend((name, 2) for name in config["common_attributes"][:2])
        for attr, variant in pairs:
            if small and index == 5:
                continue
            definition = definitions[attr]
            kind = definition["data_type"]
            if attr == "NOZZLE_HOLES":
                value = "7 × 0.32 mm @ 155°"
            elif attr in ("PART_NUMBER", "IMO_NUMBER"):
                value = f"00{index:04d}{variant}" if attr == "PART_NUMBER" else "0012345"
            elif kind == "DECIMAL":
                value = "13.025"
            elif kind == "BOOLEAN":
                value = "TRUE" if index % 2 else "FALSE"
            else:
                value = f"{attr} synthetic {index % 11:02d}.{variant}"
            option = None
            if kind.endswith("_SELECT"):
                combination = (attr, value)
                option = option_keys.setdefault(combination, f"O{len(option_keys) + 1}")
                if combination not in options:
                    options.add(combination)
                    standard["Options"].append(dict(key=option, action="create", attribute=attr,
                                                    option_key="DEMO-" + option, display_name=value))
            value_count += 1
            attr_key = f"V{value_count:05d}"
            standard["Specification Values"].append(dict(key=attr_key, specification=key, attribute=attr,
                                                          value=value if option is None else None, option=option))
            legacy[formats.LEGACY_ATTR].append(dict(id=attr_key, company_id="DEMO", material_spec_id=key,
                                                   prop_name=definition["display_name"], prop_value=value))
        # Some specs have no SKU; others have two or three brands.
        brand_count = 0 if index % 20 == 0 else (3 if index <= 56 else 2)
        for brand_index in range(1, brand_count + 1):
            sku_key = f"{key}-{brand_index}"
            brand = f"BR{brand_index}"
            legacy_brand = BRANDS[brand_index - 1]
            if small and index == 1 and brand_index in (1, 2):
                brand = ("UNBRANDED", "UNKNOWN-BRAND")[brand_index - 1]
                legacy_brand = None
            sku_code = f"{historical}{brand_index:03d}" if historical.startswith("SF") else f"{historical}-{brand_index}"
            standard["SKUs"].append(dict(key=sku_key, action="update" if second and index == 2 else "create",
                                         specification=key, brand=brand, display_name=display, stock_uom=uom))
            standard["Historical Identifiers"].append(dict(key=sku_key, target_type="SKUs", target=sku_key,
                                                           source="SYNTHETIC", value=sku_code))
            legacy[formats.LEGACY_SKU].append(dict(id=sku_key, company_id="DEMO", material_code=sku_code,
                                                  material_spec_id=key, factory_brand=legacy_brand, status="ACTIVE"))
    if small:
        sku = legacy[formats.LEGACY_SKU]
        sku[0]["material_code"] = "CONFLICT-001"
        for index, status in enumerate((None, "SYNTHETIC-UNKNOWN", "INACTIVE")):
            sku[index]["status"] = status
        sku[-1]["material_spec_id"] = "UNKNOWN"
        sku.append(dict(sku[-2]))
        attr = legacy[formats.LEGACY_ATTR]
        attr.append(dict(attr[0], id="DUPLICATE-SELECT"))
        single = next((row for row in attr if row["prop_name"] == "冷却类型"), None)
        if single:
            attr.append(dict(single, id="SINGLE-CONFLICT", prop_value="Another synthetic value"))
        standard["SKUs"][-1]["stock_uom"] = next(unit for unit in UNIT if unit != standard["SKUs"][-1]["stock_uom"])
    return legacy, standard


def generate(directory, *, anomalies=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for suffix, second in (("", False), ("-v2", True)):
        if anomalies and second:
            break
        legacy, standard = _synthetic(second=second, small=anomalies)
        _write(directory / f"legacy{suffix}.xlsx", {key: _table(value.split(), legacy[key])
                                                   for key, value in formats.LEGACY_FIELDS.items()})
        _write(directory / f"master{suffix}.xlsx", {key: _table(value, standard[key])
                                                   for key, value in formats.HEADERS.items()})
    if anomalies:
        from openpyxl import Workbook

        book = Workbook()
        book.active["A1"] = "=1+1"
        with (directory / "formula.xlsx").open("xb") as stream:
            book.save(stream)
        with (directory / "invalid.xlsx").open("xb") as stream:
            stream.write(b"synthetic invalid workbook")
        from zipfile import ZipFile

        with ZipFile(directory / "unsupported.xlsx", "x") as archive:
            archive.writestr("xl/embeddings/synthetic.bin", b"unsupported package fixture")
    else:
        with (directory / "template.xlsx").open("xb") as stream:
            formats.template(stream)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("directory", type=Path)
    cli.add_argument("--anomalies", action="store_true")
    args = cli.parse_args()
    generate(args.directory, anomalies=args.anomalies)
