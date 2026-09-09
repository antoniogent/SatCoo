-- =========================================================================
-- Tabella profiles: piano utente, collegata a Supabase Auth
-- Esegui tutto questo script in un colpo solo nell'SQL Editor di Supabase
-- =========================================================================

create table if not exists public.profiles (
    user_id uuid primary key references auth.users(id) on delete cascade,
    email text,
    plan text not null default 'free' check (plan in ('free', 'pro', 'pro_plus')),
    stripe_customer_id text,
    stripe_subscription_id text,
    monitored_sat text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Row Level Security: ogni utente vede/modifica solo il proprio profilo.
-- La SUPABASE_SERVICE_ROLE_KEY che usa l'app Streamlit e il futuro webhook
-- bypassano comunque queste regole (accesso admin), quindi non si rompe nulla.
alter table public.profiles enable row level security;

create policy "Users can view own profile"
    on public.profiles for select
    using (auth.uid() = user_id);

create policy "Users can update own profile"
    on public.profiles for update
    using (auth.uid() = user_id);

-- Trigger: ad ogni nuova registrazione in auth.users, crea automaticamente
-- la riga corrispondente in profiles con plan='free'. Così get_user_plan()
-- nell'app trova sempre un profilo, anche per chi si registra oggi.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
    insert into public.profiles (user_id, email)
    values (new.id, new.email);
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute procedure public.handle_new_user();

-- Indice utile per il futuro webhook (lookup per customer Stripe)
create index if not exists idx_profiles_stripe_customer
    on public.profiles (stripe_customer_id);
