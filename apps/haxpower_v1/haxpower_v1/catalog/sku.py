"""Internal, permission-checked Brand and SKU operations."""

from collections.abc import Sequence
from unicodedata import normalize
from uuid import uuid4

import frappe

from ..adapters.erpnext import item
from . import service as catalog
from . import validation as check
from .dto import ACTIVE, INACTIVE
from .persistence import catalog_doc, catalog_write, require_maintainer
from .sku_dto import Alias, Brand, BrandData, Identifier, ItemMappingDiagnostic, SKUData, StockItem
from .sku_persistence import BRAND, SKU

IDENTIFIER_KINDS = ("OEM", "MANUFACTURER", "ENGRAVING", "LEGACY", "OTHER")
IMMUTABLE_FIELDS = ("code", "specification", "brand", "stock_uom", "quantity_precision")


def _load(doctype, record_id):
    return catalog_doc(doctype, check.identifier(record_id, doctype))


def _search_text(value):
    return normalize("NFKC", value).casefold()


def _metadata(code, name, description):
    code = check.key(code, "Code")
    name = check.text(name, "Display name", 140, required=True)
    description = check.text(description, "Description", 4000)
    return dict(code=code, display_name=name, description=description,
                search_text=_search_text("\n".join((code, name, description))))


def _brand(doc):
    return Brand(doc.name, BrandData(
        code=doc.code, display_name=doc.display_name, description=doc.description or "", status=doc.status,
    ))


def save_brand(data: BrandData, *, record_id: str | None = None) -> Brand:
    with catalog_write():
        changes = _metadata(data.code, data.display_name, data.description)
        status = check.status(data.status)
        if record_id is None:
            doc = frappe.get_doc(doctype=BRAND, status=status, **changes)
            doc.insert(ignore_permissions=True, set_name=uuid4().hex)
        else:
            doc = _load(BRAND, record_id)
            if doc.code != changes["code"]:
                frappe.throw("An existing brand code is immutable.")
            if status == INACTIVE and frappe.db.get_value(
                SKU, {"brand": doc.name, "status": ACTIVE}, for_update=True,
            ):
                frappe.throw("A brand with active SKUs cannot be deactivated.")
            doc.update(dict(changes, status=status))
            doc.save(ignore_permissions=True)
        return _brand(doc)


def get_brand(record_id: str) -> Brand:
    require_maintainer()
    return _brand(_load(BRAND, record_id))


def _sku_fields(data):
    fields = _metadata(data.code, data.display_name, data.description)
    return dict(
        fields, specification=check.identifier(data.specification, "Specification"),
        brand=check.identifier(data.brand, "Brand"),
        stock_uom=check.text(data.stock_uom, "Stock UOM", 140, required=True),
        quantity_precision=check.order(data.quantity_precision, "Quantity precision"),
    )


def _attachment_rows(doc, *, identifiers, aliases):
    for table, values, dto in (("identifiers", identifiers, Identifier), ("aliases", aliases, Alias)):
        if values is None:
            continue
        if not isinstance(values, Sequence) or len(values) > 100 or any(not isinstance(v, dto) for v in values):
            frappe.throw(f"Provide up to 100 typed {table} per SKU.")
        doc.set(table, [])
        for row in values:
            value = check.text(row.value, "Identifier or alias", 140, required=True)
            fields = dict(value=value, search_text=check.text(_search_text(value), "Search text", 140),
                          source=check.text(row.source, "Source", 140))
            if table == "identifiers":
                fields.update(kind=check.choice(row.kind, IDENTIFIER_KINDS, "identifier type"),
                              notes=check.text(row.notes, "Identifier notes", 4000))
            else:
                fields.update(language=check.text(row.language, "Alias language", 35),
                              searchable=check.boolean(row.searchable, "Alias searchable"))
            doc.append(table, fields)


def _stock_item(doc, *, locked=False):
    return StockItem(
        id=doc.name, revision=doc.revision, status=doc.status,
        data=SKUData(code=doc.code, specification=doc.specification, brand=doc.brand,
                     display_name=doc.display_name, description=doc.description or "", stock_uom=doc.stock_uom,
                     quantity_precision=doc.quantity_precision),
        brand=_brand(_load(BRAND, doc.brand)) if doc.brand else None,
        identifiers=tuple(
            Identifier(kind=row.kind, value=row.value, source=row.source or "", notes=row.notes or "")
            for row in doc.identifiers
        ),
        aliases=tuple(
            Alias(value=row.value, language=row.language or "", source=row.source or "", searchable=bool(row.searchable))
            for row in doc.aliases
        ),
        mapping_status=item.mapping_status(doc, locked=locked),
    )


