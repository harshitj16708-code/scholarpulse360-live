from datetime import datetime, date, timedelta
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from dotenv import load_dotenv
from groq import Groq
from supabase import create_client, Client
import json
import os
import re
import requests
import secrets
import subprocess
import urllib.request
from urllib.parse import parse_qs, urlparse
from werkzeug.utils import secure_filename
import xml.etree.ElementTree as ET
from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    YouTubeTranscriptApi,
)
from jinja2.exceptions import TemplateNotFound

# Safe Import for National Holidays
try:
    import holidays
except ImportError:
    holidays = None

# Load Environment Variables
load_dotenv()

# ==========================================
# 1. PATH RESOLUTION & CONFIGURATION
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if os.path.exists(os.path.join(BASE_DIR, 'templates')):
    TEMPLATE_FOLDER = os.path.join(BASE_DIR, 'templates')
    STATIC_FOLDER = os.path.join(BASE_DIR, 'static')
elif os.path.exists(os.path.join(os.path.dirname(BASE_DIR), 'templates')):
    TEMPLATE_FOLDER = os.path.join(os.path.dirname(BASE_DIR), 'templates')
    STATIC_FOLDER = os.path.join(os.path.dirname(BASE_DIR), 'static')
else:
    TEMPLATE_FOLDER = os.path.join(BASE_DIR, 'templates')
    STATIC_FOLDER = os.path.join(BASE_DIR, 'static')

ALLOWED_EXTENSIONS = {
    'png', 'jpg', 'jpeg', 'gif', 'webp',  # Images
    'mp4', 'mov', 'avi', 'mkv',          # Videos
    'txt', 'csv',                        # Text
    'pdf'                                # PDF
}

STORAGE_BUCKET = "ScholarPulse360"

def render_auth_template(primary_template, fallback_template, **context):
    try:
        return render_template(primary_template, **context)
    except TemplateNotFound:
        try:
            return render_template(fallback_template, **context)
        except TemplateNotFound:
            return render_template('login.html', error=context.get('error', 'An error occurred.'))

# ==========================================
# 2. FLASK & SUPABASE SETUP
# ==========================================
app = Flask(
    __name__, template_folder=TEMPLATE_FOLDER, static_folder=STATIC_FOLDER
)

app.secret_key = os.environ.get(
    'SECRET_KEY', 'scholarpulse360_secure_session_key_2026'
)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

def allowed_file(filename):
    return (
        '.' in filename
        and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
    )

def get_scholar_rank(xp):
    """
    Scholar Rank determined strictly from cumulative Scholar XP:
    - Beginner: 0 - 499 XP
    - Intermediate: 500 - 1,499 XP
    - Pro: 1,500 - 3,499 XP
    - Pro+: 3,500 - 6,999 XP
    - Elite: 7,000 - 11,999 XP
    - Master: 12,000 - 19,999 XP
    - Master+: 20,000+ XP
    """
    try:
        xp = int(xp or 0)
    except (ValueError, TypeError):
        xp = 0

    if xp < 500:
        return 'Beginner'
    elif xp < 1500:
        return 'Intermediate'
    elif xp < 3500:
        return 'Pro'
    elif xp < 7000:
        return 'Pro+'
    elif xp < 12000:
        return 'Elite'
    elif xp < 20000:
        return 'Master'
    else:
        return 'Master+'

def apply_inactivity_penalty(user_id, base_xp):
    """
    Deduct 1% of total accumulated XP for every consecutive calendar day
    a user was completely inactive (0% goal completion or no log).
    """
    if not supabase or not user_id:
        return base_xp

    try:
        u_res = supabase.table('users').select('created_at, last_penalty_date').eq('id', user_id).execute()
        if not u_res.data:
            return base_xp

        user_rec = u_res.data[0]
        created_str = user_rec.get('created_at')
        last_penalty_str = user_rec.get('last_penalty_date')

        today = date.today()

        dp_res = supabase.table('daily_progress') \
            .select('record_date, goal_score') \
            .eq('user_id', user_id) \
            .execute()

        active_dates = set()
        for r in (dp_res.data or []):
            if float(r.get('goal_score') or 0) > 0:
                active_dates.add(r.get('record_date'))

        if last_penalty_str:
            start_d = date.fromisoformat(last_penalty_str) + timedelta(days=1)
        elif created_str:
            try:
                start_d = datetime.fromisoformat(created_str.replace('Z', '+00:00')).date()
            except Exception:
                start_d = today
        else:
            start_d = today

        inactive_days = 0
        curr_d = start_d
        while curr_d < today:
            d_str = curr_d.isoformat()
            if d_str not in active_dates:
                inactive_days += 1
            curr_d += timedelta(days=1)

        if inactive_days > 0 and base_xp > 0:
            penalty_factor = (0.99) ** inactive_days
            new_xp = int(base_xp * penalty_factor)
            new_xp = max(0, new_xp)

            yesterday_str = (today - timedelta(days=1)).isoformat()
            supabase.table('users').update({
                'xp': new_xp,
                'last_penalty_date': yesterday_str,
                'scholar_rank': get_scholar_rank(new_xp)
            }).eq('id', user_id).execute()

            return new_xp

    except Exception as e:
        print(f"Error applying inactivity penalty: {e}")

    return base_xp

def fetch_user_by_id(user_id):
    """Fetch complete user profile and state data with safe aliases from Supabase."""
    if not supabase or not user_id:
        return None
    try:
        res = supabase.table('users').select('*').eq('id', user_id).execute()
        if res.data and len(res.data) > 0:
            user_data = res.data[0]

            try:
                stats_res = supabase.table('user_stats').select('*').eq('user_id', user_id).execute()
                if stats_res.data and len(stats_res.data) > 0:
                    stats = stats_res.data[0]
                    user_data['total_goals'] = stats.get('total_goals', 0)
                    user_data['completed_goals'] = stats.get('completed_goals', 0)
                    user_data['water_intake_num'] = stats.get('water_intake', 0)
            except Exception as e:
                print(f"Error fetching user_stats: {e}")

            raw_xp = user_data.get('xp')
            try:
                xp_val = int(raw_xp) if raw_xp is not None else 0
            except (ValueError, TypeError):
                xp_val = 0

            xp_val = apply_inactivity_penalty(user_id, xp_val)
            user_data['xp'] = xp_val
            user_data['scholar_xp'] = xp_val
            user_data['scholar_rank'] = get_scholar_rank(xp_val)
            user_data['rank'] = user_data['scholar_rank']
            
            raw_streak = user_data.get('streak')
            try:
                user_data['streak'] = int(raw_streak) if raw_streak is not None else 0
            except (ValueError, TypeError):
                user_data['streak'] = 0
                
            user_data['active_streak'] = user_data['streak']
            user_data['day_streak'] = user_data['streak']

            user_data.setdefault('completion_rate', 0)
            user_data.setdefault('goal_score', 0)
            user_data.setdefault('completion_pct', 0)
            user_data.setdefault('aura_tag', 'Negative')
            user_data.setdefault('water_intake', '0 ml')
            return user_data
    except Exception as e:
        print(f"Error fetching user: {e}")
    return None

