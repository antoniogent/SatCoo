-- =========================================================================
-- Tabella purchases: acquisti one-time "Single Analysis" (€49)
-- Distinta da profiles.plan, che riguarda solo gli abbonamenti PRO/PRO PLUS
-- =========================================================================

create table if not exists public.purchases (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    report_id text not null,
    stripe_checkout_session_id text,
    created_at timestamptz not null default now(),
    unique (user_id, report_id)
);

alter table public.purchases enable row level security;

create policy "Users can view own purchases"
    on public.purchases for select
    using (auth.uid() = user_id);

create index if not exists idx_purchases_user_report
    on public.purchases (user_id, report_id);
