-- Order tracking and SKUs, without long locks.
set lock_timeout = '5s';

alter table public.orders add column tracking_id uuid;
alter table public.orders alter column tracking_id set default gen_random_uuid();  -- new rows only
alter table public.orders add column shipped_at timestamptz;
alter table public.orders add column currency text not null default 'USD';        -- constant default: instant

alter table public.products add column sku text;
alter table public.products add constraint products_sku_not_null check (sku is not null) not valid;
alter table public.order_items add constraint order_items_quantity_positive check (quantity > 0) not valid;
alter table public.orders add constraint orders_profile_fk foreign key (profile_id) references public.profiles (id) not valid;