def fetch_leaderboard(current_user_id=None, state_filter=None, tag_filter=None):
    """Fetch all registered users from database ordered by Aura/XP and completion score."""
    if not supabase:
        return [], None

    try:
        users_res = supabase.table('users').select('id, name, username, state, avatar_url, xp, streak, scholar_rank, bio').execute()
        raw_users = users_res.data or []

        leaderboard = []
        today_str = date.today().isoformat()

        for u in raw_users:
            uid = u.get('id')
            if not uid:
                continue

            user_state = (u.get('state') or 'Maharashtra').strip()

            if state_filter and state_filter.lower() != 'all':
                if user_state.lower() != state_filter.lower():
                    continue

            metrics = compute_goal_score_and_aura(uid, today_str, update_db=False)

            if tag_filter and tag_filter.lower() != 'all':
                t_filter = tag_filter.lower()
                if t_filter in ['chad', 'chad 🗿'] and metrics['aura_category'] != 'Chad':
                    continue
                elif t_filter in ['master', 'positive', '+999 aura 🗿🔥'] and metrics['aura_category'] != 'Master':
                    continue
                elif t_filter in ['negative', '-999 aura 😤'] and metrics['aura_category'] != 'Negative':
                    continue

            leaderboard.append({
                'id': uid,
                'name': u.get('name') or u.get('username') or 'Scholar',
                'username': u.get('username') or '',
                'state': user_state,
                'avatar_url': u.get('avatar_url') or '',
                'avatar': u.get('avatar_url') or '',
                'xp': metrics['xp'],
                'scholar_xp': metrics['xp'],
                'streak': metrics['streak'],
                'active_streak': metrics['streak'],
                'scholar_rank': metrics['scholar_rank'],
                'rank': metrics['scholar_rank'],
                'score': metrics['score'],
                'goal_score': metrics['score'],
                'completion_rate': metrics['score'],
                'completion_pct': metrics['score'],
                'goals_completion_pct': metrics['score'],
                'aura': metrics['aura'],
                'aura_title': metrics['aura_title'],
                'aura_category': metrics['aura_category'],
                'aura_tag': metrics['aura_tag'],
                'water_intake': f"{metrics['water_ml']} ml",
                'is_current_user': (uid == current_user_id) if current_user_id else False
            })

        leaderboard.sort(key=lambda x: (x['xp'], x['score']), reverse=True)

        my_position = None
        for index, item in enumerate(leaderboard, start=1):
            item['position'] = index
            item['rank_num'] = index
            if item['is_current_user']:
                my_position = item

        if current_user_id and not my_position:
            cur_u = fetch_user_by_id(current_user_id)
            if cur_u:
                cur_metrics = compute_goal_score_and_aura(current_user_id, today_str, update_db=False)
                my_position = {
                    'id': current_user_id,
                    'name': cur_u.get('name') or cur_u.get('username') or 'Scholar',
                    'username': cur_u.get('username') or '',
                    'state': cur_u.get('state') or 'Maharashtra',
                    'avatar_url': cur_u.get('avatar_url') or '',
                    'avatar': cur_u.get('avatar_url') or '',
                    'xp': cur_metrics['xp'],
                    'scholar_xp': cur_metrics['xp'],
                    'streak': cur_metrics['streak'],
                    'active_streak': cur_metrics['streak'],
                    'scholar_rank': cur_metrics['scholar_rank'],
                    'rank': cur_metrics['scholar_rank'],
                    'score': cur_metrics['score'],
                    'goal_score': cur_metrics['score'],
                    'completion_rate': cur_metrics['score'],
                    'completion_pct': cur_metrics['score'],
                    'goals_completion_pct': cur_metrics['score'],
                    'aura': cur_metrics['aura'],
                    'aura_title': cur_metrics['aura_title'],
                    'aura_category': cur_metrics['aura_category'],
                    'aura_tag': cur_metrics['aura_tag'],
                    'water_intake': f"{cur_metrics['water_ml']} ml",
                    'is_current_user': True,
                    'position': '-',
                    'rank_num': '-'
                }

        return leaderboard, my_position
    except Exception as e:
        print(f"Error fetching leaderboard: {e}")
        return [], None

# ==========================================
# GROQ AI HELPER FUNCTION (GPT-120B ONLY)
# ==========================================
def ask_groq_ai(system_prompt, user_content):
    if not GROQ_API_KEY:
        return 'Error: GROQ_API_KEY missing in .env file.'

    headers = {
        'Authorization': f'Bearer {GROQ_API_KEY}',
        'Content-Type': 'application/json',
    }
   
    payload = {
        'model': 'openai/gpt-oss-120b',
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_content},
        ],
        'temperature': 0.5,
        'max_tokens': 1500,
    }
    try:
        response = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            json=payload,
            headers=headers,
            timeout=30,
        )
        res_data = response.json()
        if 'choices' in res_data:
            return res_data['choices'][0]['message']['content']
        else:
            return f"AI Error: {res_data.get('error', {}).get('message', 'Unknown Error')}"
    except Exception as e:
        return f'Network Error: {str(e)}'

# ==========================================
# YOUTUBE EXTRACTION & TRANSCRIPT ENGINE
# ==========================================
def extract_youtube_video_id(url):
    if not url:
        return None
    url = url.strip()

    try:
        parsed = urlparse(url)
        if parsed.hostname in ['www.youtube.com', 'youtube.com', 'm.youtube.com', 'music.youtube.com']:
            query = parse_qs(parsed.query)
            if 'v' in query and query['v']:
                return query['v'][0]
        elif parsed.hostname == 'youtu.be':
            path = parsed.path.lstrip('/')
            if path:
                return path.split('/')[0]
    except Exception:
        pass

    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11})',
        r'(?:embed\/|v\/|vi\/|shorts\/|live\/)([0-9A-Za-z_-]{11})',
        r'youtu\.be\/([0-9A-Za-z_-]{11})',
        r'^([0-9A-Za-z_-]{11})$',
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    return None

