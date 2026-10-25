"""Client-facing texts. Python renders every template, so prices, totals, contacts and photos are always exact."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

from .catalog import Catalog, fmt_uah

MODEL_TEMPLATES = ("product_card", "product_photos", "product_color", "price_quote", "vat_explainer",
                   "department_contact", "services_list")
# The model chooses these too, but only Python decides which are due (order state) and fills every value.
ORDER_TEMPLATES = ("order_summary", "order_confirmed", "payment_details", "order_to_manager", "order_cancelled")


@dataclass
class RenderedPart:
    """One outgoing Telegram message. `history_text` is what the model sees of it in later turns."""

    kind: str  # text | photos
    text: str = ""  # message text or photo caption (HTML)
    photos: list[str] = field(default_factory=list)  # image sources; salesbot/photos.py turns them into file_ids
    history_text: str = ""
    meta: dict | None = None  # first part of a reply carries {"products": [...]} into messages.meta
    template_id: str | None = None  # None = the model's own free text


def plain(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


class Templates:
    def __init__(self, content: dict, catalog: Catalog, persona_name: str):
        self.content, self.catalog, self.persona_name = content, catalog, persona_name
        self.t = content["templates"]

    def text(self, template_id: str, **values: Any) -> RenderedPart:
        """Render a text template; values are HTML-escaped unless they are already-built HTML (`*_html`)."""
        esc = {k: (v if k.endswith("_html") else html.escape(str(v))) for k, v in values.items()}
        body = self.t[template_id]["text_uk"].format(**{k.removesuffix("_html"): v for k, v in esc.items()})
        return RenderedPart("text", body, history_text=plain(body), template_id=template_id)

    def payment_details(self, total_with_vat: int) -> RenderedPart:
        pay = {k: v for k, v in self.content["payment"].items() if k != "markers"}
        return self.text("payment_details", total_with_vat=fmt_uah(total_with_vat), **pay)

    @staticmethod
    def _photos(srcs: list[str], caption: str, label: str) -> RenderedPart:
        if not srcs:
            return RenderedPart("text", caption, history_text=plain(caption))
        return RenderedPart("photos", caption, srcs, history_text=f"[{label}: {len(srcs)} фото] {plain(caption)}")

    def render(self, part: dict) -> RenderedPart:
        tid = part["template_id"]
        cat = self.catalog
        rendered = self._render(tid, part, cat)
        rendered.template_id = tid
        return rendered

    def _render(self, tid: str, part: dict, cat: Catalog) -> RenderedPart:
        if tid in ("product_card", "product_photos", "product_color", "vat_explainer"):
            p = cat.products[part["product_slug"]]
        if tid == "product_card":
            caption = self.t[tid]["caption_uk"].format(name_uk=html.escape(p["name_uk"]))
            return self._photos(cat.photo_sources(p["slug"])[: self.t[tid]["max_photos"]], caption, "картка товару")
        if tid == "product_photos":
            caption = self.t[tid]["caption_uk"].format(short_name=html.escape(p["short_name"]))
            return self._photos(cat.photo_sources(p["slug"])[: self.t[tid]["max_photos"]], caption, "фото")
        if tid == "product_color":
            c = next(x for x in p["colors"] if x["id"] == part["color_id"])
            caption = self.t[tid]["caption_uk"].format(short_name=html.escape(p["short_name"]),
                                                       color_label=html.escape(c["label_uk"]))
            return self._photos([c["src"]], caption, "фото кольору")
        if tid == "price_quote":
            lines, total, _ = cat.quote(part["items"])
            fmt_line = self.t[tid]["line_uk"]
            rendered = [fmt_line.format(
                short_name=html.escape(line.short_name),
                options_line=html.escape(f" (+ {', '.join(line.option_labels)})") if line.option_labels else "",
                qty=line.qty, line_with_vat=fmt_uah(line.line_with_vat)) for line in lines]
            return self.text(tid, lines_html="\n".join(rendered), total_with_vat=fmt_uah(total))
        if tid == "vat_explainer":
            return self.text(tid, price_without_vat=fmt_uah(p["price"]["without_vat"]))
        if tid == "department_contact":
            d = self.content["departments"][part["department"]]
            contacts = "".join(f"\n{html.escape(v)}" for v in (d.get("phone"), d.get("email"), d.get("url")) if v)
            note = f"\n{html.escape(d['note_uk'])}" if d.get("note_uk") else ""
            return self.text(tid, dept_title=d["title_uk"], dept_lines_html=contacts, dept_note_html=note)
        if tid == "services_list":
            return self.text(tid, services="\n".join(f"• {s}" for s in self.content["services_uk"]))
        raise ValueError(f"unknown template {tid}")
