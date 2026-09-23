// Copy this file to config.js and fill in your own Supabase project's
// public URL and "anon" (public, read-only) key -- both are safe to put in
// a public website's code, since Row Level Security (set up in
// sql/schema.sql) only allows this key to read, never write.
//
// Find these in the Supabase dashboard: Project Settings -> API.
//   SUPABASE_URL      -> "Project URL"
//   SUPABASE_ANON_KEY -> "anon public" key (NOT the "service_role" key --
//                         that one is secret and must never appear here)

window.USCORE_CONFIG = {
  SUPABASE_URL: "https://YOUR-PROJECT-REF.supabase.co",
  SUPABASE_ANON_KEY: "YOUR-ANON-PUBLIC-KEY",
};
