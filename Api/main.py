from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from integration import (
	activate_product_in_both_by_reference,
	create_product_by_reference_to_prestashop,
	create_products_from_odoo_to_prestashop,
	create_odoo_products,
	deactivate_product_in_both_by_reference,
	deactivate_prestashop_product_by_reference,
	fetch_odoo_customers,
	fetch_odoo_order_by_reference,
	fetch_odoo_orders,
	fetch_odoo_payments,
	fetch_odoo_product_by_sku,
	fetch_prestashop_customers,
	fetch_prestashop_order_by_reference,
	fetch_prestashop_orders,
	fetch_prestashop_products,
	fetch_odoo_products,
	fetch_odoo_vendors,
	get_product_mappings,
	get_sync_logs,
	load_products_from_json,
	send_products_to_prestashop,
	update_prestashop_product_by_reference,
)

app = FastAPI(title="Odoo-Prestashop Integration API")


class ImportRequest(BaseModel):
	json_path: str = Field(default="products.json")


class SyncRequest(BaseModel):
	limit: int = Field(default=100, ge=1, le=1000)


@app.get("/")
def read_root():
	return {"message": "API running"}


@app.get("/flow/create-products")
def flow_create_products(limit: int = 100):
	try:
		result = create_products_from_odoo_to_prestashop(limit=limit)
		return result
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/flow/create-product/by-reference/{reference}")
def flow_create_product_by_reference(reference: str):
	try:
		result = create_product_by_reference_to_prestashop(reference=reference)
		if not result.get("created"):
			return result
		return result
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/flow/update-product/by-reference/{reference}")
def flow_update_product_by_reference(reference: str):
	try:
		result = update_prestashop_product_by_reference(reference=reference)
		if not result.get("updated"):
			return result
		return result
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/flow/deactivate-product/by-reference/{reference}")
def flow_deactivate_product_by_reference(reference: str):
	try:
		result = deactivate_prestashop_product_by_reference(reference=reference)
		if not result.get("deactivated"):
			return result
		return result
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/flow/deactivate-product/both/by-reference/{reference}")
def flow_deactivate_product_both_by_reference(reference: str):
	try:
		result = deactivate_product_in_both_by_reference(reference=reference)
		return result
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/flow/activate-product/both/by-reference/{reference}")
def flow_activate_product_both_by_reference(reference: str):
	try:
		result = activate_product_in_both_by_reference(reference=reference)
		return result
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/odoo/import-products")
def import_products_from_json(payload: ImportRequest):
	try:
		products = load_products_from_json(payload.json_path)
		return create_odoo_products(products)
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/odoo/products")
def list_odoo_products(limit: int = 100):
	try:
		products = fetch_odoo_products(limit=limit)
		return {"count": len(products), "products": products}
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/odoo/products/by-sku/{sku}")
def get_odoo_product_by_sku(sku: str):
	try:
		product = fetch_odoo_product_by_sku(sku=sku)
		if not product:
			raise HTTPException(status_code=404, detail="Product not found")
		return product
	except HTTPException:
		raise
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/odoo/orders")
def list_odoo_orders(limit: int = 100):
	try:
		orders = fetch_odoo_orders(limit=limit)
		return {"count": len(orders), "orders": orders}
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/odoo/orders/by-reference/{reference}")
def get_odoo_order_by_reference(reference: str):
	try:
		order = fetch_odoo_order_by_reference(reference=reference)
		if not order:
			raise HTTPException(status_code=404, detail="Order not found")
		return order
	except HTTPException:
		raise
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/odoo/customers")
def list_odoo_customers(limit: int = 100):
	try:
		customers = fetch_odoo_customers(limit=limit)
		return {"count": len(customers), "customers": customers}
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/odoo/vendors")
def list_odoo_vendors(limit: int = 100):
	try:
		vendors = fetch_odoo_vendors(limit=limit)
		return {"count": len(vendors), "vendors": vendors}
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/odoo/payments")
def list_odoo_payments(limit: int = 100):
	try:
		payments = fetch_odoo_payments(limit=limit)
		return {"count": len(payments), "payments": payments}
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sync/odoo-to-prestashop")
def sync_odoo_to_prestashop(payload: SyncRequest):
	try:
		products = fetch_odoo_products(limit=payload.limit)
		result = send_products_to_prestashop(products)
		return {
			"odoo_products": len(products),
			"prestashop_result": result,
		}
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/prestashop/products")
def list_prestashop_products(limit: int = 100):
	try:
		products = fetch_prestashop_products(limit=limit)
		return {"count": len(products), "products": products}
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/prestashop/orders")
def list_prestashop_orders(limit: int = 100):
	try:
		orders = fetch_prestashop_orders(limit=limit)
		return {"count": len(orders), "orders": orders}
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/prestashop/orders/by-reference/{reference}")
def get_prestashop_order_by_reference(reference: str):
	try:
		order = fetch_prestashop_order_by_reference(reference=reference)
		if not order:
			raise HTTPException(status_code=404, detail="Order not found")
		return order
	except HTTPException:
		raise
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/prestashop/customers")
def list_prestashop_customers(limit: int = 100):
	try:
		customers = fetch_prestashop_customers(limit=limit)
		return {"count": len(customers), "customers": customers}
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/sync/logs")
def list_sync_logs(limit: int = 20):
	try:
		return get_sync_logs(limit=limit)
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/sync/mappings")
def list_product_mappings(limit: int = 100):
	try:
		return get_product_mappings(limit=limit)
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc