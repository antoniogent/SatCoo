-- =========================================================================
-- Tabella monitored_satellites: satelliti che un utente PRO/PRO PLUS
-- vuole monitorare per ricevere alert email ad ogni nuova pubblicazione
-- BR IFIC. Il limite (1 per PRO, 3 per PRO PLUS) è applicato in app.py,
-- non qui a livello di database.
-- =========================================================================

create table if not exists public.monitored_satellites (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    satellite_name text not null,
    created_at timestamptz not null default now(),
    unique (user_id, satellite_name)
);

alter table public.monitored_satellites enable row level security;

create policy "Users can view own monitored satellites"
    on public.monitored_satellites for select
    using (auth.uid() = user_id);

create policy "Users can delete own monitored satellites"
    on public.monitored_satellites for delete
    using (auth.uid() = user_id);

create index if not exists idx_monitored_satellites_user
    on public.monitored_satellites (user_id);

-- Indice usato dallo script di invio alert per trovare velocemente,
-- per ogni satellite monitorato, chi avvisare.
create index if not exists idx_monitored_satellites_name
    on public.monitored_satellites (satellite_name);
