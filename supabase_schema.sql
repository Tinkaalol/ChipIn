-- Доработка таблицы groups под бота. Выполните один раз: Supabase -> SQL Editor -> New query -> Run.
-- Скрипт можно запускать повторно.

alter table public.groups add column if not exists price_per_slot int;
alter table public.groups add column if not exists billing_date date;
alter table public.groups add column if not exists invite_link text;
alter table public.groups add column if not exists members jsonb not null default '[]'::jsonb;

-- Email организатора бот больше не спрашивает
alter table public.groups alter column creator_email drop not null;

-- Новые группы создаются со статусом open (раньше было collecting)
alter table public.groups alter column status set default 'open';

-- Если вставка падает с ошибкой про check constraint на status, посмотрите ограничения:
--   select conname, pg_get_constraintdef(oid) from pg_constraint where conrelid = 'public.groups'::regclass;
-- и разрешите значения open и full, например:
--   alter table public.groups drop constraint <имя из запроса>;
--   alter table public.groups add constraint groups_status_check check (status in ('open', 'full', 'collecting'));
