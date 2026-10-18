"""Example source node: synthetic sales data (no input ports)."""
import datetime as dt

import numpy as np
import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE

REGIONS = ["North", "South", "East", "West"]
PRODUCTS = {"Widget": 9.5, "Gadget": 24.0, "Gizmo": 3.25, "Doohickey": 14.0}


class SyntheticSalesGenerator(NodeModel):
    factory = "example.SyntheticSalesGenerator"  # stable id stored in .vkf files
    name = "Synthetic Sales Generator"
    category = "Custom/Examples"
    node_type = "Source"
    icon = "$$"
    short_description = "Generates a reproducible table of sales transactions."
    description = (
        "Creates random sales rows with a date, region, product, units, unit price and a "
        "deliberately messy customer name (extra spaces, mixed case) for cleaning demos. "
        "A few rows get extreme unit counts so outlier detection has something to find."
    )
    out_ports = [Port(TABLE, "Sales", "Generated transactions")]
    fields = [
        F("rows", "int", "Number of rows", 200),
        F("seed", "int", "Random seed", 7),
        F("start", "string", "First date (YYYY-MM-DD)", "2024-01-01"),
        F("days", "int", "Days covered", 180),
    ]

    def execute(self, inputs, s, ctx):
        try:
            start = dt.date.fromisoformat(s["start"])
        except ValueError:
            raise InvalidSettingsError(f"Invalid start date: {s['start']!r}") from None
        n = 0 if ctx.dry else max(0, s["rows"])  # configure only needs the columns
        rng = np.random.default_rng(s["seed"])
        products = rng.choice(list(PRODUCTS), n)
        units = rng.poisson(12, n)
        spikes = rng.random(n) < 0.03  # a few outliers
        units = np.where(spikes, units * 15, units)
        names = rng.choice(["  alice SMITH", "Bob  jones ", "CARLOS  díaz", "dana lee", " Émile Roux  "], n)
        return [pl.DataFrame({
            "date": pl.Series([start + dt.timedelta(days=int(d)) for d in rng.integers(0, max(1, s["days"]), n)],
                              dtype=pl.Date),
            "region": pl.Series(rng.choice(REGIONS, n), dtype=pl.String),
            "product": pl.Series(products, dtype=pl.String),
            "units": pl.Series(units, dtype=pl.Int64),
            "unit_price": pl.Series([PRODUCTS[p] for p in products], dtype=pl.Float64),
            "customer": pl.Series(names, dtype=pl.String),
        })]
