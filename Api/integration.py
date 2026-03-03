import base64
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import xmlrpc.client
from datetime import datetime, timezone

from pydantic import BaseModel, ValidationError, field_validator

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAPPING_PATH = os.path.join(BASE_DIR, "product_mapping.json")
SYNC_LOGS_PATH = os.path.join(BASE_DIR, "sync_logs.jsonl")


class ProductInput(BaseModel):
    name: str
    sku: str | None = None
    default_code: str | None = None
    price: float | None = None
    list_price: float | None = None
    cost: float | None = None
    standard_price: float | None = None
    description: str | None = None
    description_sale: str | None = None
    type: str = "consu"
    active: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name is required")
        return cleaned

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        valid_types = {"consu", "service", "product"}
        if value not in valid_types:
            raise ValueError("type must be one of: consu, service, product")
        return value


def _load_local_env() -> None:
    env_path = ".env"
    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_local_env()


def _get_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value or ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: str, fallback):
    if not os.path.exists(path):
        return fallback
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def _save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def odoo_config() -> dict:
    return {
        "url": _get_env("ODOO_URL", required=True).rstrip("/"),
        "db": _get_env("ODOO_DB", required=True),
        "username": _get_env("ODOO_USERNAME", required=True),
        "api_key": _get_env("ODOO_API_KEY", required=True),
    }


def prestashop_config() -> dict:
    return {
        "url": _get_env("PRESTASHOP_URL", required=True).rstrip("/"),
        "api_key": _get_env("PRESTASHOP_API_KEY", required=True),
        "lang_id": int(_get_env("PRESTASHOP_LANG_ID", "1")),
        "default_category_id": int(_get_env("PRESTASHOP_DEFAULT_CATEGORY_ID", "2")),
        "retries": int(_get_env("PRESTASHOP_RETRIES", "2")),
        "timeout": int(_get_env("PRESTASHOP_TIMEOUT", "30")),
    }


def get_odoo_clients() -> tuple[int, xmlrpc.client.ServerProxy]:
    config = odoo_config()
    common = xmlrpc.client.ServerProxy(f"{config['url']}/xmlrpc/2/common")
    uid = common.authenticate(config["db"], config["username"], config["api_key"], {})
    if not uid:
        raise ValueError("Odoo authentication failed. Check ODOO_DB, ODOO_USERNAME, and ODOO_API_KEY")
    models = xmlrpc.client.ServerProxy(f"{config['url']}/xmlrpc/2/object")
    return uid, models


def _odoo_execute(model: str, method: str, args: list | None = None, kwargs: dict | None = None):
    config = odoo_config()
    uid, models = get_odoo_clients()
    return models.execute_kw(
        config["db"],
        uid,
        config["api_key"],
        model,
        method,
        args or [],
        kwargs or {},
    )


def _validate_products_payload(products: list[dict]) -> list[dict]:
    validated = []
    errors = []

    for index, item in enumerate(products, start=1):
        try:
            model = ProductInput.model_validate(item)
            validated.append(model.model_dump())
        except ValidationError as exc:
            errors.append({"index": index, "product": item, "error": exc.errors()})

    if errors:
        raise ValueError(f"Invalid product payload: {json.dumps(errors, ensure_ascii=False)}")

    return validated


def _normalize_product(raw: dict) -> dict:
    name = str(raw.get("name", "")).strip()
    if not name:
        raise ValueError("Each product must include a non-empty 'name'")

    sku = str(raw.get("sku", raw.get("default_code", ""))).strip() or None
    price_value = raw.get("price", raw.get("list_price", 0))
    cost_value = raw.get("cost", raw.get("standard_price"))

    values = {
        "name": name,
        "default_code": sku,
        "list_price": float(price_value or 0),
        "type": str(raw.get("type", "consu")),
        "description_sale": str(raw.get("description", raw.get("description_sale", ""))).strip() or False,
        "active": bool(raw.get("active", True)),
    }
    if cost_value is not None:
        values["standard_price"] = float(cost_value)

    return values


