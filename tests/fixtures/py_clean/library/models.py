from django.contrib.postgres.indexes import GinIndex
from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=200)


class Book(models.Model):
    title = models.CharField(max_length=300)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")
    price = models.DecimalField(max_digits=12, decimal_places=2)
    published_at = models.DateTimeField()

    class Meta:
        indexes = [GinIndex(fields=["title"], name="book_title_trgm", opclasses=["gin_trgm_ops"])]
