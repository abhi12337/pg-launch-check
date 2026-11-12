-- Shop schema: profiles, products, orders, order items.

create table public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  username text not null unique,
  created_at timestamp not null default now()
);

create table public.products (
  id text primary key default gen_random_uuid()::text,
  name text not null,
  price double precision not null,
  created_at timestamptz not null default now()
);

create table public.orders (
  id bigint generated always as identity primary key,
  profile_id uuid not null references public.profiles (id),
  total real not null,
  status text not null default 'pending',
  created_at timestamp not null default now()
);

create table public.order_items (
  order_id bigint not null references public.orders (id) on delete cascade,
  product_id text not null references public.products (id),
  quantity int not null
);

alter table public.orders enable row level security;
alter table public.order_items enable row level security;