def create_odoo_products(products: list[dict]) -> dict:
    validated_products = _validate_products_payload(products)
    created_ids = []
    skipped = []
    errors = []
    seen_keys = set()

    for index, product in enumerate(validated_products, start=1):
        try:
            values = _normalize_product(product)

            duplicate_key = (values.get("default_code") or "", values["name"].strip().lower())
            if duplicate_key in seen_keys:
                skipped.append(
                    {
                        "index": index,
                        "product": product,
                        "reason": "Duplicated in input payload",
                    }
                )
                continue
            seen_keys.add(duplicate_key)

            if values.get("default_code"):
                domain = [[("default_code", "=", values["default_code"])]]
            else:
                domain = [[("name", "=", values["name"])]]

            existing_ids = _odoo_execute("product.template", "search", domain, {"limit": 1})
            if existing_ids:
                skipped.append(
                    {
                        "index": index,
                        "product": product,
                        "reason": f"Already exists in Odoo (id={existing_ids[0]})",
                    }
                )
                continue

            product_id = _odoo_execute("product.template", "create", [values])
            created_ids.append(product_id)
        except Exception as exc:
            errors.append({"index": index, "product": product, "error": str(exc)})

    return {
        "requested": len(validated_products),
        "created": len(created_ids),
        "created_ids": created_ids,
        "skipped": skipped,
        "errors": errors,
    }


def load_products_from_json(json_path: str) -> list[dict]:
    with open(json_path, "r", encoding="utf-8") as file:
        payload = json.load(file)

    if isinstance(payload, list):
        return _validate_products_payload(payload)
    if isinstance(payload, dict) and isinstance(payload.get("products"), list):
        return _validate_products_payload(payload["products"])

    raise ValueError("JSON must be a list of products or an object with a 'products' list")


def fetch_odoo_products(limit: int = 100) -> list[dict]:
    fields = ["id", "name", "default_code", "list_price", "description_sale", "active"]
    return _odoo_execute(
        "product.template",
        "search_read",
        [[("active", "=", True)]],
        {"fields": fields, "limit": int(limit), "order": "id asc"},
    )


def fetch_odoo_product_by_sku(sku: str) -> dict | None:
    value = str(sku).strip()
    if not value:
        raise ValueError("SKU is required")

    fields = ["id", "name", "default_code", "list_price", "description_sale", "active"]
    rows = _odoo_execute(
        "product.template",
        "search_read",
        [[("default_code", "=", value)]],
        {"fields": fields, "limit": 1},
    )
    return rows[0] if rows else None


def fetch_odoo_product_by_reference(reference: str) -> dict | None:
    value = str(reference).strip()
    if not value:
        raise ValueError("Reference is required")

    fields = ["id", "name", "default_code", "list_price", "qty_available", "description_sale", "active"]
    rows = _odoo_execute(
        "product.template",
        "search_read",
        [[("default_code", "=", value)]],
        {"fields": fields, "limit": 1, "context": {"active_test": False}},
    )
    return rows[0] if rows else None


def fetch_odoo_products_for_creation(limit: int = 100) -> list[dict]:
    fields = ["id", "name", "default_code", "list_price", "qty_available", "description_sale", "active"]
    return _odoo_execute(
        "product.template",
        "search_read",
        [[("active", "=", True)]],
        {"fields": fields, "limit": int(limit), "order": "id asc"},
    )


def fetch_odoo_orders(limit: int = 100) -> list[dict]:
    fields = ["id", "name", "client_order_ref", "partner_id", "amount_total", "state", "date_order"]
    return _odoo_execute(
        "sale.order",
        "search_read",
        [[]],
        {"fields": fields, "limit": int(limit), "order": "id desc"},
    )


def fetch_odoo_order_by_reference(reference: str) -> dict | None:
    value = str(reference).strip()
    if not value:
        raise ValueError("Reference is required")

    fields = ["id", "name", "client_order_ref", "partner_id", "amount_total", "state", "date_order"]
    rows = _odoo_execute(
        "sale.order",
        "search_read",
        [["|", ("name", "=", value), ("client_order_ref", "=", value)]],
        {"fields": fields, "limit": 1, "order": "id desc"},
    )
    return rows[0] if rows else None


