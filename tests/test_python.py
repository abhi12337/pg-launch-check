"""Python application code rules: SQLAlchemy, Django, psycopg, asyncpg."""

REQ = "sqlalchemy\npsycopg[binary]\nasyncpg\n"


def py(scan, code: str, name: str = "app/main.py", **files):
    return scan({name: code, "requirements.txt": REQ, **files})


SA_MODELS = '''
from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    posts: Mapped[list["Post"]] = relationship(back_populates="user")


class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    user: Mapped["User"] = relationship(back_populates="posts")
'''

DJ_MODELS = '''
from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=100)


class Book(models.Model):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")
    tags = models.ManyToManyField("Tag")


class Tag(models.Model):
    name = models.CharField(max_length=50)
'''


# ---------------------------------------------------------------- PGL011
def test_sql_injection_patterns(scan):
    res = py(scan, '''
        from sqlalchemy import text

        def a(cur, email):
            cur.execute(f"SELECT id FROM users WHERE email = '{email}'")

        def b(cur, email):
            cur.execute("SELECT id FROM users WHERE email = '%s'" % email)

        def c(cur, email):
            sql = "SELECT id FROM users WHERE email = '" + email + "'"
            cur.execute(sql)

        def d(session, name):
            session.execute(text("DELETE FROM users WHERE name = '{}'".format(name)))

        async def e(conn, q):
            return await conn.fetch(f"SELECT id FROM users WHERE name LIKE '{q}' LIMIT 5")

        def f(Book, title):
            return Book.objects.raw(f"SELECT * FROM library_book WHERE title = '{title}'")
        ''')
    assert [(r[0], r[2], r[3]) for r in res if r[0] == "PGL011"] == [
        ("PGL011", 4, "high"), ("PGL011", 7, "high"), ("PGL011", 11, "high"), ("PGL011", 14, "high"),
        ("PGL011", 17, "high"), ("PGL011", 20, "high")]


def test_parameterized_sql_is_safe(scan):
    res = py(scan, '''
        from psycopg import sql
        from sqlalchemy import text

        TABLE = "users"
        PAGE = 20

        def a(cur, session, conn, email, ids, limit):
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            cur.execute(f"SELECT id FROM {TABLE} WHERE id = %s LIMIT {PAGE}", (email,))
            marks = ", ".join(["%s"] * len(ids))
            cur.execute(f"SELECT id FROM users WHERE id IN ({marks})", ids)
            cur.execute("SELECT id FROM users ORDER BY id LIMIT %d" % int(limit))
            cur.execute(f"SELECT id FROM users LIMIT {int(limit)}")
            session.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email})
            cur.execute(sql.SQL("SELECT id FROM {} WHERE email = %s").format(sql.Identifier(TABLE)), (email,))
        ''')
    assert [r for r in res if r[0] == "PGL011"] == []


# ---------------------------------------------------------------- PGL012 / 018 / 019
def test_connections_per_request(scan):
    res = py(scan, '''
        import asyncpg
        import psycopg
        from fastapi import FastAPI
        from sqlalchemy import create_engine

        app = FastAPI()

        @app.get("/a")
        def a():
            engine = create_engine("postgresql+psycopg://localhost/app")
            return engine

        @app.get("/b")
        async def b():
            conn = await asyncpg.connect("postgresql://localhost/app")
            return await conn.fetch("SELECT 1")

        def view(request):
            with psycopg.connect("postgresql://localhost/app") as conn:
                return conn.execute("SELECT 1").fetchone()
        ''')
    assert [(r[0], r[2], r[3]) for r in res] == [("PGL012", 10, "high"), ("PGL012", 15, "medium"),
                                                 ("PGL012", 19, "medium")]


def test_pool_size_and_statement_timeout(scan):
    res = py(scan, '''
        import asyncpg
        from psycopg_pool import ConnectionPool
        from sqlalchemy import create_engine

        engine = create_engine("postgresql+psycopg://localhost/app")
        sqlite = create_engine("sqlite:///local.db")

        async def init():
            return await asyncpg.create_pool("postgresql://localhost/app")

        pool = ConnectionPool("postgresql://localhost/app")
        ''', name="app/db.py")
    assert [(r[0], r[2]) for r in res] == [("PGL018", 5), ("PGL018", 9), ("PGL018", 11), ("PGL019", 5),
                                           ("PGL019", 9), ("PGL019", 11)]
    ok = py(scan, '''
        import asyncpg
        from psycopg_pool import ConnectionPool
        from sqlalchemy import create_engine

        engine = create_engine("postgresql+psycopg://localhost/app", pool_size=10, max_overflow=5,
                               connect_args={"options": "-c statement_timeout=15000"})

        async def init():
            return await asyncpg.create_pool("postgresql://x/app", max_size=10, command_timeout=15)

        pool = ConnectionPool("postgresql://x/app", max_size=10, kwargs={"options": "-c statement_timeout=15000"})
        ''', name="app/db.py")
    assert ok == []


