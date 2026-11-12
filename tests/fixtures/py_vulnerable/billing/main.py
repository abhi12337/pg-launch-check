import psycopg
from fastapi import FastAPI
from sqlalchemy import select, text

from .db import DATABASE_URL, SessionLocal
from .models import Customer, Invoice

app = FastAPI()


@app.get("/customers")
def list_customers():
    with SessionLocal() as session:
        customers = session.scalars(select(Customer)).all()
        return [{"id": c.id, "name": c.name} for c in customers]


@app.get("/customers/with-invoices")
def customers_with_invoices():
    with SessionLocal() as session:
        out = []
        for customer in session.scalars(select(Customer).order_by(Customer.id).limit(100)):
            invoices = session.scalars(select(Invoice).where(Invoice.customer_id == customer.id).limit(20)).all()
            out.append({"name": customer.name, "total": sum(i.amount for i in invoices)})
        return out


@app.get("/invoices")
def recent_invoices():
    with SessionLocal() as session:
        rows = session.scalars(select(Invoice).order_by(Invoice.id.desc()).limit(50)).all()
        result = []
        for inv in rows:
            result.append({"id": inv.id, "customer": inv.customer.name})
        return result


@app.get("/invoices/search")
def search_invoices(q: str, page: int = 0):
    with SessionLocal() as session:
        sql = f"SELECT * FROM invoices WHERE note LIKE '%{q}%' ORDER BY id LIMIT 50 OFFSET {page * 50}"
        return [dict(r._mapping) for r in session.execute(text(sql))]


@app.get("/customers/by-email")
def customer_by_email(email: str):
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM customers WHERE email = '%s'" % email)
        return cur.fetchone()