def fetch_odoo_customers(limit: int = 100) -> list[dict]:
    fields = ["id", "name", "email", "phone", "mobile", "vat", "customer_rank", "supplier_rank", "active"]
    return _odoo_execute(
        "res.partner",
        "search_read",
        [[("customer_rank", ">", 0)]],
        {"fields": fields, "limit": int(limit), "order": "id desc"},
    )


def fetch_odoo_vendors(limit: int = 100) -> list[dict]:
    fields = ["id", "name", "email", "phone", "mobile", "vat", "customer_rank", "supplier_rank", "active"]
    return _odoo_execute(
        "res.partner",
        "search_read",
        [[("supplier_rank", ">", 0)]],
        {"fields": fields, "limit": int(limit), "order": "id desc"},
    )


def fetch_odoo_payments(limit: int = 100) -> list[dict]:
    fields = ["id", "name", "ref", "amount", "state", "payment_type", "partner_type", "date", "partner_id"]
    return _odoo_execute(
        "account.payment",
        "search_read",
        [[]],
        {"fields": fields, "limit": int(limit), "order": "id desc"},
    )


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "producto"


def _build_prestashop_product_xml(product: dict, lang_id: int, default_category_id: int) -> str:
    name = str(product.get("name", "")).strip()
    if not name:
        raise ValueError("Odoo product without name")

    price = float(product.get("list_price", 0.0))
    reference = str(product.get("default_code", "")).strip()
    description = str(product.get("description_sale", "")).strip()
    link_rewrite = _slugify(name)

    root = ET.Element("prestashop")
    product_node = ET.SubElement(root, "product")
    ET.SubElement(product_node, "id_category_default").text = str(default_category_id)
    ET.SubElement(product_node, "id_tax_rules_group").text = "1"
    ET.SubElement(product_node, "id_shop_default").text = "1"
    ET.SubElement(product_node, "active").text = "1"
    ET.SubElement(product_node, "state").text = "1"
    ET.SubElement(product_node, "available_for_order").text = "1"
    ET.SubElement(product_node, "show_price").text = "1"
    ET.SubElement(product_node, "minimal_quantity").text = "1"
    ET.SubElement(product_node, "price").text = f"{price:.2f}"
    ET.SubElement(product_node, "reference").text = reference

    name_node = ET.SubElement(product_node, "name")
    ET.SubElement(name_node, "language", {"id": str(lang_id)}).text = name

    link_rewrite_node = ET.SubElement(product_node, "link_rewrite")
    ET.SubElement(link_rewrite_node, "language", {"id": str(lang_id)}).text = link_rewrite

    description_node = ET.SubElement(product_node, "description_short")
    ET.SubElement(description_node, "language", {"id": str(lang_id)}).text = description

    associations = ET.SubElement(product_node, "associations")
    categories = ET.SubElement(associations, "categories")
    category = ET.SubElement(categories, "category")
    ET.SubElement(category, "id").text = str(default_category_id)

    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")


def _prestashop_request(
    path: str,
    method: str = "GET",
    data: str | None = None,
    output_format: str | None = "JSON",
) -> bytes:
    config = prestashop_config()
    url = f"{config['url']}{path}"
    token = base64.b64encode(f"{config['api_key']}:".encode("utf-8")).decode("utf-8")
    headers = {"Authorization": f"Basic {token}"}
    if output_format:
        headers["Output-Format"] = output_format

    raw_data = None
    if data is not None:
        raw_data = data.encode("utf-8")
        headers["Content-Type"] = "application/xml"

    max_attempts = max(1, config["retries"] + 1)
    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(url, data=raw_data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=config["timeout"]) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="ignore")
            is_retriable = error.code in {429} or error.code >= 500
            if is_retriable and attempt < max_attempts:
                time.sleep(1.2 * attempt)
                continue
            raise ValueError(f"Prestashop HTTP {error.code}: {details}") from error
        except urllib.error.URLError as error:
            if attempt < max_attempts:
                time.sleep(1.2 * attempt)
                continue
            raise ValueError(f"Prestashop connection error: {error}") from error

    raise ValueError("Prestashop request failed after retries")


