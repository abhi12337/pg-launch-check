-- Order tracking, SKUs and some constraints.

create index orders_status_idx on public.orders (status);

alter table public.orders add column tracking_id uuid default gen_random_uuid();
alter table public.orders add column shipped_at timestamptz;
alter table public.orders add column currency text not null;
alter table public.orders alter column status type varchar(20);

alter table public.products add column sku text;
alter table public.products alter column sku set not null;
alter table public.products add constraint products_sku_key unique (sku);

alter table public.order_items add constraint order_items_quantity_check check (quantity > 0);
alter table public.orders add constraint orders_profile_fk foreign key (profile_id) references public.profiles (id);
