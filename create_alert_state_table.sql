-- =========================================================================
-- Tabella alert_state: un'unica riga che tiene traccia dell'ultima
-- pubblicazione BR IFIC (WIC) per cui abbiamo già inviato gli alert email.
-- Evita di rimandare le stesse notifiche ad ogni esecuzione dello script.
-- =========================================================================

create table if not exists public.alert_state (
    id integer primary key default 1,
    last_alerted_wic integer not null default 0,
    updated_at timestamptz not null default now(),
    constraint alert_state_singleton check (id = 1)
);

insert into public.alert_state (id, last_alerted_wic)
values (1, 0)
on conflict (id) do nothing;

-- Nessuna RLS qui: questa tabella viene letta/scritta solo dallo script
-- server-side send_interference_alerts.py con la service_role key, mai
-- dal browser dell'utente.