def _extract_prestashop_product_ids(payload: bytes) -> list[int]:
    text = payload.decode("utf-8", errors="ignore").strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)

        if isinstance(parsed, dict):
            products = parsed.get("products", [])
            if isinstance(products, dict):
                products = products.get("product", products)
            if isinstance(products, dict):
                products = [products]
            if isinstance(products, list):
                ids = []
                for item in products:
                    if isinstance(item, dict) and item.get("id") is not None:
                        ids.append(int(item["id"]))
                return ids

        if isinstance(parsed, list):
            ids = []
            for item in parsed:
                if isinstance(item, dict) and item.get("id") is not None:
                    ids.append(int(item["id"]))
            return ids
    except json.JSONDecodeError:
        pass

    root = ET.fromstring(text)
    ids = []
    for product in root.findall(".//product"):
        raw_id = product.get("id")
        if raw_id:
            ids.append(int(raw_id))
            continue
        id_node = product.find("id")
        if id_node is not None and id_node.text:
            ids.append(int(id_node.text))
    return ids


def _extract_prestashop_products(payload: bytes) -> list[dict]:
    text = payload.decode("utf-8", errors="ignore").strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            products = parsed.get("products", [])
            if isinstance(products, dict):
                products = products.get("product", products)
            if isinstance(products, dict):
                products = [products]
            if isinstance(products, list):
                return [item for item in products if isinstance(item, dict)]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    except json.JSONDecodeError:
        pass

    root = ET.fromstring(text)
    products = []
    for product in root.findall(".//product"):
        row = {
            "id": int(product.get("id")) if product.get("id") else None,
            "reference": None,
            "price": None,
            "name": None,
            "active": None,
            "date_upd": None,
        }
        for key in ["reference", "price", "active", "date_upd"]:
            node = product.find(key)
            if node is not None and node.text is not None:
                row[key] = node.text

        name_node = product.find("name")
        if name_node is not None:
            language = name_node.find("language")
            if language is not None and language.text:
                row["name"] = language.text
            elif name_node.text:
                row["name"] = name_node.text

        products.append(row)
    return products


def _extract_prestashop_collection(payload: bytes, root_key: str) -> list[dict]:
    text = payload.decode("utf-8", errors="ignore").strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            rows = parsed.get(root_key, [])
            if isinstance(rows, dict):
                singular = root_key[:-1] if root_key.endswith("s") else root_key
                rows = rows.get(singular, rows)
            if isinstance(rows, dict):
                rows = [rows]
            if isinstance(rows, list):
                return [item for item in rows if isinstance(item, dict)]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    except json.JSONDecodeError:
        pass

    root = ET.fromstring(text)
    rows = []
    singular = root_key[:-1] if root_key.endswith("s") else root_key
    for item in root.findall(f".//{singular}"):
        record = {}
        if item.get("id"):
            record["id"] = int(item.get("id"))
        for child in list(item):
            if child.text is not None and child.text.strip():
                record[child.tag] = child.text
        rows.append(record)
    return rows


def _find_language_node(parent: ET.Element, lang_id: int) -> ET.Element | None:
    for node in parent.findall("language"):
        if node.get("id") == str(lang_id):
            return node
    return None


def _set_multilang_value(product_node: ET.Element, field_name: str, value: str, lang_id: int) -> None:
    field_node = product_node.find(field_name)
    if field_node is None:
        field_node = ET.SubElement(product_node, field_name)

    language_node = _find_language_node(field_node, lang_id)
    if language_node is None:
        language_node = ET.SubElement(field_node, "language", {"id": str(lang_id)})
    language_node.text = value


