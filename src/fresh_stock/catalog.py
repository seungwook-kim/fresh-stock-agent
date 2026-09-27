"""품목·거래처 목록 불러오기.

기본 목록(data/*.csv)은 가상 매장용 가정값이다. 실제 매장 값으로 바꿔 쓸 수 있다.
"""

import csv
from dataclasses import dataclass
from functools import cached_property
from importlib import resources
from pathlib import Path

from .domain import Category, Item, Supplier


@dataclass(frozen=True)
class Catalog:
    items: tuple[Item, ...]
    suppliers: tuple[Supplier, ...]

    def item(self, item_id: str) -> Item:
        return self._items_by_id[item_id]

    def supplier(self, supplier_id: str) -> Supplier:
        return next(s for s in self.suppliers if s.id == supplier_id)

    @cached_property
    def _items_by_id(self) -> dict[str, Item]:
        return {i.id: i for i in self.items}


def _read_rows(path: Path | None, default_name: str) -> list[dict[str, str]]:
    if path is not None:
        text = path.read_text(encoding="utf-8")
    else:
        text = resources.files("fresh_stock").joinpath("data", default_name).read_text(encoding="utf-8")
    return list(csv.DictReader(text.splitlines()))


def load_catalog(items_csv: Path | None = None, suppliers_csv: Path | None = None) -> Catalog:
    suppliers = tuple(
        Supplier(id=r["id"], name=r["name"], channel=r["channel"])
        for r in _read_rows(suppliers_csv, "suppliers.csv")
    )
    supplier_ids = {s.id for s in suppliers}
    items = []
    for r in _read_rows(items_csv, "catalog.csv"):
        if r["supplier_id"] not in supplier_ids:
            raise ValueError(f"{r['name']}: 알 수 없는 거래처 {r['supplier_id']}")
        items.append(
            Item(
                id=r["id"],
                name=r["name"],
                category=Category(r["category"]),
                unit=r["unit"],
                supplier_id=r["supplier_id"],
                base_demand=float(r["base_demand"]),
                price=int(r["price"]),
                cost=int(r["cost"]),
                shelf_life=int(r["shelf_life"]),
            )
        )
    return Catalog(items=tuple(items), suppliers=suppliers)
