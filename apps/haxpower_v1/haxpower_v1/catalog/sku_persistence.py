"""Frappe records for the SKU catalog."""

from .persistence import CatalogDocument

BRAND = "HP V1 Brand"
SKU = "HP V1 Stock Item"
IDENTIFIER = "HP V1 Product Identifier"
ALIAS = "HP V1 Product Alias"


class SKUDocument(CatalogDocument):
    def before_change(self):
        # Document.db_set emits before_change instead of the standard validation hooks.
        self.validate()