# ---------------------------------------------------------------- PGL013
def test_explicit_query_in_loop(scan):
    res = py(scan, '''
        from sqlalchemy import select
        from .models import Post, User

        def a(session, users, cur, Book):
            for u in users:
                posts = session.scalars(select(Post).where(Post.user_id == u.id)).all()
            names = [session.get(User, pid) for pid in [1, 2, 3]]
            for row in cur.fetchall():
                cur.execute("SELECT id FROM tags WHERE post_id = %s", (row[0],))
            for b in Book.objects.all()[:10]:
                count = Book.objects.filter(author_id=b.author_id).count()
            for u in users:
                session.add(Post(user_id=u.id))
            while users:
                users = session.scalars(select(User).where(User.id > users[-1].id).limit(100)).all()
        ''', **{"app/models.py": SA_MODELS})
    assert [(r[0], r[1], r[2]) for r in res] == [("PGL013", "app/main.py", 6), ("PGL013", "app/main.py", 7),
                                                 ("PGL013", "app/main.py", 9), ("PGL013", "app/main.py", 11)]


def test_lazy_relationships(scan):
    res = py(scan, '''
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from library.models import Book
        from .models import Post, User

        def sa(session):
            for p in session.scalars(select(Post).limit(20)):
                print(p.user.id)
            rows = session.scalars(select(Post).options(selectinload(Post.user)).limit(20)).all()
            for p in rows:
                print(p.user.id)

        def dj():
            for b in Book.objects.order_by("id")[:20]:
                print(b.author.name, b.author_id)
            for b in Book.objects.select_related("author")[:20]:
                print(b.author.name)
            for b in Book.objects.all()[:20]:
                print(b.tags.all())
            for b in Book.objects.prefetch_related("tags")[:20]:
                print(b.tags.all())
        ''', **{"app/models.py": SA_MODELS, "library/models.py": DJ_MODELS, "library/__init__.py": ""})
    assert [(r[0], r[2]) for r in res] == [("PGL013", 8), ("PGL013", 15), ("PGL013", 19)]


def test_async_sqlalchemy_lazy_load_is_not_reported(scan):
    res = py(scan, '''
        from sqlalchemy import select
        from .models import Post

        async def a(session):
            rows = (await session.scalars(select(Post).limit(20))).all()
            for p in rows:
                print(p.user)
        ''', **{"app/models.py": SA_MODELS})
    assert res == []


# ---------------------------------------------------------------- PGL014 / 015
def test_unbounded_in_handlers(scan):
    res = py(scan, '''
        from django.core.paginator import Paginator
        from sqlalchemy import select
        from fastapi import FastAPI
        from .models import Post, User

        app = FastAPI()

        @app.get("/posts")
        def posts(session):
            return session.scalars(select(Post)).all()

        @app.get("/posts/ok")
        def posts_ok(session, ids: list[int]):
            a = session.scalars(select(Post).limit(50)).all()
            b = session.scalars(select(Post).where(Post.id.in_(ids))).all()
            return a, b

        def book_list(request, Book):
            return list(Book.objects.filter(published=True))

        def book_ok(request, Book):
            first = Book.objects.filter(published=True).first()
            page = Book.objects.order_by("id")[:20]
            one = Book.objects.filter(pk=3)
            return first, page, one

        def paged(request, Book):
            return Paginator(Book.objects.all(), 25).page(1)

        def job(session):
            return session.scalars(select(Post)).all()
        ''', **{"app/models.py": SA_MODELS})
    assert [(r[0], r[2]) for r in res] == [("PGL014", 10), ("PGL014", 19)]


def test_offset(scan):
    res = py(scan, '''
        from sqlalchemy import select
        from .models import Post

        def a(session, page, Book):
            session.scalars(select(Post).order_by(Post.id).offset(page * 20).limit(20))
            session.scalars(select(Post).order_by(Post.id).offset(0).limit(20))
            start = page * 20
            return Book.objects.order_by("id")[start:start + 20], Book.objects.all()[0:20]
        ''', **{"app/models.py": SA_MODELS})
    assert [(r[0], r[2], r[3]) for r in res] == [("PGL015", 5, "info"), ("PGL015", 8, "info")]