def _set_text_field(product_node: ET.Element, field_name: str, value: str) -> None:
    node = product_node.find(field_name)
    if node is None:
        node = ET.SubElement(product_node, field_name)
    node.text = value


def _prestashop_find_existing_product_id(reference: str, name: str) -> int | None:
    if reference:
        query = urllib.parse.urlencode(
            {
                "filter[reference]": f"[{reference}]",
                "display": "[id]",
            },
            safe="[]",
        )
        payload = _prestashop_request(f"/api/products?{query}", method="GET")
        ids = _extract_prestashop_product_ids(payload)
        if ids:
            return ids[0]

    if name:
        query = urllib.parse.urlencode(
            {
                "filter[name]": f"[{name}]",
                "display": "[id]",
            },
            safe="[]",
        )
        payload = _prestashop_request(f"/api/products?{query}", method="GET")
        ids = _extract_prestashop_product_ids(payload)
        if ids:
            return ids[0]

    return None


def _update_prestashop_product(product_id: int, product: dict, lang_id: int, default_category_id: int) -> None:
    payload = _prestashop_request(f"/api/products/{product_id}", method="GET", output_format=None)
    root = ET.fromstring(payload.decode("utf-8", errors="ignore"))
    product_node = root.find(".//product")
    if product_node is None:
        raise ValueError(f"Prestashop product id={product_id} not found in XML response")

    for tag in ["manufacturer_name", "quantity"]:
        node = product_node.find(tag)
        if node is not None:
            product_node.remove(node)

    name = str(product.get("name", "")).strip()
    description = str(product.get("description_sale", "")).strip()
    reference = str(product.get("default_code", "")).strip()
    price = float(product.get("list_price", 0.0))

    _set_text_field(product_node, "id", str(product_id))
    _set_text_field(product_node, "id_category_default", str(default_category_id))
    _set_text_field(product_node, "price", f"{price:.2f}")
    _set_text_field(product_node, "reference", reference)
    _set_multilang_value(product_node, "name", name, lang_id)
    _set_multilang_value(product_node, "description_short", description, lang_id)
    _set_multilang_value(product_node, "link_rewrite", _slugify(name), lang_id)

    updated_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    _prestashop_request(f"/api/products/{product_id}", method="PUT", data=updated_xml, output_format=None)


def _set_prestashop_product_active(product_id: int, active: bool) -> None:
    payload = _prestashop_request(f"/api/products/{product_id}", method="GET", output_format=None)
    root = ET.fromstring(payload.decode("utf-8", errors="ignore"))
    product_node = root.find(".//product")
    if product_node is None:
        raise ValueError(f"Prestashop product id={product_id} not found in XML response")

    for tag in ["manufacturer_name", "quantity"]:
        node = product_node.find(tag)
        if node is not None:
            product_node.remove(node)

    _set_text_field(product_node, "id", str(product_id))
    _set_text_field(product_node, "active", "1" if active else "0")

    updated_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    _prestashop_request(f"/api/products/{product_id}", method="PUT", data=updated_xml, output_format=None)


def _must_skip_zero_price_zero_stock(product: dict) -> bool:
    price = float(product.get("list_price", 0) or 0)
    qty = float(product.get("qty_available", 0) or 0)
    return price <= 0 and qty <= 0


