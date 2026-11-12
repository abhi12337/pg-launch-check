from django.db import connection
from django.http import JsonResponse

from .models import Book


def book_list(request):
    books = Book.objects.order_by("-published_at")
    data = []
    for book in books:
        data.append({"title": book.title, "author": book.author.name})
    return JsonResponse(data, safe=False)


def search(request):
    q = request.GET.get("q", "")
    books = Book.objects.filter(title__icontains=q)[:20]
    return JsonResponse([b.title for b in books], safe=False)


def books_by_author(request):
    name = request.GET["name"]
    with connection.cursor() as cur:
        cur.execute(f"SELECT b.id, b.title FROM library_book b JOIN library_author a ON a.id = b.author_id "
                    f"WHERE a.name = '{name}' LIMIT 100")
        return JsonResponse(cur.fetchall(), safe=False)


def page(request):
    n = int(request.GET.get("page", 1))
    books = Book.objects.order_by("id")[(n - 1) * 20:n * 20]
    return JsonResponse([b.title for b in books], safe=False)
