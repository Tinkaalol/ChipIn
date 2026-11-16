-- Таблица заявок на оплату. Выполните один раз в Supabase: SQL Editor -> New query -> Run.
create table if not exists public.applications (
    id uuid primary key default gen_random_uuid(),
    action text not null check (action in ('create', 'join')),
    service_slug text not null,
    group_id uuid,
    email text not null,
    tg_username text,
    tg_id bigint not null,
    tg_name text,
    amount_kzt int not null,
    status text not null default 'pending'
        check (status in ('pending', 'approved', 'rejected', 'failed')),
    created_at timestamptz not null default now()
);

-- В заявках чужие email: закрываем доступ для публичного ключа. Бот работает с ключом
-- service_role, он обходит RLS.
alter table public.applications enable row level security;