def create_products_from_odoo_to_prestashop(limit: int = 100) -> dict:
    config = prestashop_config()
    products = fetch_odoo_products_for_creation(limit=limit)

    created = 0
    skipped = []
    errors = []

    for index, product in enumerate(products, start=1):
        try:
            reference = str(product.get("default_code", "")).strip()
            name = str(product.get("name", "")).strip()

            if _must_skip_zero_price_zero_stock(product):
                skipped.append(
                    {
                        "index": index,
                        "product": product,
                        "reason": "Skipped because price=0 and stock=0",
                    }
                )
                continue

            existing_id = _prestashop_find_existing_product_id(reference=reference, name=name)
            if existing_id is not None:
                skipped.append(
                    {
                        "index": index,
                        "product": product,
                        "reason": f"Already exists in Prestashop (id={existing_id})",
                    }
                )
                continue

            xml_payload = _build_prestashop_product_xml(
                product,
                lang_id=config["lang_id"],
                default_category_id=config["default_category_id"],
            )
            create_payload = _prestashop_request("/api/products", method="POST", data=xml_payload, output_format=None)
            created_ids = _extract_prestashop_product_ids(create_payload)
            prestashop_id = created_ids[0] if created_ids else _prestashop_find_existing_product_id(reference, name)
            if prestashop_id is not None:
                _update_mapping(product, int(prestashop_id), "created")
            created += 1
        except Exception as exc:
            errors.append({"index": index, "product": product, "error": str(exc)})

    summary = {
        "requested": len(products),
        "created": created,
        "skipped": skipped,
        "errors": errors,
    }

    _append_sync_log(
        {
            "timestamp": _utc_now_iso(),
            "flow": "create_products_get",
            "requested": summary["requested"],
            "created": summary["created"],
            "skipped": len(summary["skipped"]),
            "errors": len(summary["errors"]),
        }
    )

    return summary


def create_product_by_reference_to_prestashop(reference: str) -> dict:
    config = prestashop_config()
    product = fetch_odoo_product_by_reference(reference)
    if not product:
        return {"created": False, "reason": "Product not found in Odoo"}

    if _must_skip_zero_price_zero_stock(product):
        return {"created": False, "reason": "Skipped because price=0 and stock=0", "product": product}

    existing_id = _prestashop_find_existing_product_id(
        reference=str(product.get("default_code", "")).strip(),
        name=str(product.get("name", "")).strip(),
    )
    if existing_id is not None:
        return {"created": False, "reason": f"Already exists in Prestashop (id={existing_id})", "product": product}

    xml_payload = _build_prestashop_product_xml(
        product,
        lang_id=config["lang_id"],
        default_category_id=config["default_category_id"],
    )
    create_payload = _prestashop_request("/api/products", method="POST", data=xml_payload, output_format=None)
    created_ids = _extract_prestashop_product_ids(create_payload)
    prestashop_id = created_ids[0] if created_ids else _prestashop_find_existing_product_id(
        reference=str(product.get("default_code", "")).strip(),
        name=str(product.get("name", "")).strip(),
    )
    if prestashop_id is not None:
        _update_mapping(product, int(prestashop_id), "created")

    return {
        "created": True,
        "product": product,
        "prestashop_id": prestashop_id,
    }


def update_prestashop_product_by_reference(reference: str) -> dict:
    config = prestashop_config()
    product = fetch_odoo_product_by_reference(reference)
    if not product:
        return {"updated": False, "reason": "Product not found in Odoo"}

    existing_id = _prestashop_find_existing_product_id(
        reference=str(product.get("default_code", "")).strip(),
        name=str(product.get("name", "")).strip(),
    )
    if existing_id is None:
        return {"updated": False, "reason": "Product not found in Prestashop", "product": product}

    _update_prestashop_product(
        product_id=existing_id,
        product=product,
        lang_id=config["lang_id"],
        default_category_id=config["default_category_id"],
    )
    _update_mapping(product, existing_id, "updated")
    return {"updated": True, "product": product, "prestashop_id": existing_id}


def deactivate_prestashop_product_by_reference(reference: str) -> dict:
    value = str(reference).strip()
    if not value:
        raise ValueError("Reference is required")

    existing_id = _prestashop_find_existing_product_id(reference=value, name="")
    if existing_id is None:
        return {"deactivated": False, "reason": "Product not found in Prestashop"}

    _set_prestashop_product_active(existing_id, False)
    return {"deactivated": True, "reference": value, "prestashop_id": existing_id}