def create_sku(
    data: SKUData, *, identifiers: Sequence[Identifier] = (), aliases: Sequence[Alias] = (),
    existing_item: str | None = None,
) -> StockItem:
    with catalog_write():
        fields = _sku_fields(data)
        spec = catalog.get_specification(fields["specification"])
        brand = _load(BRAND, fields["brand"])
        if spec.data.status != ACTIVE or catalog.get_category(spec.data.category).data.status != ACTIVE:
            frappe.throw("SKU creation requires an active specification and category.")
        if brand.status != ACTIVE:
            frappe.throw("SKU creation requires an active brand.")
        if frappe.db.get_value(SKU, {"code": fields["code"]}, for_update=True):
            frappe.throw("This SKU code already exists.", frappe.UniqueValidationError)
        group = item.validate_configuration(fields["stock_uom"], fields["quantity_precision"])
        native_name = fields["code"] if existing_item is None else check.text(
            existing_item, "ERPNext Item name", 140, required=True,
        )
        sku_id = uuid4().hex
        doc = frappe.get_doc(
            doctype=SKU, name=sku_id, status=ACTIVE, revision=1, native_item=native_name,
            native_item_group=group, **fields,
        )
        _attachment_rows(doc, identifiers=identifiers, aliases=aliases)
        item.bind_item(doc, adopt=existing_item is not None)
        doc.insert(ignore_permissions=True, set_name=sku_id)
        result = _stock_item(doc, locked=True)
        if result.mapping_status != "Verified":
            frappe.throw("ERPNext did not persist the requested SKU mapping.")
        return result


def get_sku(record_id: str) -> StockItem:
    require_maintainer()
    return _stock_item(_load(SKU, record_id))


def edit_sku(
    record_id: str, data: SKUData, *, expected_revision: int,
    identifiers: Sequence[Identifier] | None = None, aliases: Sequence[Alias] | None = None,
) -> StockItem:
    with catalog_write():
        doc = _load(SKU, record_id)
        if check.order(expected_revision, "SKU revision") != doc.revision:
            frappe.throw("The SKU revision changed. Reload it before editing.")
        fields = _sku_fields(data)
        if any(fields[field] != doc.get(field) for field in IMMUTABLE_FIELDS):
            frappe.throw("SKU code, specification, brand, stock UOM and quantity precision are immutable.")
        if item.mapping_status(doc, locked=True) != "Verified":
            frappe.throw("The native Item mapping needs review before editing this SKU.")
        _attachment_rows(doc, identifiers=identifiers, aliases=aliases)
        if fields["display_name"] != doc.display_name:
            item.update_item_name(doc, fields["display_name"])
        doc.update(dict(fields, revision=doc.revision + 1))
        doc.save(ignore_permissions=True)
        result = _stock_item(doc, locked=True)
        if result.mapping_status != "Verified":
            frappe.throw("The native Item did not retain the requested SKU changes.")
        return result


def deactivate_sku(record_id: str) -> StockItem:
    with catalog_write():
        doc = _load(SKU, record_id)
        result = _stock_item(doc, locked=True)
        if result.mapping_status != "Verified":
            frappe.throw("The native Item mapping needs review before deactivating this SKU.")
        if doc.status != INACTIVE:
            item.assert_deactivation_safe(doc)
        return result


def diagnose_item_mapping(item_name: str) -> ItemMappingDiagnostic:
    require_maintainer()
    if frappe.session.user != "Administrator":
        frappe.throw("Site administration permission is required for native Item diagnostics.", frappe.PermissionError)
    name = check.text(item_name, "ERPNext Item name", 140, required=True)
    return item.mapping_for_item(name)


def search_skus(query: str, *, limit: int = 20, offset: int = 0, include_inactive: bool = False) -> tuple[StockItem, ...]:
    require_maintainer()
    if not 1 <= check.order(limit, "Result limit") <= 50:
        frappe.throw("SKU search supports up to 50 results per page.")
    offset = check.order(offset, "Result offset")
    ids, _ = search_sku_ids(query, limit=limit, offset=offset, include_inactive=include_inactive)
    return tuple(_stock_item(_load(SKU, sku_id)) for sku_id in ids)


