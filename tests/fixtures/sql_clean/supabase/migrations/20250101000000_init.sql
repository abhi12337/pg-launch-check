-- Shop schema: profiles, products, orders, order items.

create table public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  username text not null unique,
  created_at timestamptz not null default now()
);

create table public.products (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  price numeric(12, 2) not null,
  created_at timestamptz not null default now()
);

create table public.orders (
  id bigint generated always as identity primary key,
  profile_id uuid not null references public.profiles (id),
  total_cents bigint not null,
  status text not null default 'pending',
  created_at timestamptz not null default now()
);

create index orders_profile_id_idx on public.orders (profile_id);

create table public.order_items (
  order_id bigint not null references public.orders (id) on delete cascade,
  product_id uuid not null references public.products (id),
  quantity int not null check (quantity > 0),
  primary key (order_id, product_id)
);

create index order_items_product_id_idx on public.order_items (product_id);

create extension if not exists pg_trgm;
create index products_name_trgm_idx on public.products using gin (name gin_trgm_ops);

alter table public.orders enable row level security;
alter table public.order_items enable row level security;