def deactivate_product_in_both_by_reference(reference: str) -> dict:
    value = str(reference).strip()
    if not value:
        raise ValueError("Reference is required")

    result = {
        "reference": value,
        "odoo": {"deactivated": False, "reason": None, "id": None},
        "prestashop": {"deactivated": False, "reason": None, "id": None},
    }

    product = fetch_odoo_product_by_reference(value)
    if product:
        odoo_id = int(product.get("id"))
        _odoo_execute("product.template", "write", [[odoo_id], {"active": False}])
        result["odoo"] = {"deactivated": True, "reason": None, "id": odoo_id}
    else:
        result["odoo"] = {"deactivated": False, "reason": "Product not found in Odoo", "id": None}

    prestashop_id = _prestashop_find_existing_product_id(reference=value, name="")
    if prestashop_id is not None:
        _set_prestashop_product_active(prestashop_id, False)
        result["prestashop"] = {"deactivated": True, "reason": None, "id": prestashop_id}
    else:
        result["prestashop"] = {"deactivated": False, "reason": "Product not found in Prestashop", "id": None}

    return result


def activate_product_in_both_by_reference(reference: str) -> dict:
    value = str(reference).strip()
    if not value:
        raise ValueError("Reference is required")

    result = {
        "reference": value,
        "odoo": {"activated": False, "reason": None, "id": None},
        "prestashop": {"activated": False, "reason": None, "id": None},
    }

    product = fetch_odoo_product_by_reference(value)
    if product:
        odoo_id = int(product.get("id"))
        _odoo_execute("product.template", "write", [[odoo_id], {"active": True}])
        result["odoo"] = {"activated": True, "reason": None, "id": odoo_id}
    else:
        result["odoo"] = {"activated": False, "reason": "Product not found in Odoo", "id": None}

    prestashop_id = _prestashop_find_existing_product_id(reference=value, name="")
    if prestashop_id is not None:
        _set_prestashop_product_active(prestashop_id, True)
        result["prestashop"] = {"activated": True, "reason": None, "id": prestashop_id}
    else:
        result["prestashop"] = {"activated": False, "reason": "Product not found in Prestashop", "id": None}

    return result


def _load_mapping() -> dict:
    data = _load_json(MAPPING_PATH, {"products": {}})
    if not isinstance(data, dict):
        return {"products": {}}
    products = data.get("products")
    if not isinstance(products, dict):
        data["products"] = {}
    return data


def _save_mapping(data: dict) -> None:
    _save_json(MAPPING_PATH, data)


def _update_mapping(odoo_product: dict, prestashop_id: int, action: str) -> None:
    mapping = _load_mapping()
    products = mapping.setdefault("products", {})

    odoo_id = str(odoo_product.get("id", ""))
    if not odoo_id:
        return

    products[odoo_id] = {
        "odoo_id": odoo_product.get("id"),
        "prestashop_id": prestashop_id,
        "reference": odoo_product.get("default_code"),
        "name": odoo_product.get("name"),
        "last_action": action,
        "last_sync_at": _utc_now_iso(),
    }
    _save_mapping(mapping)


def get_product_mappings(limit: int = 100) -> dict:
    mapping = _load_mapping()
    rows = list(mapping.get("products", {}).values())
    rows.sort(key=lambda row: row.get("odoo_id") or 0)
    return {
        "count": len(rows),
        "mappings": rows[:limit],
    }


def _append_sync_log(entry: dict) -> None:
    line = json.dumps(entry, ensure_ascii=False)
    with open(SYNC_LOGS_PATH, "a", encoding="utf-8") as file:
        file.write(line + "\n")


def get_sync_logs(limit: int = 20) -> dict:
    if not os.path.exists(SYNC_LOGS_PATH):
        return {"count": 0, "logs": []}

    with open(SYNC_LOGS_PATH, "r", encoding="utf-8") as file:
        lines = [line.strip() for line in file if line.strip()]

    logs = []
    for line in reversed(lines):
        try:
            logs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(logs) >= limit:
            break

    return {"count": len(logs), "logs": logs}


