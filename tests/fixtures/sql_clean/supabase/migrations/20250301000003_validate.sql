set lock_timeout = '5s';

alter table public.products validate constraint products_sku_not_null;
alter table public.products alter column sku set not null;   -- uses the validated CHECK, no scan
alter table public.products drop constraint products_sku_not_null;
alter table public.products add constraint products_sku_key unique using index products_sku_key;
alter table public.order_items validate constraint order_items_quantity_positive;
alter table public.orders validate constraint orders_profile_fk;
