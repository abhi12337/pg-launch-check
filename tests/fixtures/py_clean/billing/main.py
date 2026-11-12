from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.orm import selectinload

from .db import SessionLocal, pool
from .models import Customer, Invoice

app = FastAPI()


@app.get("/customers")
def list_customers(after: int = 0):
    with SessionLocal() as session:
        stmt = select(Customer).where(Customer.id > after).order_by(Customer.id).limit(50)
        return [{"id": c.id, "name": c.name} for c in session.scalars(stmt)]


@app.get("/customers/with-invoices")
def customers_with_invoices():
    with SessionLocal() as session:
        stmt = (
            select(Customer.name, func.coalesce(func.sum(Invoice.amount), 0))
            .join(Invoice, Invoice.customer_id == Customer.id, isouter=True)
            .group_by(Customer.id)
            .order_by(Customer.id)
            .limit(100)
        )
        return [{"name": name, "total": total} for name, total in session.execute(stmt)]


@app.get("/invoices")
def recent_invoices():
    with SessionLocal() as session:
        rows = session.scalars(
            select(Invoice).options(selectinload(Invoice.customer)).order_by(Invoice.id.desc()).limit(50)
        ).all()
        result = []
        for inv in rows:
            result.append({"id": inv.id, "customer": inv.customer.name})
        return result


@app.get("/invoices/search")
def search_invoices(q: str, after: int = 0):
    with SessionLocal() as session:
        sql = text("SELECT id, note, amount FROM invoices WHERE note ILIKE :pattern AND id > :after ORDER BY id LIMIT 50")
        return [dict(r._mapping) for r in session.execute(sql, {"pattern": f"%{q}%", "after": after})]


@app.get("/customers/by-email")
def customer_by_email(email: str):
    with pool.connection() as conn:
        return conn.execute("SELECT id, name FROM customers WHERE email = %s", (email,)).fetchone()
