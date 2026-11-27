"""Allowlisted native HTTP paths for the dedicated V1 Frappe site."""

from werkzeug.exceptions import HTTPException

import frappe
from frappe.api import API_URL_MAP, v1, v2

from .native import guard_master_list, individual_master_access
from .registry import NATIVE_MASTERS
from .policy import active_actor, deny

PUBLIC_COMMANDS = frozenset({
    "login", "logout", "ping", "frappe.ping", "frappe.auth.get_logged_user",
    "frappe.handler.logout", "frappe.handler.ping",
})
BUSINESS_COMMANDS = frozenset(
    f"haxpower_v1.{module}.{name}"
    for module, names in (
        ("access.api", ("list_warehouses", "get_warehouse", "list_locations", "get_location",
                        "get_specification", "get_sku", "search_products", "product_filters", "csrf_token")),
        ("inventory.read.api", ("stock", "distribution", "batches", "transit", "ledger", "period",
                                "documents", "corrections", "document", "history", "audit")),
        ("reporting.api", ("generate", "status", "download")),
    )
    for name in names
)
LIST_COMMANDS = frozenset({
    "frappe.client.get_list", "frappe.client.get_value", "frappe.client.get_count",
    "frappe.desk.reportview.get", "frappe.desk.reportview.get_list", "frappe.desk.reportview.get_count",
})
RECORD_COMMANDS = frozenset({
    "frappe.client.get", "frappe.client.set_value", "frappe.client.delete",
    "frappe.desk.form.load.getdoc",
})
DOCUMENT_COMMANDS = frozenset({"frappe.client.insert", "frappe.client.save", "frappe.desk.form.save.savedocs"})
NATIVE_LIST = (v1.document_list, v2.document_list, v2.count)
NATIVE_DETAIL = (
    v1.read_doc, v1.create_doc, v1.update_doc, v1.delete_doc,
    v2.read_doc, v2.create_doc, v2.update_doc, v2.delete_doc, v2.get_meta,
)
LIST_PARAMETERS = frozenset({
    "cmd", "doctype", "fields", "fieldname", "filters", "or_filters", "order_by", "start", "limit",
    "limit_start", "limit_page_length", "as_dict", "distinct", "view", "_",
})
FILTER_OPERATORS = frozenset({"=", "!=", "<", ">", "<=", ">=", "in", "not in", "like", "not like", "is", "between"})


def _json(value):
    if isinstance(value, str):
        try:
            return frappe.parse_json(value)
        except ValueError:
            deny()
    return value


def _master(doctype):
    # The site operator uses the Bench control plane for native management.
    if not isinstance(doctype, str) or doctype not in NATIVE_MASTERS or active_actor() == "Administrator":
        deny()


def _fields(doctype):
    return {"name", "owner", "creation", "modified", "modified_by", "docstatus"} | {
        field.fieldname for field in frappe.get_meta(doctype).fields
        if not field.permlevel and not field.hidden and not field.fieldname.startswith("custom_hp_v1_")
        and field.fieldtype not in ("Table", "Table MultiSelect", "Password", "HTML")
        and not (field.fieldtype == "Link" and field.options in ("Item", "Warehouse", "Batch"))
    }


def _filter_allowed(field, operator, fields):
    return isinstance(field, str) and field in fields and isinstance(operator, str) and operator in FILTER_OPERATORS


def _verify_filters(doctype, fields, filters):
    if filters is None:
        return
    if isinstance(filters, dict):
        for field, value in filters.items():
            operator = value[0] if isinstance(value, list) and len(value) == 2 else "="
            if not _filter_allowed(field, operator, fields):
                deny()
        return
    if isinstance(filters, list):
        for row in filters:
            if not isinstance(row, list) or len(row) not in (3, 4):
                deny()
            if len(row) == 4 and row[0] != doctype:
                deny()
            field, operator, _ = row[-3:]
            if not _filter_allowed(field, operator, fields):
                deny()
        return
    deny()


def _verify_list(doctype, args, *, get_value=False):
    _master(doctype)
    guard_master_list(doctype)
    if args.keys() - LIST_PARAMETERS or args.get("view") not in (None, "", "List"):
        deny()
    fields = _fields(doctype)
    projection = args.get("fieldname") if get_value else args.get("fields")
    if get_value and isinstance(projection, str) and projection in fields:
        projection = [projection]
    elif projection:
        projection = _json(projection)
    if projection and (
        not isinstance(projection, list) or any(not isinstance(field, str) or field not in fields for field in projection)
    ):
        deny()
    filters = args.get("filters")
    if get_value and isinstance(filters, str) and not filters.startswith(("{", "[")):
        filters = {"name": filters}
    else:
        filters = _json(filters)
    _verify_filters(doctype, fields, filters)
    _verify_filters(doctype, fields, _json(args.get("or_filters")))
    if order := args.get("order_by"):
        if not isinstance(order, str):
            deny()
        for sort in order.split(","):
            parts = sort.split()
            if (not parts or parts[0] not in fields or len(parts) > 2
                    or (len(parts) == 2 and parts[1].lower() not in ("asc", "desc"))):
                deny()


