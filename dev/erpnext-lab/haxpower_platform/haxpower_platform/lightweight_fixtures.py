"""Fictional, independent Stage 2B scope on the locked ERPNext lab site."""

COMPANY = "HP Stage 2B Lab"
LOCATIONS = {"W1/L1": "HP2B W1 L1 - H2B", "W1/L2": "HP2B W1 L2 - H2B", "W2/L1": "HP2B W2 L1 - H2B"}
TRANSIT = "HP2B Transit - H2B"
ITEMS = {sku: "HP2B-" + sku for sku in ("RECEIPT", "ISSUE", "COUNT", "PARTIAL", "COMPLETE", "FAULT", "BATCH")}
LOG = "Stage 2B Stock Operation"