def fetch_prestashop_products(limit: int = 100) -> list[dict]:
    query = urllib.parse.urlencode(
        {
            "display": "[id,reference,name,price,active,date_upd]",
            "limit": f"0,{int(limit)}",
            "sort": "[id_ASC]",
        },
        safe="[],:",
    )
    payload = _prestashop_request(f"/api/products?{query}", method="GET")
    return _extract_prestashop_products(payload)


def fetch_prestashop_orders(limit: int = 100) -> list[dict]:
    query = urllib.parse.urlencode(
        {
            "display": "[id,reference,id_customer,total_paid,current_state,date_add,date_upd]",
            "limit": f"0,{int(limit)}",
            "sort": "[id_DESC]",
        },
        safe="[],:",
    )
    payload = _prestashop_request(f"/api/orders?{query}", method="GET")
    return _extract_prestashop_collection(payload, "orders")


def fetch_prestashop_order_by_reference(reference: str) -> dict | None:
    value = str(reference).strip()
    if not value:
        raise ValueError("Reference is required")

    query = urllib.parse.urlencode(
        {
            "filter[reference]": f"[{value}]",
            "display": "[id,reference,id_customer,total_paid,current_state,date_add,date_upd]",
            "limit": "0,1",
        },
        safe="[],:",
    )
    payload = _prestashop_request(f"/api/orders?{query}", method="GET")
    rows = _extract_prestashop_collection(payload, "orders")
    return rows[0] if rows else None


def fetch_prestashop_customers(limit: int = 100) -> list[dict]:
    query = urllib.parse.urlencode(
        {
            "display": "[id,firstname,lastname,email,active,date_add,date_upd]",
            "limit": f"0,{int(limit)}",
            "sort": "[id_DESC]",
        },
        safe="[],:",
    )
    payload = _prestashop_request(f"/api/customers?{query}", method="GET")
    return _extract_prestashop_collection(payload, "customers")


def send_products_to_prestashop(products: list[dict]) -> dict:
    config = prestashop_config()
    synced = 0
    updated = 0
    skipped = []
    errors = []
    seen_keys = set()

    mapping = _load_mapping().get("products", {})

    for index, product in enumerate(products, start=1):
        try:
            reference = str(product.get("default_code", "")).strip()
            name = str(product.get("name", "")).strip()
            odoo_id = str(product.get("id", ""))
            duplicate_key = (reference, name.lower())

            if duplicate_key in seen_keys:
                skipped.append(
                    {
                        "index": index,
                        "product": product,
                        "reason": "Duplicated in sync payload",
                    }
                )
                continue
            seen_keys.add(duplicate_key)

            existing_id = None
            if odoo_id and mapping.get(odoo_id):
                map_presta_id = mapping[odoo_id].get("prestashop_id")
                if map_presta_id:
                    existing_id = int(map_presta_id)

            if existing_id is None:
                existing_id = _prestashop_find_existing_product_id(reference=reference, name=name)

            if existing_id is not None:
                _update_prestashop_product(
                    product_id=existing_id,
                    product=product,
                    lang_id=config["lang_id"],
                    default_category_id=config["default_category_id"],
                )
                _update_mapping(product, existing_id, "updated")
                updated += 1
                continue

            xml_payload = _build_prestashop_product_xml(
                product,
                lang_id=config["lang_id"],
                default_category_id=config["default_category_id"],
            )
            create_payload = _prestashop_request("/api/products", method="POST", data=xml_payload, output_format=None)
            created_ids = _extract_prestashop_product_ids(create_payload)
            prestashop_id = created_ids[0] if created_ids else _prestashop_find_existing_product_id(reference, name)
            if prestashop_id is not None:
                _update_mapping(product, int(prestashop_id), "created")
            synced += 1
        except Exception as exc:
            errors.append({"index": index, "product": product, "error": str(exc)})

    summary = {
        "requested": len(products),
        "synced": synced,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }

    _append_sync_log(
        {
            "timestamp": _utc_now_iso(),
            "requested": summary["requested"],
            "synced": summary["synced"],
            "updated": summary["updated"],
            "skipped": len(summary["skipped"]),
            "errors": len(summary["errors"]),
        }
    )

    return summary