def get_youtube_transcript(video_id):
    if not video_id:
        return None

    video_id = video_id.strip()

    try:
        data = YouTubeTranscriptApi.get_transcript(
            video_id,
            languages=['en', 'en-US', 'en-GB', 'hi', 'hi-IN', 'es', 'fr', 'de', 'ja', 'ko'],
        )
        text = ' '.join([item['text'] for item in data if item.get('text')])
        if text:
            return text
    except Exception as e:
        print(f'[Method 1 Failed] {video_id}: {e}')

    try:
        ytt = YouTubeTranscriptApi()
        transcript_list = ytt.list(video_id)

        for t in transcript_list:
            try:
                data = t.fetch()
                text = ' '.join([item['text'] for item in data if item.get('text')])
                if text:
                    return text
            except Exception:
                continue

        for t in transcript_list:
            if t.is_translatable:
                try:
                    data = t.translate('en').fetch()
                    text = ' '.join([item['text'] for item in data if item.get('text')])
                    if text:
                        return text
                except Exception:
                    continue
    except Exception as e:
        print(f'[Method 2 Failed] {video_id}: {e}')

    try:
        url = f'https://www.youtube.com/watch?v={video_id}'
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                ' (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
        }
        req = urllib.request.Request(url, headers=headers)
        html = urllib.request.urlopen(req, timeout=10).read().decode('utf-8')

        match = re.search(r'ytInitialPlayerResponse\s*=\s*({.+?});(?:var|\n|<)', html)
        if not match:
            match = re.search(r'ytInitialPlayerResponse\s*=\s*({.+?});', html)

        if match:
            player_data = json.loads(match.group(1))
            captions = (
                player_data.get('captions', {})
                .get('playerCaptionsTracklistRenderer', {})
                .get('captionTracks', [])
            )

            if captions:
                base_url = captions[0].get('baseUrl')
                if base_url:
                    xml_req = urllib.request.Request(base_url, headers=headers)
                    xml_data = urllib.request.urlopen(xml_req, timeout=10).read().decode('utf-8')
                    root = ET.fromstring(xml_data)
                    scraped = ' '.join([elem.text for elem in root.findall('.//text') if elem.text])
                    if scraped:
                        return scraped
    except Exception as e:
        print(f'[Method 3 Failed] {video_id}: {e}')

    try:
        cmd = [
            'yt-dlp',
            '--skip-download',
            '--write-subs',
            '--write-auto-subs',
            '--sub-format',
            'json3',
            '--dump-json',
            f'https://www.youtube.com/watch?v={video_id}',
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            info = json.loads(res.stdout)
            subtitles = info.get('subtitles') or info.get('automatic_captions') or {}
            if subtitles:
                first_lang = list(subtitles.keys())[0]
                sub_url = subtitles[first_lang][0].get('url')
                if sub_url:
                    sub_req = urllib.request.Request(sub_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(sub_req, timeout=10) as response:
                        sub_content = response.read().decode('utf-8')
                        if 'events' in sub_content:
                            sub_data = json.loads(sub_content)
                            text_parts = [
                                seg.get('utf8', '')
                                for event in sub_data.get('events', [])
                                for seg in event.get('segs', [])
                            ]
                            full_text = ' '.join(text_parts).strip()
                            if full_text:
                                return full_text
    except Exception as e:
        print(f'[Method 4 Failed] {video_id}: {e}')

    return None

@app.after_request
def add_no_cache_headers(response):
    response.headers['Cache-Control'] = (
        'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    )
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    return response

# ==========================================
# 3. DOMAIN HELPER COMPUTATIONS & FORMULAS
# ==========================================
def check_water_criteria_complete(user):
    """Check if required profile fields for water calculation are present."""
    if not user:
        return False
    try:
        weight = user.get('weight')
        if weight is None or weight == '' or float(weight) <= 0:
            return False
        activity = user.get('activity_level')
        climate = user.get('climate') or user.get('local_climate')
        if not activity or not climate:
            return False
        return True
    except (ValueError, TypeError):
        return False

def calculate_water_target(user_id):
    """Calculate exact personalized fluid requirement. Returns 0 target if preferences unconfigured."""
    user = fetch_user_by_id(user_id) or {}
    criteria_complete = check_water_criteria_complete(user)

    if not criteria_complete:
        return 0, 0.0, None, False

    try:
        weight = float(user.get('weight') or 70.0)
    except (ValueError, TypeError):
        weight = 70.0

    weight_unit = str(user.get('weight_unit', 'kg')).lower()
    if weight_unit in ['lbs', 'lb']:
        weight_kg = weight * 0.45359237
    else:
        weight_kg = weight

    base_ml = weight_kg * 35.0

    activity_level = str(user.get('activity_level', 'active')).lower()
    if 'athlete' in activity_level:
        activity_bonus = 1000.0
    elif 'active' in activity_level or 'moderate' in activity_level or 'very' in activity_level:
        activity_bonus = 500.0
    else:
        activity_bonus = 0.0

    climate = str(user.get('climate') or user.get('local_climate') or 'cool').lower()
    if 'hot' in climate or 'dry' in climate or 'humid' in climate:
        climate_bonus = 500.0
    else:
        climate_bonus = 0.0

    total_target_ml = int(round(base_ml + activity_bonus + climate_bonus))
    total_target_ml = max(1000, min(total_target_ml, 8000))
    target_l_num = round(total_target_ml / 1000.0, 2)
    target_l_str = f"{target_l_num:.2f}".rstrip('0').rstrip('.') + " L"

    return total_target_ml, target_l_num, target_l_str, True

def calculate_user_streak(user_id, current_today_date=None, current_today_score=None):
    """
    Calculate active streak strictly from daily goal completion history.
    - Day counts successful ONLY when daily goal completion > 60% (61%-100%).
    - Scores <= 60% reset active daily streak progression to 0.
    """
    if not supabase or not user_id:
        return 0

    try:
        res = supabase.table('daily_progress') \
            .select('record_date, goal_score') \
            .eq('user_id', user_id) \
            .execute()

        raw_data = res.data or []
        scores_by_date = {}

        def safe_score(val):
            try:
                return float(val or 0)
            except (ValueError, TypeError):
                return 0.0

        for r in raw_data:
            if r.get('record_date'):
                scores_by_date[r['record_date']] = safe_score(r.get('goal_score'))

        if current_today_date and current_today_score is not None:
            scores_by_date[current_today_date] = safe_score(current_today_score)

        today = date.today()
        today_str = today.isoformat()
        today_score = scores_by_date.get(today_str, 0)

        STREAK_THRESHOLD = 60.0

        if today_score > STREAK_THRESHOLD:
            streak = 0
            check_d = today
            while True:
                d_str = check_d.isoformat()
                if d_str in scores_by_date and scores_by_date[d_str] > STREAK_THRESHOLD:
                    streak += 1
                    check_d -= timedelta(days=1)
                else:
                    break
            return streak
        else:
            yesterday_str = (today - timedelta(days=1)).isoformat()
            if today_str in scores_by_date and scores_by_date[today_str] <= STREAK_THRESHOLD:
                return 0
            elif yesterday_str in scores_by_date and scores_by_date[yesterday_str] > STREAK_THRESHOLD:
                streak = 0
                check_d = today - timedelta(days=1)
                while True:
                    d_str = check_d.isoformat()
                    if d_str in scores_by_date and scores_by_date[d_str] > STREAK_THRESHOLD:
                        streak += 1
                        check_d -= timedelta(days=1)
                    else:
                        break
                return streak
            else:
                return 0

    except Exception as e:
        print(f"Error calculating streak: {e}")
        return 0

def compute_goal_score_and_aura(user_id, target_date_str=None, update_db=True):
    """
    Centralized Single Source of Truth for Goal Completion %, Aura, XP, Streak, and Rank.
    """
    if not target_date_str:
        target_date_str = date.today().isoformat()

    target_ml, target_l_num, target_l_str, criteria_complete = calculate_water_target(user_id)
    current_ml = 0

    if supabase and user_id:
        try:
            w_res = supabase.table('water_intake').select('amount_ml').eq('user_id', user_id).eq('record_date', target_date_str).execute()
            if w_res.data:
                current_ml = sum(int(item.get('amount_ml', 0) or 0) for item in w_res.data)
        except Exception as e:
            print(f"Error reading water intake: {e}")

    tasks_total = 0
    tasks_completed = 0

    if supabase and user_id:
        try:
            t_res = supabase.table('daily_targets').select('completed').eq('user_id', user_id).eq('target_date', target_date_str).execute()
            if t_res.data:
                tasks_total = len(t_res.data)
                tasks_completed = sum(1 for t in t_res.data if t.get('completed'))
        except Exception as e:
            print(f"Error reading daily targets: {e}")

    if tasks_total == 0 and supabase and user_id:
        try:
            us_res = supabase.table('user_stats').select('total_goals, completed_goals').eq('user_id', user_id).execute()
            if us_res.data and len(us_res.data) > 0:
                tasks_total = us_res.data[0].get('total_goals', 0) or 0
                tasks_completed = us_res.data[0].get('completed_goals', 0) or 0
        except Exception as e:
            print(f"Error reading user_stats: {e}")

    if criteria_complete and target_ml > 0:
        water_score = min(1.0, current_ml / target_ml)
        if tasks_total > 0:
            tasks_score = tasks_completed / tasks_total
            overall_score = round(((water_score * 0.4) + (tasks_score * 0.6)) * 100)
        else:
            overall_score = round(water_score * 100)
    else:
        if tasks_total > 0:
            overall_score = round((tasks_completed / tasks_total) * 100)
        else:
            overall_score = 0

    overall_score = max(0, min(100, int(overall_score)))
    streak = calculate_user_streak(user_id, current_today_date=target_date_str, current_today_score=overall_score)

    # 10 XP per 1% goal completion score (84% score = 840 XP)
    calculated_xp = int(overall_score * 10)

    user_xp = calculated_xp
    user_streak = streak
    user_rank = get_scholar_rank(user_xp)

    if overall_score < 50:
        aura_title = "-999 Aura 😤"
        aura_category = "Negative"
        aura_tag = "Negative"
    elif overall_score <= 79:
        aura_title = "Chad 🗿"
        aura_category = "Chad"
        aura_tag = "Chad"
    else:
        aura_title = "+999 Aura 🗿🔥"
        aura_category = "Master"
        aura_tag = "Master"

    if supabase and user_id and update_db:
        try:
            supabase.table('daily_progress').upsert({
                'user_id': user_id,
                'record_date': target_date_str,
                'goal_score': overall_score,
                'water_completed_ml': current_ml,
                'water_target_ml': target_ml,
                'tasks_completed': tasks_completed,
                'tasks_total': tasks_total,
                'xp_awarded': user_xp,
                'updated_at': datetime.utcnow().isoformat()
            }, on_conflict='user_id,record_date').execute()

            supabase.table('user_stats').upsert({
                'user_id': user_id,
                'xp': user_xp,
                'aura_level': (user_xp // 100) + 1,
                'completed_goals': tasks_completed,
                'total_goals': tasks_total,
                'water_intake': current_ml,
                'updated_at': datetime.utcnow().isoformat()
            }, on_conflict='user_id').execute()

            supabase.table('users').update({
                'xp': user_xp,
                'streak': user_streak,
                'scholar_rank': user_rank
            }).eq('id', user_id).execute()

        except Exception as e:
            print(f"Error persisting goal score and aura: {e}")

    water_progress_pct = round((current_ml / target_ml) * 100) if (criteria_complete and target_ml > 0) else 0

    return {
        'score': overall_score,
        'goal_score': overall_score,
        'goal_completion_pct': overall_score,
        'completion_pct': overall_score,
        'completion_rate': overall_score,
        'aura': aura_title,
        'aura_title': aura_title,
        'aura_category': aura_category,
        'aura_tag': aura_tag,
        'water_ml': current_ml,
        'water_consumed_ml': current_ml,
        'water_target_ml': target_ml,
        'water_target_l': target_l_num,
        'personalized_water_target': target_l_str,
        'water_target': target_l_str,
        'water_progress_pct': water_progress_pct,
        'water_completion_pct': water_progress_pct,
        'criteria_complete': criteria_complete,
        'xp': user_xp,
        'scholar_xp': user_xp,
        'streak': user_streak,
        'active_streak': user_streak,
        'day_streak': user_streak,
        'scholar_rank': user_rank,
        'rank': user_rank,
        'aura_level': (user_xp // 100) + 1,
        'total_goals': tasks_total,
        'completed_goals': tasks_completed
    }

# ==========================================
# 4. AUTH & ROUTING (SUPABASE INTEGRATED)
# ==========================================
@app.route('/favicon.ico')
@app.route('/favicon.png')
def favicon():
    return send_from_directory(app.static_folder, 'favicon.png', mimetype='image/png')

@app.route('/new-feature')
def new_feature_page():
    if not session.get('user_id'):
        return redirect(url_for('login_page'))
    return render_template('new_feature.html')
@app.route('/creator')
@app.route('/creators')
def creator():
    return render_template('creator.html')
@app.route('/', endpoint='index')
@app.route('/dashboard', endpoint='dashboard')
def dashboard():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login_page'))

    today_str = date.today().isoformat()
    metrics = compute_goal_score_and_aura(user_id, today_str, update_db=True)
    user = fetch_user_by_id(user_id)

    total_goals = metrics.get('total_goals', 0)
    completed_goals = metrics.get('completed_goals', 0)
    water_intake = metrics.get('water_ml', 0)
    calculated_xp = metrics.get('xp', 0)
    aura_level = metrics.get('aura_level', (calculated_xp // 100) + 1)
    scholar_rank = metrics.get('scholar_rank', get_scholar_rank(calculated_xp))

    if user:
        user.update({
            'score': metrics['score'],
            'goal_score': metrics['score'],
            'goal_completion_pct': metrics['score'],
            'completion_pct': metrics['score'],
            'completion_rate': metrics['score'],
            'aura': metrics['aura'],
            'aura_category': metrics['aura_category'],
            'aura_tag': metrics['aura_tag'],
            'water_target_ml': metrics['water_target_ml'],
            'water_target_l': metrics['water_target_l'],
            'personalized_water_target': metrics['personalized_water_target'],
            'criteria_complete': metrics['criteria_complete'],
            'scholar_xp': calculated_xp,
            'xp': calculated_xp,
            'scholar_rank': scholar_rank,
            'rank': scholar_rank,
            'aura_level': aura_level,
            'completed_goals': completed_goals,
            'total_goals': total_goals,
            'active_streak': metrics['streak'],
            'water_intake': f"{water_intake} ml"
        })

    leaderboard, _ = fetch_leaderboard(current_user_id=user_id)

    return render_template(
        'index.html',
        user=user,
        leaderboard=leaderboard,
        xp=calculated_xp,
        scholar_xp=calculated_xp,
        scholar_rank=scholar_rank,
        rank=scholar_rank,
        aura_level=aura_level,
        completed_goals=completed_goals,
        total_goals=total_goals,
        water_intake=water_intake
    )

@app.route('/signup', methods=['GET', 'POST'])
def signup_page():
    if session.get('user_id'):
        return redirect(url_for('index'))

    if request.method == 'POST':
        name = (request.form.get('name', '').strip() or request.form.get('username', '').strip())
        username = request.form.get('username', '').strip().lower()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not username or not email or not password:
            return render_template('signup.html', error='All fields are required.')

        try:
            existing_user = supabase.table('users').select('id').eq('username', username).execute()
            if existing_user.data:
                return render_template('signup.html', error='Username is already taken.')

            auth_res = supabase.auth.sign_up({
                "email": email,
                "password": password,
                "options": {
                    "data": {"username": username, "name": name}
                }
            })

            if not auth_res.user:
                return render_template('signup.html', error='Registration failed.')

            user_id = auth_res.user.id

            profile_data = {
                "id": user_id,
                "name": name,
                "username": username,
                "email": email,
                "bio": "",
                "state": "Maharashtra",
                "weight": None,
                "weight_unit": "kg",
                "activity_level": None,
                "climate": None,
                "avatar_url": "",
                "xp": 0,
                "streak": 0,
                "scholar_rank": "Beginner"
            }
            supabase.table('users').upsert(profile_data).execute()

            supabase.table('user_stats').upsert({
                "user_id": user_id,
                "xp": 0,
                "aura_level": 1,
                "total_goals": 0,
                "completed_goals": 0,
                "water_intake": 0
            }, on_conflict='user_id').execute()

            session['user_id'] = user_id
            session['username'] = username
            
            compute_goal_score_and_aura(user_id, date.today().isoformat(), update_db=True)
            return redirect(url_for('index'))

        except Exception as e:
            return render_template('signup.html', error=str(e))

    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if session.get('user_id'):
        return redirect(url_for('index'))

    error = None
    if request.method == 'POST':
        identifier = request.form.get('username', '').strip().lower() or request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not identifier or not password:
            return render_template('login.html', error='Please enter both username/email and password.')

        try:
            target_email = identifier

            if '@' not in identifier:
                user_res = supabase.table('users').select('email').eq('username', identifier).execute()
                if user_res.data and len(user_res.data) > 0:
                    target_email = user_res.data[0].get('email')
                else:
                    return render_template('login.html', error='No account found with that username.')

            try:
                supabase.auth.sign_out()
            except Exception:
                pass

            auth_res = supabase.auth.sign_in_with_password({
                "email": target_email,
                "password": password
            })

            if auth_res and auth_res.user:
                session.clear()
                session['user_id'] = auth_res.user.id
                
                meta = auth_res.user.user_metadata or {}
                username = meta.get('username')

                if not username:
                    user_profile = fetch_user_by_id(auth_res.user.id)
                    username = user_profile.get('username') if user_profile else identifier

                session['username'] = username
                
                compute_goal_score_and_aura(auth_res.user.id, date.today().isoformat(), update_db=True)
                return redirect(url_for('index'))
            else:
                error = 'Invalid email/username or password.'

        except Exception as e:
            err_msg = str(e)
            if 'invalid' in err_msg.lower() or 'credentials' in err_msg.lower():
                error = 'Invalid email/username or password.'
            elif 'email not confirmed' in err_msg.lower():
                error = 'Please confirm your email address before logging in.'
            else:
                error = f'Login failed: {err_msg}'

    return render_template('login.html', error=error)

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password_page():
    error = None
    success = None
    if request.method == 'POST':
        identifier = request.form.get('email', '').strip().lower() or request.form.get('username', '').strip().lower()

        if not identifier:
            error = 'Please enter your username or email address.'
        else:
            try:
                email = identifier
                if '@' not in identifier:
                    user_res = supabase.table('users').select('email').eq('username', identifier).execute()
                    if user_res.data and len(user_res.data) > 0:
                        email = user_res.data[0]['email']
                    else:
                        error = 'No account found with this username.'

                if not error:
                    redirect_url = url_for('reset_password_page', _external=True)
                    supabase.auth.reset_password_for_email(
                        email,
                        redirect_to=redirect_url
                    )
                    success = "Password reset link has been sent to your email address!"
            except Exception as e:
                error = "Unable to process password reset request. Please try again."

    return render_auth_template(
        'forgetpass.html',
        'forgot_password.html',
        error=error,
        success=success,
        show_reset_fields=False
    )

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password_page():
    error = None
    success = None
    show_reset_fields = True

    code = request.args.get('code')
    token_hash = request.args.get('token_hash')
    token_type = request.args.get('type', 'recovery')
    access_token = request.args.get('access_token') or request.form.get('access_token')
    refresh_token = request.args.get('refresh_token') or request.form.get('refresh_token')

    try:
        if code:
            supabase.auth.exchange_code_for_session({"auth_code": code})
        elif token_hash and token_type == 'recovery':
            supabase.auth.verify_otp({
                "token_hash": token_hash,
                "type": "recovery"
            })
        elif access_token and refresh_token:
            supabase.auth.set_session(access_token, refresh_token)
    except Exception as e:
        pass

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        post_access_token = request.form.get('access_token')
        post_refresh_token = request.form.get('refresh_token')
        
        if post_access_token and post_refresh_token:
            try:
                supabase.auth.set_session(post_access_token, post_refresh_token)
            except Exception as e:
                pass

        if not password or password != confirm_password:
            error = 'Passwords do not match.'
        elif len(password) < 6:
            error = 'Password must be at least 6 characters long.'
        else:
            try:
                res = supabase.auth.update_user({"password": password})
                if res.user:
                    return redirect(url_for('login_page'))
                else:
                    error = 'Password update failed. Please request a new reset link.'
            except Exception as e:
                error = "Session expired or invalid link. Please request a new password reset link."

    return render_auth_template(
        'forgetpass.html',
        'forgot_password.html',
        error=error,
        success=success,
        show_reset_fields=show_reset_fields,
        access_token=access_token or '',
        refresh_token=refresh_token or ''
    )

@app.route('/profile')
def profile_page():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login_page'))

    today_str = date.today().isoformat()
    metrics = compute_goal_score_and_aura(user_id, today_str, update_db=True)

    user = fetch_user_by_id(user_id)
    if not user:
        session.clear()
        return redirect(url_for('login_page'))

    xp_val = metrics.get('xp', user.get('xp', 0))
    scholar_rank = metrics.get('scholar_rank', get_scholar_rank(xp_val))
    aura_level = metrics.get('aura_level', (xp_val // 100) + 1)

    user.update({
        'water_target_ml': metrics['water_target_ml'],
        'water_target_l': metrics['water_target_l'],
        'personalized_water_target': metrics['personalized_water_target'],
        'water_target': metrics['personalized_water_target'],
        'target_water': metrics['personalized_water_target'],
        'water_intake': f"{metrics['water_ml']} ml",
        'criteria_complete': metrics['criteria_complete'],
        'score': metrics['score'],
        'goal_score': metrics['score'],
        'goal_completion_pct': metrics['score'],
        'completion_pct': metrics['score'],
        'completion_rate': metrics['score'],
        'aura': metrics['aura'],
        'aura_category': metrics['aura_category'],
        'aura_tag': metrics['aura_tag'],
        'scholar_xp': xp_val,
        'xp': xp_val,
        'scholar_rank': scholar_rank,
        'rank': scholar_rank,
        'aura_level': aura_level,
        'completed_goals': metrics.get('completed_goals', user.get('completed_goals', 0)),
        'total_goals': metrics.get('total_goals', user.get('total_goals', 0)),
        'active_streak': metrics['streak'],
        'streak': metrics['streak']
    })

    return render_template('profile.html', user=user)

@app.route('/fitness')
def fitness_page():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login_page'))

    today_str = date.today().isoformat()
    metrics = compute_goal_score_and_aura(user_id, today_str, update_db=True)

    user = fetch_user_by_id(user_id)
    if not user:
        session.clear()
        return redirect(url_for('login_page'))

    xp_val = metrics.get('xp', user.get('xp', 0))
    scholar_rank = metrics.get('scholar_rank', get_scholar_rank(xp_val))
    aura_level = metrics.get('aura_level', (xp_val // 100) + 1)

    user.update({
        'water_target_ml': metrics['water_target_ml'],
        'water_target_l': metrics['water_target_l'],
        'personalized_water_target': metrics['personalized_water_target'],
        'water_target': metrics['personalized_water_target'],
        'target_water': metrics['personalized_water_target'],
        'criteria_complete': metrics['criteria_complete'],
        'score': metrics['score'],
        'goal_score': metrics['score'],
        'goal_completion_pct': metrics['score'],
        'completion_pct': metrics['score'],
        'completion_rate': metrics['score'],
        'aura': metrics['aura'],
        'aura_category': metrics['aura_category'],
        'aura_tag': metrics['aura_tag'],
        'water_consumed_ml': metrics['water_ml'],
        'water_intake': f"{metrics['water_ml']} ml",
        'water_progress_pct': metrics['water_progress_pct'],
        'scholar_xp': xp_val,
        'xp': xp_val,
        'scholar_rank': scholar_rank,
        'rank': scholar_rank,
        'aura_level': aura_level,
        'completed_goals': metrics.get('completed_goals', user.get('completed_goals', 0)),
        'total_goals': metrics.get('total_goals', user.get('total_goals', 0))
    })

    return render_template('fitness.html', user=user)

@app.route('/study')
def study_page():
    if not session.get('user_id'):
        return redirect(url_for('login_page'))
    return render_template('study.html')

@app.route('/opportunities')
def opportunities_page():
    if not session.get('user_id'):
        return redirect(url_for('login_page'))
    return render_template('opportunities.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

# ==========================================
# 5. CORE BUSINESS LOGIC API ENDPOINTS
# ==========================================

@app.route('/api/profile', methods=['GET'])
@app.route('/api/profile/stats', methods=['GET'])
def get_profile_stats():
    """Return real user profile statistics and completion metrics."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'status': 'error', 'message': 'Unauthorized'}), 401

    today_str = date.today().isoformat()
    metrics = compute_goal_score_and_aura(user_id, today_str, update_db=True)
    user = fetch_user_by_id(user_id) or {}
    user.update({
        'completion_rate': metrics['score'],
        'goals_completion_pct': metrics['score'],
        'scholar_xp': metrics['xp'],
        'xp': metrics['xp'],
        'scholar_rank': metrics['scholar_rank'],
        'rank': metrics['scholar_rank'],
        'aura_level': metrics['aura_level'],
        'active_streak': metrics['streak'],
        'aura_tag': metrics['aura_tag'],
        'completed_goals': metrics['completed_goals'],
        'total_goals': metrics['total_goals'],
        'water_intake': f"{metrics['water_ml']} ml"
    })

    return jsonify({
        'success': True,
        'status': 'success',
        'user': user,
        'profile': user,
        'xp': metrics['xp'],
        'scholar_xp': metrics['scholar_xp'],
        'scholar_rank': metrics['scholar_rank'],
        'rank': metrics['scholar_rank'],
        'aura_level': metrics['aura_level'],
        'streak': metrics['streak'],
        'active_streak': metrics['streak'],
        'day_streak': metrics['streak'],
        'goal_score': metrics['score'],
        'goal_completion_pct': metrics['score'],
        'completion_pct': metrics['score'],
        'completion_rate': metrics['score'],
        'goals_completion_pct': metrics['score'],
        'completed_goals': metrics['completed_goals'],
        'total_goals': metrics['total_goals'],
        'water_target': metrics['personalized_water_target'],
        'target_water': metrics['personalized_water_target'],
        'water_target_ml': metrics['water_target_ml'],
        'water_target_l': metrics['water_target_l'],
        'personalized_water_target': metrics['personalized_water_target'],
        'water_consumed_ml': metrics['water_ml'],
        'water_intake': f"{metrics['water_ml']} ml",
        'water_progress_pct': metrics['water_progress_pct'],
        'state': user.get('state', 'Maharashtra'),
        'aura': metrics['aura'],
        'aura_category': metrics['aura_category'],
        'aura_tag': metrics['aura_tag'],
        'criteria_complete': metrics['criteria_complete']
    })

@app.route('/api/profile/update', methods=['POST'])
@app.route('/profile/update', methods=['POST'])
def api_update_profile():
    """Update profile data in Supabase including State, Weight, Unit, Activity Level, Climate."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'error', 'success': False, 'message': 'Unauthorized.'}), 401

    try:
        data = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})

        name = data.get('name', '').strip()
        username = data.get('username', '').strip().lower()
        state = data.get('state', '').strip()
        bio = data.get('bio', '').strip()
        weight = data.get('weight')
        weight_unit = data.get('weight_unit', 'kg').strip()
        activity_level = data.get('activity_level', '').strip()
        climate = data.get('climate', '').strip() or data.get('local_climate', '').strip()

        updates = {}
        if username:
            check = supabase.table('users').select('id').eq('username', username).neq('id', user_id).execute()
            if check.data:
                return jsonify({'status': 'error', 'success': False, 'message': 'Username taken.'}), 400
            updates['username'] = username
            session['username'] = username

        if name:
            updates['name'] = name
        if state:
            updates['state'] = state
        if bio is not None:
            updates['bio'] = bio
        if weight is not None and weight != '':
            updates['weight'] = float(weight)
        if weight_unit:
            updates['weight_unit'] = weight_unit
        if activity_level:
            updates['activity_level'] = activity_level
        if climate:
            updates['climate'] = climate

        if 'avatar' in request.files:
            file = request.files['avatar']
            if file and allowed_file(file.filename):
                file_ext = file.filename.rsplit('.', 1)[1].lower()
                storage_path = f"user_file_{user_id}_{int(datetime.utcnow().timestamp())}.{file_ext}"
                file_bytes = file.read()
                content_type = file.content_type or 'application/octet-stream'

                supabase.storage.from_(STORAGE_BUCKET).upload(
                    path=storage_path,
                    file=file_bytes,
                    file_options={"content-type": content_type, "upsert": "true"}
                )

                public_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(storage_path)
                updates['avatar_url'] = public_url

        if updates:
            supabase.table('users').update(updates).eq('id', user_id).execute()

        updated_user = fetch_user_by_id(user_id)
        metrics = compute_goal_score_and_aura(user_id, update_db=True)

        return jsonify({
            'status': 'success',
            'success': True,
            'message': 'Profile updated successfully!',
            'user': updated_user,
            **metrics
        })

    except Exception as e:
        return jsonify({'status': 'error', 'success': False, 'message': str(e)}), 500

@app.route('/api/water', methods=['GET', 'POST'])
def handle_water():
    """Daily Water Intake logging and progress retrieval."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    today_str = request.args.get('date') or date.today().isoformat()
    user = fetch_user_by_id(user_id)

    if not check_water_criteria_complete(user):
        return jsonify({
            'success': False,
            'criteria_missing': True,
            'message': 'Please complete your Water Intake Criteria in your Profile first.'
        }), 400

    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        amount_ml = int(data.get('amount_ml', 0) or data.get('amount', 0))

        if amount_ml > 0:
            try:
                recent_cutoff = (datetime.utcnow() - timedelta(seconds=5)).isoformat()
                dup_check = supabase.table('water_intake') \
                    .select('id, created_at') \
                    .eq('user_id', user_id) \
                    .eq('record_date', today_str) \
                    .eq('amount_ml', amount_ml) \
                    .gte('created_at', recent_cutoff) \
                    .execute()

                if not dup_check.data:
                    supabase.table('water_intake').insert({
                        'user_id': user_id,
                        'amount_ml': amount_ml,
                        'record_date': today_str,
                        'created_at': datetime.utcnow().isoformat()
                    }).execute()
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

    metrics = compute_goal_score_and_aura(user_id, today_str, update_db=True)
    return jsonify({
        'success': True,
        'status': 'success',
        'date': today_str,
        'amount_ml': metrics['water_ml'],
        **metrics
    })

@app.route('/api/targets', methods=['GET', 'POST', 'PUT', 'DELETE'])
def handle_targets():
    """Daily Targets/Tasks management mapped strictly to user and date."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    today_str = request.args.get('date') or date.today().isoformat()

    if request.method == 'GET':
        try:
            res = supabase.table('daily_targets').select('*').eq('user_id', user_id).eq('target_date', today_str).execute()
            tasks = res.data or []
            metrics = compute_goal_score_and_aura(user_id, today_str, update_db=True)
            return jsonify({
                'success': True,
                'date': today_str,
                'tasks': tasks,
                'targets': tasks,
                **metrics
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    elif request.method == 'POST':
        data = request.get_json(force=True) or {}
        title = data.get('title', '').strip()
        if not title:
            return jsonify({'success': False, 'error': 'Task title required'}), 400

        try:
            existing = supabase.table('daily_targets').select('*').eq('user_id', user_id).eq('target_date', today_str).eq('title', title).execute()
            if existing.data:
                task_obj = existing.data[0]
            else:
                res = supabase.table('daily_targets').insert({
                    'user_id': user_id,
                    'title': title,
                    'completed': False,
                    'target_date': today_str,
                    'created_at': datetime.utcnow().isoformat()
                }).execute()
                task_obj = res.data[0] if res.data else {}

            metrics = compute_goal_score_and_aura(user_id, today_str, update_db=True)
            return jsonify({
                'success': True,
                'task': task_obj,
                'target': task_obj,
                **metrics
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    elif request.method == 'PUT':
        data = request.get_json(force=True) or {}
        task_id = data.get('task_id') or data.get('id')
        completed = bool(data.get('completed', False))

        if not task_id:
            return jsonify({'success': False, 'error': 'Task ID required'}), 400

        try:
            target_date_for_task = today_str
            t_info = supabase.table('daily_targets').select('target_date').eq('id', task_id).eq('user_id', user_id).execute()
            if t_info.data and t_info.data[0].get('target_date'):
                target_date_for_task = t_info.data[0]['target_date']

            supabase.table('daily_targets').update({'completed': completed}).eq('id', task_id).eq('user_id', user_id).execute()
            metrics = compute_goal_score_and_aura(user_id, target_date_for_task, update_db=True)
            return jsonify({
                'success': True,
                **metrics
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    elif request.method == 'DELETE':
        task_id = request.args.get('task_id') or request.args.get('id')
        if not task_id and request.is_json:
            data = request.get_json(silent=True) or {}
            task_id = data.get('task_id') or data.get('id')

        if not task_id:
            return jsonify({'success': False, 'error': 'Task ID required'}), 400

        try:
            target_date_for_task = today_str
            t_info = supabase.table('daily_targets').select('target_date').eq('id', task_id).eq('user_id', user_id).execute()
            if t_info.data and t_info.data[0].get('target_date'):
                target_date_for_task = t_info.data[0]['target_date']

            supabase.table('daily_targets').delete().eq('id', task_id).eq('user_id', user_id).execute()
            metrics = compute_goal_score_and_aura(user_id, target_date_for_task, update_db=True)
            return jsonify({
                'success': True,
                **metrics
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/score', methods=['GET'])
def get_user_score():
    """Retrieve current Goal Completion Score, Aura, XP, Streak, and Rank."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    today_str = request.args.get('date') or date.today().isoformat()
    metrics = compute_goal_score_and_aura(user_id, today_str, update_db=True)
    return jsonify({'success': True, **metrics})

@app.route('/api/user/sync-home', methods=['POST'])
def sync_home():
    """Sync daily progress state and return updated metrics for frontend dashboard."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    target_date = data.get('date') or date.today().isoformat()

    metrics = compute_goal_score_and_aura(user_id, target_date, update_db=True)
    return jsonify({'success': True, **metrics})

@app.route('/api/leaderboard', methods=['GET'])
def get_leaderboard():
    """Global Aura Leaderboard backend utilizing dynamic real user data and filters."""
    user_id = session.get('user_id')
    state_filter = request.args.get('state', '').strip()
    tag_filter = (request.args.get('tag') or request.args.get('aura_tag') or request.args.get('filter') or '').strip()

    leaderboard, my_position = fetch_leaderboard(
        current_user_id=user_id,
        state_filter=state_filter,
        tag_filter=tag_filter
    )

    return jsonify({
        'success': True,
        'leaderboard': leaderboard,
        'my_position': my_position,
        'total_users': len(leaderboard)
    })

# ==========================================
# PRIVATE SQUAD MANAGEMENT APIS
# ==========================================

@app.route('/api/squad/my-squad', methods=['GET'])
@app.route('/api/squad', methods=['GET'])
def get_my_squad():
    """Retrieve active squad details and member list with complete real-time metrics."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    if not supabase:
        return jsonify({'success': True, 'has_squad': False, 'squad': None})

    try:
        member_res = supabase.table('squad_members').select('squad_id').eq('user_id', user_id).execute()
        if not member_res.data:
            return jsonify({'success': True, 'has_squad': False, 'squad': None})

        squad_id = member_res.data[0]['squad_id']
        squad_res = supabase.table('squads').select('*').eq('id', squad_id).execute()
        if not squad_res.data:
            return jsonify({'success': True, 'has_squad': False, 'squad': None})

        squad = squad_res.data[0]

        members_res = supabase.table('squad_members').select('user_id').eq('squad_id', squad_id).execute()
        member_ids = [m['user_id'] for m in (members_res.data or [])]

        today_str = date.today().isoformat()
        members_data = []

        for mid in member_ids:
            u_info = fetch_user_by_id(mid)
            if u_info:
                metrics = compute_goal_score_and_aura(mid, today_str, update_db=False)
                members_data.append({
                    'id': mid,
                    'name': u_info.get('name') or u_info.get('username') or 'Squad Member',
                    'username': u_info.get('username') or '',
                    'avatar_url': u_info.get('avatar_url') or '',
                    'avatar': u_info.get('avatar_url') or '',
                    'xp': metrics['xp'],
                    'scholar_xp': metrics['xp'],
                    'streak': metrics['streak'],
                    'active_streak': metrics['streak'],
                    'score': metrics['score'],
                    'goal_score': metrics['score'],
                    'completion_rate': metrics['score'],
                    'goals_completion_pct': metrics['score'],
                    'completion_pct': metrics['score'],
                    'goal_completion_pct': metrics['score'],
                    'aura': metrics['aura'],
                    'aura_title': metrics['aura_title'],
                    'aura_category': metrics['aura_category'],
                    'aura_tag': metrics['aura_tag'],
                    'scholar_rank': metrics['scholar_rank'],
                    'is_current_user': (mid == user_id)
                })

        members_data.sort(key=lambda x: (x['xp'], x['score']), reverse=True)
        for idx, m in enumerate(members_data, start=1):
            m['squad_rank'] = idx

        squad['members'] = members_data
        squad['member_count'] = len(members_data)

        return jsonify({'success': True, 'has_squad': True, 'squad': squad})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/squad/create', methods=['POST'])
def create_squad():
    """Create a new real private squad and return full squad details immediately."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    squad_name = data.get('name', '').strip() or f"{session.get('username', 'Scholar')}'s Squad"

    try:
        existing = supabase.table('squad_members').select('id').eq('user_id', user_id).execute()
        if existing.data:
            return jsonify({'success': False, 'error': 'You are already in a squad. Leave your current squad first.'}), 400

        code = f"SP-{secrets.token_hex(2).upper()}"

        squad_res = supabase.table('squads').insert({
            'name': squad_name,
            'code': code,
            'created_by': user_id,
            'created_at': datetime.utcnow().isoformat()
        }).execute()

        if not squad_res.data:
            return jsonify({'success': False, 'error': 'Failed to create squad.'}), 500

        squad_id = squad_res.data[0]['id']

        try:
            supabase.table('squad_members').insert({
                'squad_id': squad_id,
                'user_id': user_id,
                'joined_at': datetime.utcnow().isoformat()
            }).execute()
        except Exception:
            supabase.table('squads').delete().eq('id', squad_id).execute()
            return jsonify({'success': False, 'error': 'You are already in a squad. Leave your current squad first.'}), 400

        return get_my_squad()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/squad/join', methods=['POST'])
def join_squad():
    """Join an existing private squad using squad code and return updated members payload immediately."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    code = data.get('code', '').strip().upper()

    if not code:
        return jsonify({'success': False, 'error': 'Squad code is required.'}), 400

    try:
        squad_res = supabase.table('squads').select('*').eq('code', code).execute()
        if not squad_res.data:
            return jsonify({'success': False, 'error': 'Invalid squad code. Please check and try again.'}), 404

        squad = squad_res.data[0]
        squad_id = squad['id']

        existing = supabase.table('squad_members').select('squad_id').eq('user_id', user_id).execute()
        if existing.data:
            if existing.data[0]['squad_id'] == squad_id:
                return get_my_squad()
            return jsonify({'success': False, 'error': 'You are already in another squad. Leave it first before joining a new one.'}), 400

        members_res = supabase.table('squad_members').select('id').eq('squad_id', squad_id).execute()
        if len(members_res.data or []) >= 7:
            return jsonify({'success': False, 'error': 'Squad is full!'}), 400

        try:
            supabase.table('squad_members').insert({
                'squad_id': squad_id,
                'user_id': user_id,
                'joined_at': datetime.utcnow().isoformat()
            }).execute()
        except Exception:
            return jsonify({'success': False, 'error': 'Error joining squad or already a member.'}), 400

        recheck = supabase.table('squad_members').select('id').eq('squad_id', squad_id).execute()
        if len(recheck.data or []) > 7:
            supabase.table('squad_members').delete().eq('squad_id', squad_id).eq('user_id', user_id).execute()
            return jsonify({'success': False, 'error': 'Squad is full!'}), 400

        return get_my_squad()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/squad/leave', methods=['POST', 'DELETE'])
def leave_squad():
    """Leave current active squad room."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        supabase.table('squad_members').delete().eq('user_id', user_id).execute()
        return jsonify({'success': True, 'has_squad': False, 'message': 'Successfully left squad.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/squad/leaderboard', methods=['GET'])
def get_squad_leaderboard():
    """Private Squad Leaderboard ranking active squad members."""
    return get_my_squad()

# ==========================================
# 6. FITNESS & AI DIET ENDPOINTS (GPT-120B)
# ==========================================

@app.route('/api/fitness/chat', methods=['POST'])
@app.route('/api/fitness-ai', methods=['POST'])
def fitness_ai_chat():
    """AI Fitness Coach using GPT-120B."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized.'}), 401

    data = request.get_json(silent=True) or {}
    message = (data.get('message') or data.get('question') or data.get('prompt') or '').strip()

    if not message:
        return jsonify({'success': False, 'error': 'Message cannot be empty.'}), 400

    system_prompt = (
        "You are an expert AI Fitness Coach & Nutrition Specialist. "
        "Provide direct, actionable, evidence-based advice for fitness, workouts, and nutrition. "
        "Use concise paragraphs, bold text for key terms, and bullet points. "
        "Do not use markdown tables."
    )

    reply = ask_groq_ai(system_prompt, message)

    return jsonify({
        'success': True,
        'reply': reply,
        'response': reply
    })

@app.route('/api/fitness/diet/analyze', methods=['POST'])
def fitness_diet_analyze():
    """AI Diet & Nutrition analysis using GPT-120B with JSON output."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized.'}), 401

    data = request.get_json(silent=True) or {}
    food_input = (data.get('food_input') or data.get('food') or '').strip()
    target_date = data.get('date') or date.today().isoformat()

    if not food_input:
        return jsonify({'success': False, 'error': 'Food description is required.'}), 400

    system_prompt = (
        "You are a precise clinical nutritionist and diet analyst. "
        "Analyze the user's food input and return strictly valid JSON (no extra text, no markdown wrapping) "
        "with the following structure:\n"
        "{\n"
        '  "calories": 450,\n'
        '  "protein_g": 25.5,\n'
        '  "carbs_g": 50.0,\n'
        '  "fat_g": 12.0,\n'
        '  "fiber_g": 6.0,\n'
        '  "sugar_g": 8.0,\n'
        '  "sodium_mg": 350,\n'
        '  "vitamins": ["Vitamin A", "Vitamin C"],\n'
        '  "minerals": ["Calcium", "Iron", "Potassium", "Magnesium"],\n'
        '  "calcium_mg": 120,\n'
        '  "iron_mg": 3.5,\n'
        '  "potassium_mg": 450,\n'
        '  "magnesium_mg": 60,\n'
        '  "energy_score": 85,\n'
        '  "overall_quality": "High Nutrient Density",\n'
        '  "deficiencies": ["Low Vitamin D"],\n'
        '  "recommendations": ["Add a leafy green salad or fruit to boost micronutrients."],\n'
        '  "general_analysis": "A well-balanced meal rich in complex carbs and lean protein."\n'
        "}"
    )

    raw_response = ask_groq_ai(system_prompt, food_input)

    clean_json = raw_response.strip()
    if clean_json.startswith('```'):
        clean_json = re.sub(r'^```(?:json)?\n?', '', clean_json)
        clean_json = re.sub(r'\n?```$', '', clean_json)

    try:
        nutrition_data = json.loads(clean_json)
    except Exception:
        nutrition_data = {
            "calories": 0,
            "protein_g": 0,
            "carbs_g": 0,
            "fat_g": 0,
            "fiber_g": 0,
            "sugar_g": 0,
            "sodium_mg": 0,
            "vitamins": [],
            "minerals": [],
            "energy_score": 50,
            "overall_quality": "Estimated",
            "deficiencies": [],
            "recommendations": ["Provide detailed portion sizes for more accurate tracking."],
            "general_analysis": raw_response
        }

    diet_record = None
    if supabase:
        try:
            existing = supabase.table('diet_records').select('id').eq('user_id', user_id).eq('record_date', target_date).execute()
            record_payload = {
                'user_id': user_id,
                'record_date': target_date,
                'food_input': food_input,
                'ai_analysis': json.dumps(nutrition_data),
                'calories': nutrition_data.get('calories', 0),
                'protein': nutrition_data.get('protein_g', 0),
                'carbohydrates': nutrition_data.get('carbs_g', 0),
                'fat': nutrition_data.get('fat_g', 0),
                'fiber': nutrition_data.get('fiber_g', 0),
                'sugar': nutrition_data.get('sugar_g', 0),
                'sodium': nutrition_data.get('sodium_mg', 0),
                'energy_score': nutrition_data.get('energy_score', 50),
                'updated_at': datetime.utcnow().isoformat()
            }

            if existing.data:
                res = supabase.table('diet_records').update(record_payload).eq('id', existing.data[0]['id']).execute()
            else:
                record_payload['created_at'] = datetime.utcnow().isoformat()
                res = supabase.table('diet_records').insert(record_payload).execute()

            if res.data:
                diet_record = res.data[0]
        except Exception as err:
            print(f"Note: Could not persist diet record to Supabase: {err}")

    return jsonify({
        'success': True,
        'food_input': food_input,
        'date': target_date,
        'nutrition': nutrition_data,
        'analysis': nutrition_data,
        'diet_record': diet_record
    })

@app.route('/api/fitness/diet/records', methods=['GET', 'POST'])
def handle_fitness_diet_records():
    """Retrieve or save/upsert diet records for authenticated user."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized.'}), 401

    target_date = request.args.get('date') or date.today().isoformat()

    if request.method == 'GET':
        records = []
        if supabase:
            try:
                res = supabase.table('diet_records').select('*').eq('user_id', user_id).eq('record_date', target_date).order('created_at', desc=True).execute()
                records = res.data or []
            except Exception as e:
                print(f"Error fetching diet records: {e}")

        return jsonify({
            'success': True,
            'date': target_date,
            'records': records
        })

    elif request.method == 'POST':
        data = request.get_json(silent=True) or {}
        target_date = data.get('date') or target_date
        food_input = data.get('food_input', '').strip()
        analysis = data.get('analysis') or data.get('ai_analysis') or {}

        if not food_input:
            return jsonify({'success': False, 'error': 'Food input required.'}), 400

        if isinstance(analysis, str):
            try:
                analysis_dict = json.loads(analysis)
            except Exception:
                analysis_dict = {'general_analysis': analysis}
        elif isinstance(analysis, dict):
            analysis_dict = analysis
        else:
            analysis_dict = {}

        record = None
        if supabase:
            try:
                existing = supabase.table('diet_records').select('id').eq('user_id', user_id).eq('record_date', target_date).execute()

                record_payload = {
                    'user_id': user_id,
                    'record_date': target_date,
                    'food_input': food_input,
                    'ai_analysis': json.dumps(analysis_dict),
                    'calories': analysis_dict.get('calories', 0),
                    'protein': analysis_dict.get('protein_g', 0) or analysis_dict.get('protein', 0),
                    'carbohydrates': analysis_dict.get('carbs_g', 0) or analysis_dict.get('carbohydrates', 0) or analysis_dict.get('carbs', 0),
                    'fat': analysis_dict.get('fat_g', 0) or analysis_dict.get('fat', 0),
                    'fiber': analysis_dict.get('fiber_g', 0) or analysis_dict.get('fiber', 0),
                    'sugar': analysis_dict.get('sugar_g', 0) or analysis_dict.get('sugar', 0),
                    'sodium': analysis_dict.get('sodium_mg', 0) or analysis_dict.get('sodium', 0),
                    'energy_score': analysis_dict.get('energy_score', 50),
                    'updated_at': datetime.utcnow().isoformat()
                }

                if existing.data:
                    res = supabase.table('diet_records').update(record_payload).eq('id', existing.data[0]['id']).execute()
                else:
                    record_payload['created_at'] = datetime.utcnow().isoformat()
                    res = supabase.table('diet_records').insert(record_payload).execute()

                if res.data:
                    record = res.data[0]
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        return jsonify({
            'success': True,
            'record': record,
            'message': "Diet record saved successfully!"
        })

@app.route('/api/fitness/stats', methods=['GET'])
@app.route('/api/fitness/data', methods=['GET'])
def get_fitness_stats():
    """Retrieve authoritative fitness summary and widgets data."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized.'}), 401

    target_date = request.args.get('date') or date.today().isoformat()
    metrics = compute_goal_score_and_aura(user_id, target_date, update_db=True)
    user = fetch_user_by_id(user_id) or {}

    return jsonify({
        'success': True,
        'date': target_date,
        'user': user,
        **metrics
    })

# ==========================================
# SCRATCHPAD & UTILITY APIS
# ==========================================

@app.route('/api/scratchpad', methods=['GET', 'POST'])
def handle_scratchpad():
    """Persistent Scratchpad for logged-in user."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        try:
            res = supabase.table('scratchpads').select('content').eq('user_id', user_id).execute()
            content = res.data[0].get('content', '') if res.data else ''
            return jsonify({'success': True, 'content': content})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    elif request.method == 'POST':
        data = request.get_json(force=True) or {}
        content = data.get('content', '')

        try:
            check = supabase.table('scratchpads').select('id').eq('user_id', user_id).execute()
            if check.data:
                supabase.table('scratchpads').update({
                    'content': content,
                    'updated_at': datetime.utcnow().isoformat()
                }).eq('user_id', user_id).execute()
            else:
                supabase.table('scratchpads').insert({
                    'user_id': user_id,
                    'content': content,
                    'updated_at': datetime.utcnow().isoformat()
                }).execute()

            return jsonify({'success': True, 'message': 'Scratchpad saved successfully.'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/scholarpulse-ai', methods=['POST'])
def scholarpulse_ai():
    data = request.get_json(force=True) or {}
    user_message = data.get('message', '').strip()

    if not user_message:
        return jsonify({'success': False, 'error': 'Message required.'}), 400

    system_prompt = (
        'You are ScholarPulse 360 AI. Respond cleanly using bold headings'
        ' (**Heading**) and bullet points (- or *). DO NOT use raw pipe tables'
        ' (|).'
    )
    reply = ask_groq_ai(system_prompt, user_message)

    return jsonify({'success': True, 'reply': reply})

@app.route('/api/youtube-summary', methods=['POST'])
def youtube_summary():
    data = request.get_json(force=True) or {}
    yt_url = data.get('url', '').strip()
    summary_type = data.get('type', 'short').strip().lower()

    if not yt_url:
        return jsonify({'success': False, 'error': 'URL is required.'}), 400

    video_id = extract_youtube_video_id(yt_url)
    if not video_id:
        return jsonify({'success': False, 'error': 'Invalid YouTube URL.'}), 400

    transcript_text = get_youtube_transcript(video_id)

    if not transcript_text:
        return (
            jsonify({
                'success': False,
                'error': (
                    'Captions/Subtitles could not be retrieved. Ensure video has'
                    ' subtitles enabled.'
                ),
            }),
            400,
        )

    truncated_transcript = transcript_text[:9000]

    if summary_type == 'long':
        system_prompt = (
            'You are NoteGPT AI. Provide a detailed summary with key arguments'
            ' and section titles using bold text (**Section**) and bullet points.'
            ' Avoid ASCII tables.'
        )
    else:
        system_prompt = (
            'You are NoteGPT AI. Provide a concise, short bulleted summary with key'
            ' takeaways. Avoid ASCII tables.'
        )

    user_prompt = f'Transcript:\n{truncated_transcript}\n\nTask: Summarize this transcript.'
    summary = ask_groq_ai(system_prompt, user_prompt)

    return jsonify({
        'success': True,
        'video_id': video_id,
        'summary_type': summary_type,
        'summary': summary,
    })

@app.route('/api/holidays', methods=['GET'])
def get_holidays():
    year = request.args.get('year', default=2026, type=int)
    country = request.args.get('country', default='IN', type=str).upper()

    holiday_list = []

    if holidays:
        try:
            country_holidays = holidays.country_holidays(country, years=year)
            for date_obj, name in sorted(country_holidays.items()):
                holiday_list.append({
                    'date': date_obj.strftime('%Y-%m-%d'),
                    'name': name,
                    'day': date_obj.strftime('%A'),
                    'is_national': True,
                })
        except Exception as e:
            print(f'Holidays package error: {e}')

    if not holiday_list:
        fallback_holidays = {
            f'{year}-01-26': 'Republic Day',
            f'{year}-08-15': 'Independence Day',
            f'{year}-10-02': 'Gandhi Jayanti',
            f'{year}-12-25': 'Christmas',
        }
        for date_str, name in fallback_holidays.items():
            d = datetime.strptime(date_str, '%Y-%m-%d')
            holiday_list.append({
                'date': date_str,
                'name': name,
                'day': d.strftime('%A'),
                'is_national': True,
            })

    return jsonify(
        {'success': True, 'year': year, 'country': country, 'holidays': holiday_list}
    )

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True, threaded=True)