def search_sku_ids(query, *, limit=None, offset=0, include_inactive=False, category=None, brand=None,
                   sku_code=None, attributes=None, candidates=None, stock_fields_only=False):
    """Search one SKU at a time, with specification fields shared by its own SKUs."""
    from . import read

    if isinstance(query, str) and len(query) > 200:
        frappe.throw("PRODUCT_SEARCH_LIMIT_EXCEEDED")
    normalized = _search_text(check.text(query, "Search query", 200))
    tokens = normalized.split()
    if len(normalized) > 200 or len(tokens) > 10:
        frappe.throw("PRODUCT_SEARCH_LIMIT_EXCEEDED")
    if stock_fields_only and (category is not None or attributes is not None):
        frappe.throw("Product read permission is required for technical filters.", frappe.PermissionError)
    if category is not None:
        check.identifier(category, "Category")
    if brand is not None:
        check.identifier(brand, "Brand")
    if sku_code is not None:
        sku_code = check.key(sku_code, "SKU code")
    if not stock_fields_only:
        attributes = read.attribute_filters(attributes if attributes is not None else {})
    conditions, params = [], []

    def condition(sql, value):
        conditions.append(sql)
        params.append(value)

    if not check.boolean(include_inactive, "Include inactive"):
        condition("sku.status = %s", ACTIVE)
    if brand is not None:
        condition("sku.brand = %s", brand)
    if sku_code is not None:
        condition("sku.code = %s", sku_code)
    if candidates is not None:
        if not candidates:
            return (), 0
        condition("sku.name IN %s", tuple(sorted(candidates)))

    if stock_fields_only:
        # The full search index also contains descriptions. Normalize only the
        # individually permitted fields, before computing matches and totals.
        where = " AND ".join(conditions) or "1 = 1"
        rows = frappe.db.sql(
            f"""SELECT sku.name, sku.code, sku.display_name, sku.stock_uom, brand.code, brand.display_name
                FROM `tab{SKU}` sku LEFT JOIN `tab{BRAND}` brand ON brand.name = sku.brand
                WHERE {where} ORDER BY (BINARY LOWER(sku.code) = BINARY %s) DESC, sku.code, sku.name""",
            (*params, normalized),
        )
        if tokens:
            searchable = ((name, _search_text("\n".join(value or "" for value in fields))) for name, *fields in rows)
            names = tuple(name for name, label in searchable if all(token in label for token in tokens))
        else:
            names = tuple(row[0] for row in rows)
        end = offset + limit if limit is not None else None
        return names[offset:end], len(names)

    spec_search = {}
    if tokens or category or attributes:
        specs = read.specification_rows({"status": ACTIVE} if not include_inactive else {})
        if category or attributes:
            specs = {key: spec for key, spec in specs.items() if read.matches(spec, category, attributes)}
            if not specs:
                return (), 0
            condition("sku.specification IN %s", tuple(specs))
        if tokens:
            spec_search = {key: read.searchable_text(spec) for key, spec in specs.items()}
    for token in tokens:
        matched = tuple(key for key, searchable in spec_search.items() if token in searchable)
        conditions.append(
            """(sku.specification IN %s
             OR LOCATE(BINARY %s, BINARY sku.search_text)
             OR LOCATE(BINARY %s, BINARY brand.search_text)
             OR EXISTS (SELECT 1 FROM `tabHP V1 Product Alias` alias
                        WHERE alias.parent = sku.name AND alias.parenttype = %s AND alias.parentfield = 'aliases'
                          AND alias.searchable = 1 AND LOCATE(BINARY %s, BINARY alias.search_text))
             OR EXISTS (SELECT 1 FROM `tabHP V1 Product Identifier` identifier
                        WHERE identifier.parent = sku.name AND identifier.parenttype = %s
                          AND identifier.parentfield = 'identifiers'
                          AND LOCATE(BINARY %s, BINARY identifier.search_text)))"""
        )
        params.extend((matched or ("",), token, token, SKU, token, SKU, token))
    where = " AND ".join(conditions) or "1 = 1"
    source = f"""FROM `tab{SKU}` sku LEFT JOIN `tab{BRAND}` brand ON brand.name = sku.brand
                 WHERE {where}"""
    sort, sort_params = "ORDER BY sku.code, sku.name", ()
    if normalized:
        sort = """ORDER BY (BINARY LOWER(sku.code) = BINARY %s) DESC,
        EXISTS (SELECT 1 FROM `tabHP V1 Product Identifier` exact_id
                WHERE exact_id.parent=sku.name AND exact_id.parenttype=%s AND exact_id.parentfield='identifiers'
                AND BINARY exact_id.search_text=BINARY %s) DESC, sku.code, sku.name"""
        sort_params = (normalized, SKU, normalized)
    page = " LIMIT %s OFFSET %s" if limit is not None else ""
    names = frappe.db.sql(f"SELECT sku.name {source} {sort}{page}",
                          (*params, *sort_params, *((limit, offset) if page else ())))
    total = frappe.db.sql(f"SELECT COUNT(*) {source}", params)[0][0] if page else len(names)
    return tuple(name for (name,) in names), total