# ---------------------------------------------------------------- PGL016 / 017
def test_select_star_and_like(scan):
    res = py(scan, '''
        """Docs: SELECT * FROM users is an example in a docstring."""
        from .models import Post

        Q1 = "SELECT * FROM users WHERE id = %s"
        Q2 = "SELECT count(*) FROM users"
        Q3 = "SELECT id FROM users WHERE name ILIKE '%' || %s || '%' LIMIT 10"

        def search(Book, session, q):
            Book.objects.filter(title__icontains=q)[:10]
            Book.objects.filter(title__istartswith=q)[:10]
            Book.objects.filter(tags__contains=["x"])[:10]
            session.query(Post).filter(Post.title.ilike(f"%{q}%")).limit(10).all()
            session.query(Post).filter(Post.title.ilike(f"{q}%")).limit(10).all()
        ''', **{"app/models.py": SA_MODELS + "    title: Mapped[str] = mapped_column()\n"})
    assert [(r[0], r[2]) for r in res] == [("PGL016", 4), ("PGL017", 6), ("PGL017", 9), ("PGL017", 12)]


def test_trigram_index_from_django_meta_suppresses(scan):
    models = DJ_MODELS.replace(
        'tags = models.ManyToManyField("Tag")',
        'tags = models.ManyToManyField("Tag")\n\n    class Meta:\n'
        '        indexes = [GinIndex(fields=["title"], name="t_trgm", opclasses=["gin_trgm_ops"])]')
    res = scan({"library/models.py": "from django.contrib.postgres.indexes import GinIndex\n" + models,
                "library/views.py": "from .models import Book\n\ndef s(request):\n"
                                    "    return list(Book.objects.filter(title__icontains=request.GET['q'])[:10])\n",
                "requirements.txt": "django\npsycopg\n"})
    assert res == []


# ---------------------------------------------------------------- model rules
def test_sqlalchemy_and_django_model_rules(scan_findings):
    fs = scan_findings({
        "app/models.py": '''
from datetime import datetime
from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_shop", "shop_id", "created_at"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    shop_id = Column(Integer, ForeignKey("shops.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    total = Column(Float)
    score = Column(Float)
    created_at = Column(DateTime)
    paid_at: Mapped[datetime]
    shipped_at = Column(DateTime(timezone=True))
''',
        "shop/models.py": '''
from django.db import models


class Item(models.Model):
    price = models.FloatField()
    weight = models.FloatField()
    created = models.DateTimeField()
''',
        "requirements.txt": "sqlalchemy\npsycopg\ndjango\n",
    })
    got = sorted((f.rule_id, f.file, f.line) for f in fs)
    assert got == [("PGL001", "app/models.py", 14), ("PGL009", "app/models.py", 17),
                   ("PGL009", "shop/models.py", 5), ("PGL010", "app/models.py", 19)]
    pgl010 = next(f for f in fs if f.rule_id == "PGL010")
    assert "created_at, paid_at" in pgl010.message


def test_non_postgres_projects_skip_model_rules(scan):
    res = scan({"app/models.py": "from sqlalchemy import Column, Float, Integer\n"
                                 "from sqlalchemy.orm import DeclarativeBase\n\n"
                                 "class Base(DeclarativeBase):\n    pass\n\n"
                                 "class P(Base):\n    __tablename__ = 'p'\n    id = Column(Integer, primary_key=True)\n"
                                 "    price = Column(Float)\n",
                "app/db.py": "from sqlalchemy import create_engine\nengine = create_engine('sqlite:///x.db')\n"})
    assert res == []


def test_sqlite_files_are_out_of_scope(scan):
    res = py(scan, '''
        import sqlite3

        def find(db, term):
            return db.execute(f"SELECT * FROM notes WHERE title LIKE '%{term}%'").fetchall()
        ''')
    assert res == []


def test_inline_ignore_in_python(scan):
    res = py(scan, '''
        Q1 = "SELECT * FROM a"  # pg-launch-check: ignore
        # pg-launch-check: ignore PGL016
        Q2 = "SELECT * FROM b"
        Q3 = "SELECT * FROM c"  # pg-launch-check: ignore PGL001
        ''')
    assert [(r[0], r[2]) for r in res] == [("PGL016", 4)]
