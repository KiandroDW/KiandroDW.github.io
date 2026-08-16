// Supabase Configurations

const SUPABASE_URL =
    "https://hupelowqbycjbnqqpogl.supabase.co";

const SUPABASE_PUBLISHABLE_KEY =
    "sb_publishable_42J5B9Mi57UubiOKryvp0g_X4VpOpZJ";

const supabaseClient = supabase.createClient(
    SUPABASE_URL,
    SUPABASE_PUBLISHABLE_KEY
);
