"""Shop data: everything specific to one shop (catalog, texts, prompt examples), kept apart from the engine.

It is read from a folder (the fictional sample shop in data/sample, or a private folder) or from the database,
where `python -m salesbot.tools shop upload <folder>` puts it. That keeps private catalogs out of git.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .storage import Storage

FILES = ("catalog.json", "synonyms.yaml", "content.yaml", "examples.md")
# Texts Python sends on its own; every shop must define them.
REQUIRED_TEMPLATES = (
    "product_card", "product_photos", "product_color", "price_quote", "vat_explainer", "department_contact",
    "services_list", "greeting", "demo_disclaimer", "order_summary", "order_confirmed", "payment_details",
    "order_to_manager", "payment_received", "order_cancelled", "handoff_ack", "fallback_handoff", "limit_reached",
    "unsupported_media",
)


class ShopDataError(ValueError):
    pass


@dataclass
class ShopData:
    catalog: dict[str, Any]
    synonyms: dict[str, str]
    content: dict[str, Any]
    examples: str
    source: str  # where it came from, for logs

    @classmethod
    def parse(cls, texts: dict[str, str], source: str) -> ShopData:
        missing = [f for f in FILES if f not in texts]
        if missing:
            raise ShopDataError(f"{source}: missing {', '.join(missing)}")
        try:
            data = cls(json.loads(texts["catalog.json"]),
                       (yaml.safe_load(texts["synonyms.yaml"]) or {}).get("synonyms") or {},
                       yaml.safe_load(texts["content.yaml"]), texts["examples.md"], source)
        except (json.JSONDecodeError, yaml.YAMLError) as e:
            raise ShopDataError(f"{source}: {e}") from e
        problems = data.problems()
        if problems:
            raise ShopDataError(f"{source}: " + "; ".join(problems))
        return data

    @classmethod
    def read_folder(cls, folder: Path) -> dict[str, str]:
        return {f: (folder / f).read_text(encoding="utf-8") for f in FILES if (folder / f).exists()}

    @classmethod
    def from_folder(cls, folder: Path) -> ShopData:
        return cls.parse(cls.read_folder(folder), str(folder))

    @classmethod
    async def from_db(cls, storage: Storage) -> ShopData:
        rows = await storage.pool.fetch("SELECT name, body FROM shop_files")
        if not rows:
            raise ShopDataError("SHOP_SOURCE=db but the database has no shop data: "
                                "run `python -m salesbot.tools shop upload <folder>` first")
        return cls.parse({r["name"]: r["body"] for r in rows}, "database")

    @classmethod
    async def load(cls, shop_dir: Path | None, storage: Storage) -> ShopData:
        return cls.from_folder(shop_dir) if shop_dir else await cls.from_db(storage)

    @staticmethod
    async def upload(storage: Storage, texts: dict[str, str]) -> None:
        """Replace the shop data in the database in one transaction (validated first by the caller)."""
        async with storage.pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM shop_files")
            await conn.executemany("INSERT INTO shop_files (name, body) VALUES ($1, $2)", list(texts.items()))

    def problems(self) -> list[str]:
        out = []
        templates = (self.content or {}).get("templates") or {}
        out += [f"content.yaml: template {t!r} is missing" for t in REQUIRED_TEMPLATES if t not in templates]
        for key in ("persona", "departments", "payment", "services_uk", "policies_uk"):
            if key not in (self.content or {}):
                out.append(f"content.yaml: section {key!r} is missing")
        for key in ("company", "intro_en"):
            if key not in (self.content or {}).get("persona", {}):
                out.append(f"content.yaml: persona.{key} is missing")
        products = self.catalog.get("products") or []
        if not products:
            out.append("catalog.json: no products")
        for p in products:
            if p["price"]["with_vat"] != round(p["price"]["without_vat"] * (1 + self.catalog.get("vat_rate", 0.2))):
                out.append(f"catalog.json: {p['slug']} price with VAT doesn't match the price without VAT")
            for c in p["colors"]:
                if not c.get("src"):
                    out.append(f"catalog.json: {p['slug']} colour {c['id']} has no photo")
        return out
