"""품목·거래처 목록 불러오기.

기본 목록(data/*.csv)은 가상 매장용 가정값이다. 가게마다 설정 화면에서 고치면 그 가게 목록이 DB에
저장되고(store.py), 고치기 전까지는 기본 목록을 쓴다.

품목은 지우지 않고 취급을 그만둔 품목(retired)으로 남긴다. 지난 기록(재고·입고·발주)에 그 품목이
남아 있어서, 판매량을 역산하거나 과거 기록을 보여줄 때 이름·단위·가격이 필요하다.
"""

import csv
import io
from dataclasses import dataclass, replace
from functools import cached_property
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from .domain import Category, Item, Supplier

ITEM_FIELDS = ("id", "name", "category", "unit", "supplier_id", "base_demand", "price", "cost", "shelf_life", "active")
SUPPLIER_FIELDS = ("id", "name", "channel")


@dataclass(frozen=True)
class Catalog:
    items: tuple[Item, ...]  # 취급 중인 품목. 재고표·발주·거래처 문자에 나온다
    suppliers: tuple[Supplier, ...]
    retired: tuple[Item, ...] = ()  # 취급을 그만둔 품목. 지난 기록을 읽을 때만 쓴다

    def item(self, item_id: str) -> Item:
        return self._items_by_id[item_id]

    def supplier(self, supplier_id: str) -> Supplier:
        return next(s for s in self.suppliers if s.id == supplier_id)

    @cached_property
    def _items_by_id(self) -> dict[str, Item]:
        return {i.id: i for i in (*self.retired, *self.items)}

    @property
    def all_items(self) -> tuple[Item, ...]:
        return (*self.items, *self.retired)

    def with_item(self, item: Item) -> "Catalog":
        """같은 id 품목을 바꾸거나(취급 여부는 그대로), 없으면 취급 품목 끝에 더한다."""
        if any(i.id == item.id for i in self.retired):
            return replace(self, retired=tuple(item if i.id == item.id else i for i in self.retired))
        if any(i.id == item.id for i in self.items):
            return replace(self, items=tuple(item if i.id == item.id else i for i in self.items))
        return replace(self, items=(*self.items, item))

    def with_active(self, item_id: str, active: bool) -> "Catalog":
        """품목 취급을 그만두거나 다시 취급한다. 다시 취급하는 품목은 끝에 붙는다."""
        item = self.item(item_id)
        items = tuple(i for i in self.items if i.id != item_id)
        retired = tuple(i for i in self.retired if i.id != item_id)
        if active:
            return replace(self, items=(*items, item), retired=retired)
        return replace(self, items=items, retired=(*retired, item))

    def with_supplier(self, supplier: Supplier) -> "Catalog":
        if any(s.id == supplier.id for s in self.suppliers):
            return replace(self, suppliers=tuple(supplier if s.id == supplier.id else s for s in self.suppliers))
        return replace(self, suppliers=(*self.suppliers, supplier))

    def without_supplier(self, supplier_id: str) -> "Catalog":
        return replace(self, suppliers=tuple(s for s in self.suppliers if s.id != supplier_id))


def _read_rows(path: "Path | Traversable | None", default_name: str) -> list[dict[str, str]]:
    if path is not None:
        text = path.read_text(encoding="utf-8-sig")  # 엑셀에서 저장한 CSV는 BOM이 붙는다
    else:
        text = resources.files("fresh_stock").joinpath("data", default_name).read_text(encoding="utf-8")
    return list(csv.DictReader(text.splitlines()))


def load_catalog(
    items_csv: "Path | Traversable | None" = None, suppliers_csv: "Path | Traversable | None" = None
) -> Catalog:
    """CSV에서 품목·거래처를 읽는다. 품목 CSV에 active 열이 있으면 0인 품목은 취급을 그만둔 품목이다."""
    suppliers = tuple(
        Supplier(id=r["id"], name=r["name"], channel=r["channel"])
        for r in _read_rows(suppliers_csv, "suppliers.csv")
    )
    supplier_ids = {s.id for s in suppliers}
    items, retired = [], []
    for r in _read_rows(items_csv, "catalog.csv"):
        if r["supplier_id"] not in supplier_ids:
            raise ValueError(f"{r['name']}: 알 수 없는 거래처 {r['supplier_id']}")
        item = Item(
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
        (items if (r.get("active") or "1").strip() != "0" else retired).append(item)
    return Catalog(items=tuple(items), suppliers=suppliers, retired=tuple(retired))


def sample_store_catalog() -> Catalog:
    """실제 종이 재고표(과일·야채·건어물·두부·계란 60줄)에 맞춘 예시 가게 목록. 가격은 2026년 도매 시세를 대충 맞춘 값이다."""
    folder = resources.files("fresh_stock").joinpath("data", "sample_store")
    return load_catalog(folder.joinpath("items.csv"), folder.joinpath("suppliers.csv"))


def catalog_csv(catalog: Catalog) -> tuple[str, str]:
    """(품목 CSV, 거래처 CSV). load_catalog로 다시 읽을 수 있다."""
    items, suppliers = io.StringIO(), io.StringIO()
    w = csv.writer(items, lineterminator="\n")
    w.writerow(ITEM_FIELDS)
    for active, group in ((1, catalog.items), (0, catalog.retired)):
        for i in group:
            w.writerow([i.id, i.name, i.category, i.unit, i.supplier_id, f"{i.base_demand:g}", i.price, i.cost,
                        i.shelf_life, active])  # fmt: skip
    w = csv.writer(suppliers, lineterminator="\n")
    w.writerow(SUPPLIER_FIELDS)
    for s in catalog.suppliers:
        w.writerow([s.id, s.name, s.channel])
    return items.getvalue(), suppliers.getvalue()
