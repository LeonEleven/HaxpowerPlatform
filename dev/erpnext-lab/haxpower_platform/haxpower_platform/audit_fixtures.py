"""Fictional Stage 1F inventory scope on the existing lab site."""

COMPANY = "HP Stage 1F Lab"
LOCATIONS = {"W1/L1": "HP1F W1 L1 - H1F", "W1/L2": "HP1F W1 L2 - H1F", "W2/L1": "HP1F W2 L1 - H1F"}
TRANSIT = "HP1F Transit - H1F"
ITEMS = {sku: "HP1F-" + sku for sku in ("AUDIT", "COUNT", "ZERO", "TRANSFER", "FAULT", "RACE")}