def _verify_write(doctype, data, *, name=None):
    if not isinstance(data, dict) or data.get("doctype", doctype) != doctype:
        deny()
    if name and data.get("name", name) != name:
        deny()
    if data.get("amended_from") or data.get("docstatus") not in (None, 0, "0"):
        deny()
    individual_master_access(doctype, name or data.get("name") or data.get("item_code"))


def _verify_rpc(command, args):
    if not isinstance(command, str) or command != frappe.override_whitelisted_method(command):
        deny()
    # A Server Script with an API name can otherwise replace a built-in RPC.
    from frappe.core.doctype.server_script.server_script_utils import get_server_script_map

    if command in get_server_script_map().get("_api", {}):
        deny()
    if command in BUSINESS_COMMANDS:
        frappe.local.hp_private_business_response = True
        return
    if command in PUBLIC_COMMANDS:
        return
    if command in LIST_COMMANDS:
        _verify_list(args.get("doctype"), args, get_value=command == "frappe.client.get_value")
        return
    if command in DOCUMENT_COMMANDS:
        doc = _json(args.get("doc"))
        if not isinstance(doc, dict) or args.get("action") not in (None, "Save"):
            deny()
        _master(doc.get("doctype"))
        _verify_write(doc["doctype"], doc)
        return
    doctype = args.get("doctype")
    _master(doctype)
    if command in RECORD_COMMANDS and args.get("name"):
        individual_master_access(doctype, args.name)
        if command == "frappe.client.set_value":
            field = args.get("fieldname")
            allowed = _fields(doctype) - {"name", "owner", "creation", "modified", "modified_by", "docstatus"}
            if not (isinstance(field, str) and field in allowed):
                changes = _json(field)
                if not isinstance(changes, dict) or changes.keys() - allowed:
                    deny()
        return
    if command == "frappe.client.rename_doc" and not frappe.utils.sbool(args.get("merge")):
        if not args.get("old_name") or not args.get("new_name"):
            deny()
        individual_master_access(doctype, args.old_name)
        individual_master_access(doctype, args.new_name)
        return
    if command == "frappe.desk.form.load.getdoctype" and not frappe.utils.sbool(args.get("with_parent")):
        return
    deny()


def enforce_http_boundary():
    """Frappe v16 auth_hooks run after cookie, OAuth and API-key auth."""
    request = frappe.request
    if request.method == "OPTIONS":
        return
    args = frappe.form_dict
    if args.cmd:
        _verify_rpc(args.cmd, args)
        return
    path = request.path
    if not path.startswith("/api/"):
        if request.method in ("GET", "HEAD") and (
            path in ("/", "/login", "/desk", "/app", "/favicon.ico")
            or path.startswith(("/desk/", "/app/"))
        ):
            return
        deny()
    try:
        endpoint, route = API_URL_MAP.bind_to_environ(request.environ).match()
    except HTTPException:
        deny()
    if endpoint in (v1.handle_rpc_call, v2.handle_rpc_call):
        if route.get("doctype"):
            deny()
        command = route["method"]
        _verify_rpc(command.split("/")[0] if endpoint is v1.handle_rpc_call else command, args)
    elif endpoint in (v2.login, v2.logout, frappe.ping):
        return
    elif endpoint in NATIVE_LIST:
        _verify_list(route["doctype"], args)
    elif endpoint in NATIVE_DETAIL:
        doctype = route["doctype"]
        _master(doctype)
        name = route.get("name")
        individual_master_access(doctype, name)
        if any(key in args for key in ("run_method", "expand_links")):
            deny()
        if endpoint in (v1.create_doc, v1.update_doc, v2.create_doc, v2.update_doc):
            data = v1.get_request_form_data() if endpoint in (v1.create_doc, v1.update_doc) else args
            _verify_write(doctype, data, name=name)
    else:
        deny()


def protect_business_cache(response=None, **_kwargs):
    if response is not None and getattr(frappe.local, "hp_private_business_response", False):
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
