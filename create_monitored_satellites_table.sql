-- =========================================================================
-- Tabella monitored_satellites: beam specifici che un utente PRO PLUS
-- vuole monitorare per ricevere alert email ad ogni nuova pubblicazione
-- BR IFIC. Il limite (3 per PRO PLUS) è applicato in app.py, non qui.
--
-- NB: questo file riflette lo schema FINALE (satellite + beam specifico).
-- Se stai leggendo la cronologia di questo progetto, la versione originale
-- salvava solo il nome del satellite; è stata aggiornata con
-- alter_monitored_satellites_add_beam.sql per includere anche il beam,
-- così il calcolo delle frequenze coincide esattamente con quello usato
-- nella ricerca manuale (che richiede sempre di scegliere un beam).
-- =========================================================================

create table if not exists public.monitored_satellites (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    satellite_name text not null,
    beam_name text not null,
    created_at timestamptz not null default now(),
    unique (user_id, satellite_name, beam_name)
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
-- per ogni satellite/beam monitorato, chi avvisare.
create index if not exists idx_monitored_satellites_name
    on public.monitored_satellites (satellite_name, beam_name);
