from supabase import create_client, Client
from config import Config

def get_supabase_client() -> Client:
    return create_client(Config.SUPABASE_URL, Config.SUPABASE_KEY)