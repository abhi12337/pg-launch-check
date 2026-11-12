from django.db import connection
from django.http import JsonResponse

from .models import Book


def book_list(request):
    books = Book.objects.select_related("author").order_by("-published_at")[:50]
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
        cur.execute("SELECT b.id, b.title FROM library_book b JOIN library_author a ON a.id = b.author_id "
                    "WHERE a.name = %s LIMIT 100", [name])
        return JsonResponse(cur.fetchall(), safe=False)


def page(request):
    after = int(request.GET.get("after", 0))
    books = Book.objects.filter(id__gt=after).order_by("id")[:20]
    return JsonResponse([b.title for b in books], safe=False)
