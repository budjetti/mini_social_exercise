from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet
import collections
import json
import sqlite3
import hashlib
import random
import re
import os
import uuid
from datetime import datetime

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None


def ensure_image_schema():
    """Add optional profile, media, reporting, and quest columns and tables."""
    db = get_db()
    users_cols = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
    if 'profile_picture' not in users_cols:
        db.execute('ALTER TABLE users ADD COLUMN profile_picture TEXT')
    if 'selected_badge' not in users_cols:
        db.execute('ALTER TABLE users ADD COLUMN selected_badge TEXT')

    posts_cols = {row['name'] for row in db.execute('PRAGMA table_info(posts)')}
    if 'image' not in posts_cols:
        db.execute('ALTER TABLE posts ADD COLUMN image TEXT')

    db.execute('''
        CREATE TABLE IF NOT EXISTS quest_tracking (
            user_id INTEGER NOT NULL,
            quest_key TEXT NOT NULL,
            PRIMARY KEY (user_id, quest_key)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS app_migrations (
            migration_key TEXT PRIMARY KEY
        )
    ''')

    for table in ('users', 'posts', 'comments'):
        columns = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
        if 'reported' not in columns:
            db.execute(f'ALTER TABLE {table} ADD COLUMN reported INTEGER NOT NULL DEFAULT 0')
            columns.add('reported')
        if 'report_count' not in columns:
            db.execute(f'ALTER TABLE {table} ADD COLUMN report_count INTEGER NOT NULL DEFAULT 0')
        db.execute(
            f'UPDATE {table} SET report_count = 1 WHERE reported = 1 AND report_count = 0'
        )

    badge_migration = db.execute(
        'SELECT 1 FROM app_migrations WHERE migration_key = ?',
        ('randomize_existing_user_badges_v1',)
    ).fetchone()
    if not badge_migration:
        # Give existing accounts one random earned badge, or None, exactly once.
        for user in db.execute('SELECT id FROM users').fetchall():
            earned_badges = [
                quest['key']
                for quest in get_user_quests(user['id'])
                if quest['completed']
            ]
            selected_badge = random.choice([None, *earned_badges])
            db.execute(
                'UPDATE users SET selected_badge = ? WHERE id = ?',
                (selected_badge, user['id'])
            )
        db.execute(
            'INSERT INTO app_migrations (migration_key) VALUES (?)',
            ('randomize_existing_user_badges_v1',)
        )

    db.commit()


@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')

REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# Quest definitions drive both profile progress displays and earned badge choices.
# Each kind below is evaluated from existing activity instead of stored counters.
QUESTS = (
    {
        'key': 'new',
        'name': 'New',
        'badge': '🌱',
        'description': 'Join Mini Social!',
        'target': 1,
        'kind': 'new',
    },
    {
        'key': 'educated',
        'name': 'Educated',
        'badge': '🎓',
        'description': 'React once, comment once, and post once.',
        'target': 3,
        'kind': 'educated',
    },
    {
        'key': 'theatrical',
        'name': 'Theatrical',
        'badge': '🎭',
        'description': 'React to five posts.',
        'target': 5,
        'kind': 'reactions',
    },
    {
        'key': 'responsive',
        'name': 'Responsive',
        'badge': '💬',
        'description': 'Comment on five posts.',
        'target': 5,
        'kind': 'comments',
    },
    {
        'key': 'talkative',
        'name': 'Talkative',
        'badge': '🎤',
        'description': 'Post five times.',
        'target': 5,
        'kind': 'posts',
    },
    {
        'key': 'picturesque',
        'name': 'Picturesque',
        'badge': '📷',
        'description': 'Post an image.',
        'target': 1,
        'kind': 'images',
    },
    {
        'key': 'literate',
        'name': 'Literate',
        'badge': '✍️',
        'description': 'Use 100+ words in one post or comment.',
        'target': 100,
        'kind': 'words',
    },
)
QUESTS_BY_KEY = {quest['key']: quest for quest in QUESTS}


def get_user_quests(user_id):
    """Calculate live quest progress and completion rates for every user."""
    db = get_db()
    users = db.execute('SELECT id FROM users').fetchall()
    user_metrics = {
        user['id']: {
            'posts': 0,
            'images': 0,
            'comments': 0,
            'reactions': 0,
            'longest_content_words': 0,
        }
        for user in users
    }

    for row in db.execute('''
        SELECT user_id, COUNT(*) AS post_count,
               SUM(CASE WHEN image IS NOT NULL AND TRIM(image) != '' THEN 1 ELSE 0 END) AS image_count
        FROM posts
        GROUP BY user_id
    '''):
        if row['user_id'] in user_metrics:
            user_metrics[row['user_id']]['posts'] = row['post_count']
            user_metrics[row['user_id']]['images'] = row['image_count'] or 0

    # Reaction and comment progress counts distinct posts, not repeat actions on one post.
    for row in db.execute('''
        SELECT user_id, COUNT(DISTINCT post_id) AS commented_posts
        FROM comments
        GROUP BY user_id
    '''):
        if row['user_id'] in user_metrics:
            user_metrics[row['user_id']]['comments'] = row['commented_posts']

    for row in db.execute('''
        SELECT user_id, COUNT(DISTINCT post_id) AS reacted_posts
        FROM reactions
        GROUP BY user_id
    '''):
        if row['user_id'] in user_metrics:
            user_metrics[row['user_id']]['reactions'] = row['reacted_posts']

    # The Literate quest uses the longest single post or comment, not a combined total.
    for row in db.execute('''
        SELECT user_id, content FROM posts
        UNION ALL
        SELECT user_id, content FROM comments
    '''):
        if row['user_id'] in user_metrics:
            word_count = len(re.findall(r'\b\w+\b', row['content'] or '', re.UNICODE))
            user_metrics[row['user_id']]['longest_content_words'] = max(
                user_metrics[row['user_id']]['longest_content_words'],
                word_count
            )

    quest_completions = {quest['key']: 0 for quest in QUESTS}
    current_metrics = user_metrics.get(user_id, {})
    current_quests = []

    # Educated requires at least one of each action, so its progress has three steps.
    for metrics in user_metrics.values():
        metrics['educated'] = sum((
            metrics['reactions'] > 0,
            metrics['comments'] > 0,
            metrics['posts'] > 0,
        ))

    for quest in QUESTS:
        kind = quest['kind']
        for metrics in user_metrics.values():
            if kind == 'new':
                completed = True
            elif kind == 'educated':
                completed = metrics['educated'] >= quest['target']
            elif kind == 'reactions':
                completed = metrics['reactions'] >= quest['target']
            elif kind == 'comments':
                completed = metrics['comments'] >= quest['target']
            elif kind == 'posts':
                completed = metrics['posts'] >= quest['target']
            elif kind == 'images':
                completed = metrics['images'] >= quest['target']
            else:
                completed = metrics['longest_content_words'] >= quest['target']
            quest_completions[quest['key']] += int(completed)

        if kind == 'new':
            progress = 1
        elif kind == 'educated':
            progress = current_metrics.get('educated', 0)
        elif kind == 'reactions':
            progress = current_metrics.get('reactions', 0)
        elif kind == 'comments':
            progress = current_metrics.get('comments', 0)
        elif kind == 'posts':
            progress = current_metrics.get('posts', 0)
        elif kind == 'images':
            progress = current_metrics.get('images', 0)
        else:
            progress = current_metrics.get('longest_content_words', 0)

        progress = min(progress, quest['target'])
        # Compare completion counts against the entire registered-user population.
        completed_count = quest_completions[quest['key']]
        total_users = len(user_metrics)
        current_quests.append({
            **quest,
            'progress': progress,
            'completed': progress >= quest['target'],
            'completed_users': completed_count,
            'completion_percent': (
                round(completed_count * 100 / total_users, 1)
                if total_users else 0
            ),
        })

    return current_quests


@app.template_global()
def display_username(username):
    """Format a username with the badge selected by that account."""
    if not username:
        return ''
    badge_cache = getattr(g, 'username_badges', None)
    if badge_cache is None:
        # Cache badge lookups for the request because templates render names repeatedly.
        badge_cache = {}
        g.username_badges = badge_cache
    if username not in badge_cache:
        user = query_db(
            'SELECT selected_badge FROM users WHERE username = ?',
            (username,),
            one=True
        )
        selected_key = user['selected_badge'] if user else None
        badge_cache[username] = QUESTS_BY_KEY.get(selected_key, {}).get('badge', '')
    badge = badge_cache[username]
    return f'{username} {badge}'.rstrip()


# Keyword groups provide a simple local classifier for both existing and new posts.
POST_TOPIC_KEYWORDS = {
    'DIY & Home Projects': (
        'diy', 'woodworking', 'handy', 'built', 'building', 'fixing', 'repair',
        'home improvement', 'home decor', 'decor project', 'upcycling',
        'bookshelf', 'terrarium', 'craft', 'handmade', 'electronics project',
        'diylife', 'diychallenge', 'diymagic', 'diyproject',
    ),
    'Gardening': (
        'garden', 'gardening', 'plant', 'plants', 'flower', 'flowers',
        'vegetable garden', 'houseplant', 'terrarium',
    ),
    'Food & Cooking': (
        'food', 'cooking', 'cook', 'cooked', 'recipe', 'meal', 'dinner',
        'breakfast', 'lunch', 'bake', 'baking', 'vegan', 'chef', 'salad',
        'coffee', 'cafe', 'chocolate', 'fondue', 'foodie', 'sweet treat',
    ),
    'Travel & Adventure': (
        'travel', 'trip', 'visited', 'visiting', 'vacation', 'holiday',
        'exploring', 'adventure', 'hike', 'hiking', 'solo trip', 'fjords',
    ),
    'Nature & Environment': (
        'nature', 'wildlife', 'climate', 'environment', 'conservation',
        'renewable energy', 'sustainability', 'eco', 'forest', 'mountain',
        'landscape', 'naturelover', 'climatecrisis', 'ecowarrior',
    ),
    'Animals & Pets': (
        'animal', 'animals', 'pet', 'pets', 'dog', 'dogs', 'puppy', 'pup',
        'cat', 'cats', 'kitten', 'reptile', 'shelter', 'adopt', 'adoption',
        'reptilelove', 'catlover', 'wildlife sanctuary',
    ),
    'Mental Health & Wellbeing': (
        'mental health', 'wellbeing', 'wellness', 'stress', 'anxiety',
        'depression', 'self care', 'therapy', 'support group',
        'mentalhealthawareness', 'mentalhealthmatters',
    ),
    'Mindfulness & Personal Growth': (
        'mindfulness', 'gratitude', 'personal growth', 'self help',
        'inspiration', 'reflection', 'perspective', 'overthinking',
        'mindful', 'selfcare', 'spreadkindness',
    ),
    'Books & Reading': (
        'book', 'books', 'reading', 'read', 'reader', 'audiobook', 'ebook',
        'novel', 'bookworm', 'audiobooklover',
    ),
    'Art & Creativity': (
        'art', 'artist', 'art festival', 'creativity', 'creative', 'painting',
        'drawing', 'photography', 'design', 'home decor', 'creativechaos',
    ),
    'Technology': (
        'technology', 'tech', 'ai', 'software', 'computer', 'phone', 'app',
        'electronics', 'bluetooth', 'speaker', 'techtalk', 'techstruggles',
    ),
    'Gaming': (
        'game', 'games', 'gaming', 'gamer', 'video game', 'co op',
        'virtual world', 'gamingtogether',
    ),
    'Parenting & Family': (
        'parent', 'parenting', 'kid', 'kids', 'child', 'children', 'family',
        'mother', 'father', 'mom', 'dad', 'parentingwin', 'handyparent',
    ),
    'Community & Volunteering': (
        'community', 'volunteer', 'volunteering', 'charity', 'local',
        'neighborhood', 'shelter', 'communitysupport', 'naturevolunteer',
    ),
    'Music': (
        'music', 'song', 'songs', 'playlist', 'hip hop', 'pop music',
        'classical music', 'musician', 'musiclover', 'popvibes',
    ),
    'Film & TV': (
        'movie', 'movies', 'film', 'cinema', 'tv show', 'documentary',
        'cinemaetiquette',
    ),
    'Fitness & Sports': (
        'fitness', 'gym', 'workout', 'exercise', 'training', 'sports',
        'football', 'running', 'run', 'squat', 'gym life',
    ),
    'Education & Learning': (
        'education', 'school', 'learning', 'learned', 'study', 'student',
        'educational reform',
    ),
    'News & Politics': (
        'news', 'politics', 'political', 'policy', 'government', 'election',
        'reform', 'independence day', 'freedom',
    ),
    'Kindness & Relationships': (
        'kindness', 'friend', 'friends', 'relationship', 'relationships',
        'good deed', 'spreadkindness', 'kindness',
    ),
}

POST_TOPIC_PATTERNS = {
    topic: tuple(
        re.compile(
            r'(?<!\w)'
            + re.escape(re.sub(r'[^\w]+', ' ', keyword.casefold()).strip()).replace(r'\ ', r'\s+')
            + r'(?!\w)'
        )
        for keyword in keywords
    )
    for topic, keywords in POST_TOPIC_KEYWORDS.items()
}


# Ensure the image-related columns exist before any route uses them.
with app.app_context():
    ensure_image_schema()


def classify_post_topics(content):
    """Return all keyword-based topics found in a post's text and hashtags."""
    # Split hashtag CamelCase before matching the normalized topic keywords.
    content = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', content or '')
    content = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', content)
    normalized_content = re.sub(r'[^\w]+', ' ', content.casefold())
    return [
        topic
        for topic, patterns in POST_TOPIC_PATTERNS.items()
        if any(pattern.search(normalized_content) for pattern in patterns)
    ]


def save_uploaded_image(upload_file, subfolder='uploads'):
    """Save a user-uploaded image and return the relative static path."""
    if not upload_file or not upload_file.filename:
        return None

    extension = os.path.splitext(upload_file.filename)[1].lower().lstrip('.')
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        return None

    filename = f"{uuid.uuid4().hex}.{extension}"
    target_dir = app.config['UPLOAD_FOLDER'] if subfolder == 'uploads' else os.path.join(app.config['UPLOAD_FOLDER'], subfolder)
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, filename)
    upload_file.save(target_path)
    relative_dir = 'uploads' if subfolder == 'uploads' else subfolder
    relative_path = os.path.join(relative_dir, filename)
    return relative_path.replace('\\', '/')


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    quest_tracker = []
    if current_user_id:
        tracked_quest_keys = {
            row['quest_key']
            for row in query_db(
                'SELECT quest_key FROM quest_tracking WHERE user_id = ?',
                (current_user_id,)
            )
        }
        if tracked_quest_keys:
            quest_tracker = [
                quest for quest in get_user_quests(current_user_id)
                if quest['key'] in tracked_quest_keys
            ]
    params = []

    # Rank users by total reactions, then collect their three most common reaction types.
    reaction_leaderboard = query_db('''
        SELECT u.id, u.username, COUNT(r.id) AS lifetime_reactions
        FROM users u
        JOIN posts p ON p.user_id = u.id
        JOIN reactions r ON r.post_id = p.id
        GROUP BY u.id, u.username
        ORDER BY lifetime_reactions DESC, u.username COLLATE NOCASE ASC, u.id ASC
        LIMIT 10
    ''')
    if reaction_leaderboard:
        leaderboard_ids = [leader['id'] for leader in reaction_leaderboard]
        placeholders = ','.join('?' for _ in leaderboard_ids)
        reaction_rows = query_db(f'''
            SELECT p.user_id, r.reaction_type, COUNT(*) AS reaction_count
            FROM posts p
            JOIN reactions r ON r.post_id = p.id
            WHERE p.user_id IN ({placeholders})
            GROUP BY p.user_id, r.reaction_type
            ORDER BY p.user_id, reaction_count DESC, r.reaction_type ASC
        ''', leaderboard_ids)
        reactions_by_user = {user_id: [] for user_id in leaderboard_ids}
        for reaction in reaction_rows:
            user_reactions = reactions_by_user[reaction['user_id']]
            if len(user_reactions) < 3:
                user_reactions.append(reaction)
        reaction_leaderboard = [
            {**dict(leader), 'reactions': reactions_by_user[leader['id']]}
            for leader in reaction_leaderboard
        ]

    #  2. Build the Query 
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = "WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        params.append(current_user_id)

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   u.profile_picture, u.created_at as user_created_at,
                   p.image, IFNULL(r.total_reactions, 0) as total_reactions
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   u.profile_picture, u.created_at as user_created_at, p.image
            FROM posts p
            JOIN users u ON p.user_id = u.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_data = []
    user_risk_scores = {}
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post['id'],))
        post_dict = dict(post)
        if session.get('username') == 'admin':
            user_id = post_dict['user_id']
            # Reuse each author's account score across their posts in this feed.
            if user_id not in user_risk_scores:
                user_risk_scores[user_id] = user_risk_analysis(user_id)
            post_dict.update(post_moderation_metrics(
                post_dict['content'],
                post_dict['user_created_at'],
                user_risk_scores[user_id]
            ))
        post_dict['content'], _ = moderate_content(post_dict['content'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated
        })

    #  4. Render Template with Pagination Info 
    return render_template('feed.html.j2', 
                           posts=posts_data, 
                           reaction_leaderboard=reaction_leaderboard,
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           quest_tracker=quest_tracker)

@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Get content and optional uploaded image from the submitted form.
    content = request.form.get('content')
    uploaded_image = request.files.get('image')
    image_path = save_uploaded_image(uploaded_image)

    # Preserve the original for risk scoring; content is filtered when displayed.
    content_to_store = content

    # Basic validation to ensure post is not empty and allow an image-only post.
    if (content_to_store and content_to_store.strip()) or image_path:
        db = get_db()
        db.execute('INSERT INTO posts (user_id, content, image) VALUES (?, ?, ?)',
                   (user_id, content_to_store or '', image_path))
        db.commit()
        flash('Your post was successfully created!', 'success')
    else:
        # This will catch empty posts or posts that were fully censored.
        flash('Post cannot be empty or was fully censored.', 'warning')

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))
    
    
@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))


@app.route('/report/<string:content_type>/<int:item_id>', methods=['POST'])
def report_content(content_type, item_id):
    """Mark a post, comment, or profile as reported and return to its page."""
    if not session.get('user_id'):
        flash('You must be logged in to report content.', 'danger')
        return redirect(url_for('login'))

    report_targets = {
        'post': ('posts', 'Post', 'id, user_id'),
        'comment': ('comments', 'Comment', 'id, user_id, post_id'),
        'profile': ('users', 'Profile', 'id AS user_id, username'),
    }
    if content_type not in report_targets:
        abort(404)

    table, label, selected_columns = report_targets[content_type]
    db = get_db()
    item = db.execute(
        f'SELECT {selected_columns} FROM {table} WHERE id = ?',
        (item_id,)
    ).fetchone()
    if item is None:
        abort(404)

    if item['user_id'] == session['user_id']:
        flash(f'You cannot report your own {content_type}.', 'warning')
        if content_type == 'post':
            return redirect(url_for('post_detail', post_id=item_id))
        if content_type == 'comment':
            return redirect(url_for('post_detail', post_id=item['post_id']))
        return redirect(url_for('user_profile', username=item['username']))

    db.execute(
        f'UPDATE {table} SET reported = 1, report_count = report_count + 1 WHERE id = ?',
        (item_id,)
    )
    db.commit()
    flash(f'{label} reported.', 'success')

    if content_type == 'post':
        return redirect(url_for('post_detail', post_id=item_id))
    if content_type == 'comment':
        return redirect(url_for('post_detail', post_id=item['post_id']))
    return redirect(url_for('user_profile', username=item['username']))


@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio
    quests = get_user_quests(user['id'])
    tracked_quest_keys = set()
    if session.get('user_id') == user['id']:
        tracked_quest_keys = {
            row['quest_key']
            for row in query_db(
                'SELECT quest_key FROM quest_tracking WHERE user_id = ?',
                (user['id'],)
            )
        }
        # A quest no longer needs a tracker once its live progress reaches the target.
        completed_tracked_keys = {
            quest['key']
            for quest in quests
            if quest['completed'] and quest['key'] in tracked_quest_keys
        }
        if completed_tracked_keys:
            db = get_db()
            db.executemany(
                'DELETE FROM quest_tracking WHERE user_id = ? AND quest_key = ?',
                [(user['id'], quest_key) for quest_key in completed_tracked_keys]
            )
            db.commit()
            tracked_quest_keys -= completed_tracked_keys

    posts_raw = query_db('SELECT id, content, user_id, created_at, image FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    # Compute detailed metrics only for admins; regular profile views stay unchanged.
    user_score = user_risk_analysis(user['id']) if session.get('username') == 'admin' else None
    for post_raw in posts_raw:
        post = dict(post_raw)
        if user_score is not None:
            post.update(post_moderation_metrics(post['content'], user['created_at'], user_score))
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        posts.append(post)

    comments_raw = query_db('SELECT id, content, user_id, post_id, created_at FROM comments WHERE user_id = ? ORDER BY created_at DESC LIMIT 100', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']
    # Count every reaction attached to a post authored by this profile's user.
    reactions_count = query_db('''
        SELECT COUNT(*) as cnt
        FROM reactions r
        JOIN posts p ON p.id = r.post_id
        WHERE p.user_id = ?
    ''', (user['id'],), one=True)['cnt']

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --

    return render_template('user_profile.html.j2', 
                           user=user, 
                           posts=posts, 
                           comments=comments,
                           followers_count=followers_count, 
                           following_count=following_count,
                           reactions_count=reactions_count,
                           is_following=is_currently_following,
                           quests=quests,
                           tracked_quest_keys=tracked_quest_keys)


@app.route('/quests/<quest_key>/<action>', methods=['POST'])
def set_quest_tracking(quest_key, action):
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to track quests.', 'danger')
        return redirect(url_for('login'))
    if quest_key not in QUESTS_BY_KEY or action not in {'track', 'untrack'}:
        abort(404)

    db = get_db()
    if action == 'track':
        quest = next(
            quest for quest in get_user_quests(user_id)
            if quest['key'] == quest_key
        )
        if quest['completed']:
            flash('Completed quests cannot be tracked.', 'info')
        else:
            # INSERT OR IGNORE makes repeated track requests idempotent.
            db.execute(
                'INSERT OR IGNORE INTO quest_tracking (user_id, quest_key) VALUES (?, ?)',
                (user_id, quest_key)
            )
            db.commit()
            flash(f'{quest["name"]} quest is now tracked.', 'success')
    else:
        db.execute(
            'DELETE FROM quest_tracking WHERE user_id = ? AND quest_key = ?',
            (user_id, quest_key)
        )
        db.commit()
        flash('Quest untracked.', 'info')

    username = session.get('username')
    if username:
        return redirect(url_for('user_profile', username=username))
    return redirect(url_for('feed'))


@app.route('/profile/badge', methods=['POST'])
def select_profile_badge():
    user_id = session.get('user_id')
    username = session.get('username')
    if not user_id or not username:
        flash('You must be logged in to select a badge.', 'danger')
        return redirect(url_for('login'))

    badge_key = request.form.get('badge_key', '')
    if badge_key:
        if badge_key not in QUESTS_BY_KEY:
            abort(400)
        quest = next(
            quest for quest in get_user_quests(user_id)
            if quest['key'] == badge_key
        )
        if not quest['completed']:
            flash('You can only select badges for completed quests.', 'warning')
            return redirect(url_for('user_profile', username=username))
        selected_badge = badge_key
    else:
        # An empty selection means no badge; the display helper omits its emoji.
        selected_badge = None

    db = get_db()
    db.execute(
        'UPDATE users SET selected_badge = ? WHERE id = ?',
        (selected_badge, user_id)
    )
    db.commit()
    flash('Profile badge updated.', 'success')
    return redirect(url_for('user_profile', username=username))


@app.route('/u/<username>/picture', methods=['POST'])
def upload_profile_picture(username):
    """Allow a logged-in user to upload or replace their profile picture."""
    current_user_id = session.get('user_id')
    if not current_user_id:
        flash('You must be logged in to upload a profile picture.', 'danger')
        return redirect(url_for('login'))

    user = query_db('SELECT id, username FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)

    if user['id'] != current_user_id:
        flash('You can only update your own profile picture.', 'danger')
        return redirect(url_for('user_profile', username=username))

    uploaded_file = request.files.get('profile_picture')
    image_path = save_uploaded_image(uploaded_file)
    if image_path is None:
        flash('Please upload a valid image file.', 'warning')
        return redirect(url_for('user_profile', username=username))

    db = get_db()
    db.execute('UPDATE users SET profile_picture = ? WHERE id = ?', (image_path, current_user_id))
    db.commit()
    flash('Profile picture updated.', 'success')
    return redirect(url_for('user_profile', username=username))
    

@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
               u.profile_picture, u.created_at as user_created_at, p.image
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    if session.get('username') == 'admin':
        post['moderation_metrics'] = post_moderation_metrics(
            post['content'],
            post['user_created_at'],
            user_risk_analysis(post['user_id'])
        )
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        print(comment['content'])
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, password, location, birthdate, profile) VALUES (?, ?, ?, ?, ?)',
                (username, hashed_password, location, birthdate, profile)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            flash(f'Welcome, {username}! Your account has been created.', 'success')
            return redirect(url_for('feed')) # Redirect to the main feed/dashboard

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()
            
    return render_template('signup.html.j2')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

def is_first_engagement(post_id, user_id):
    post = query_db('''
        SELECT p.user_id,
               NOT EXISTS (SELECT 1 FROM reactions WHERE post_id = p.id)
               AND NOT EXISTS (SELECT 1 FROM comments WHERE post_id = p.id) AS no_prior_engagement
        FROM posts p
        WHERE p.id = ?
    ''', (post_id,), one=True)
    return bool(post and post['user_id'] != user_id and post['no_prior_engagement'])


@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        first_engagement = is_first_engagement(post_id, user_id)
        db = get_db()
        db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        db.commit()
        if first_engagement:
            flash('You were the first user to engage with this post. Great work!', 'first_engagement')
        else:
            flash('Your comment was added.', 'success')
    else:
        flash('Comment cannot be empty.', 'warning')

    # Redirect back to the page the user came from (likely the post detail page)
    return redirect(request.referrer or url_for('post_detail', post_id=post_id))

@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        first_engagement = is_first_engagement(post_id, user_id)
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    if not existing_reaction and first_engagement:
        flash('You were the first user to engage with this post. Great work!', 'first_engagement')

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
    
    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    current_tab = request.args.get('tab', 'users') # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db(
        'SELECT id, username, profile, created_at, reported, report_count FROM users'
    )
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: (x['reported'], x['risk_score']), reverse=True)
    user_risk_scores = {
        user['id']: user['risk_score']
        for user in all_users
    }
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, p.reported, p.report_count, u.id as user_id,
               u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.reported DESC, p.id DESC
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        metrics = post_moderation_metrics(
            post_dict['content'],
            post_dict['user_created_at'],
            user_risk_scores[post_dict['user_id']]
        )
        post_dict['content_score'] = metrics['content_score']
        post_dict['user_risk_score'] = metrics['user_risk_score']
        final_score = metrics['post_risk_score']
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: (x['reported'], x['risk_score']), reverse=True)

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, c.reported, c.report_count, u.username,
               u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.reported DESC, c.id DESC
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, comments_offset))
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        _, score = moderate_content(comment_dict['content'])
        author_created_dt = comment_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: (x['reported'], x['risk_score']), reverse=True)


    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           PAGE_SIZE=PAGE_SIZE)



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM quest_tracking WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/rules')
def rules():
    return render_template('rules.html.j2')

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# ----- Functions to be implemented are below
# Coding Assignment #2

# Assignment 2.2
def user_risk_analysis(user_id):
    """
    Args:
        user_id: The ID of the user on which we perform risk analysis.

    Returns:
        A float number score showing the risk associated with this user. There are no strict rules or bounds to this score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk. (An upper bound of 5.0 is applied to this score elsewhere in the codebase) 
        
        You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
        Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """
    
    db = get_db()
    user = db.execute(
        'SELECT profile, created_at FROM users WHERE id = ?',
        (user_id,)
    ).fetchone()
    if user is None:
        raise LookupError(f'User {user_id} does not exist')

    profile_score = moderate_content(user['profile'])[1]
    # Average content scores within each content type before applying the specified weights.
    post_rows = db.execute(
        'SELECT content FROM posts WHERE user_id = ?',
        (user_id,)
    ).fetchall()
    comment_rows = db.execute(
        'SELECT content FROM comments WHERE user_id = ?',
        (user_id,)
    ).fetchall()

    average_post_score = (
        sum(moderate_content(post['content'])[1] for post in post_rows) / len(post_rows)
        if post_rows else 0.0
    )
    average_comment_score = (
        sum(moderate_content(comment['content'])[1] for comment in comment_rows) / len(comment_rows)
        if comment_rows else 0.0
    )
    content_risk_score = profile_score + average_post_score * 3 + average_comment_score

    # Younger accounts receive a larger multiplier, then the final user score is capped.
    created_at = user['created_at']
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    if created_at is None:
        raise ValueError(f'User {user_id} has no account creation timestamp')
    now = datetime.now(created_at.tzinfo) if created_at.tzinfo else datetime.utcnow()
    account_age_days = (now - created_at).days

    if account_age_days < 7:
        content_risk_score *= 1.5
    elif account_age_days < 30:
        content_risk_score *= 1.2

    return min(5.0, float(content_risk_score))

    
# Assignment 2.1
def moderate_content(content):
    """
    Args
        content: the text content of a post or comment to be moderated.
        
    Returns: 
        A tuple containing the moderated content (string) and a severity score (float). There are no strict rules or bounds to the severity score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk.
    
    This function moderates a string of content and calculates a severity score based on
    rules loaded from the 'censorship.dat' file. These are already loaded as TIER1_WORDS, TIER2_PHRASES and TIER3_WORDS. Tier 1 corresponds to strong profanity, Tier 2 to scam/spam phrases and Tier 3 to mild profanity.
    
    You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
    Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """

    if content is None:
        content = ''
    if not isinstance(content, str):
        raise TypeError('Content to moderate must be a string')

    # Severe words and spam phrases remove the entire item and bypass later scoring.
    for word in TIER1_WORDS:
        if word and re.search(r'(?<!\w)' + re.escape(word) + r'(?!\w)', content, re.IGNORECASE):
            return '[content removed due to severe violation]', 5.0

    for phrase in TIER2_PHRASES:
        if phrase and re.search(re.escape(phrase), content, re.IGNORECASE):
            return '[content removed due to spam/scam policy]', 5.0

    score = 0.0
    moderated_content = content
    # Match common URL forms, including domains that disguise dots as [.].
    url_pattern = re.compile(
        r"""(?ix)
        \b(?:https?://|www\.)[^\s<>()\[\]{}"'`]+
        |
        (?<![@\w])
        (?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.|\[\.\]))+[a-z]{2,}
        (?::\d+)?
        (?:/[^\s<>()\[\]{}"'`]*)?
        (?:\?[^\s<>()\[\]{}"'`]*)?
        (?:\#[^\s<>()\[\]{}"'`]*)?
        """
    )
    # Keep original-text spans so links still count if their text also matches a mild word.
    urls = list(url_pattern.finditer(content))

    # Mask every whole-word mild violation and add two points per occurrence.
    tier3_words = dict.fromkeys(word.casefold() for word in TIER3_WORDS if word)
    for word in tier3_words:
        pattern = re.compile(r'(?<!\w)' + re.escape(word) + r'(?!\w)', re.IGNORECASE)
        matches = list(pattern.finditer(moderated_content))
        if matches:
            score += 2.0 * len(matches)
            moderated_content = pattern.sub(
                lambda match: '*' * len(match.group(0)),
                moderated_content
            )

    if urls:
        score += 2.0 * len(urls)
    # Replace from right to left so earlier match offsets remain valid.
    for match in reversed(urls):
        url = match.group(0)
        trimmed_url = url.rstrip('.,!?;:')
        replacement = '[link removed]' + url[len(trimmed_url):]
        moderated_content = (
            moderated_content[:match.start()]
            + replacement
            + moderated_content[match.end():]
        )

    # Score capitalization from the original text; this rule does not alter content.
    alphabetic_characters = [character for character in content if character.isalpha()]
    if (
        len(alphabetic_characters) > 15
        and sum(character.isupper() for character in alphabetic_characters)
        / len(alphabetic_characters) > 0.7
    ):
        score += 0.5

    return moderated_content, score


def post_moderation_metrics(content, author_created_at, user_score):
    """Return the rule-based content and post risk metrics for a post."""
    content_score = moderate_content(content)[1]
    if isinstance(author_created_at, str):
        author_created_at = datetime.fromisoformat(author_created_at)
    if author_created_at is None:
        raise ValueError('Post author has no account creation timestamp')

    # Apply the same under-seven-day multiplier used for post and comment risk.
    now = datetime.now(author_created_at.tzinfo) if author_created_at.tzinfo else datetime.utcnow()
    post_risk_score = content_score
    if (now - author_created_at).days < 7:
        post_risk_score *= 1.5

    # Convert the adjusted numeric score to the dashboard's four risk labels.
    if post_risk_score >= 5:
        risk_level = 'HIGH'
    elif post_risk_score >= 3:
        risk_level = 'MEDIUM'
    elif post_risk_score >= 1:
        risk_level = 'LOW'
    else:
        risk_level = 'NONE'

    return {
        'content_score': round(content_score, 2),
        'post_risk_score': round(post_risk_score, 2),
        'user_risk_score': round(user_score, 2),
        'risk_level': risk_level,
    }


# Coding Assignment #3
# Assignment 3.1
def recommend(user_id, filter_following):
    """Recommend up to five posts using reaction-topic affinity and follows."""
    db = get_db()
    # Score the whole eligible pool so recommendations are global, not page-limited.
    posts_query = '''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
               u.profile_picture, u.created_at as user_created_at, p.image
        FROM posts p
        JOIN users u ON p.user_id = u.id
    '''
    posts_params = []
    if user_id is not None:
        posts_query += ' WHERE p.user_id != ?'
        posts_params.append(user_id)
    if filter_following and user_id is not None:
        posts_query += '''
            AND p.user_id IN (
                SELECT followed_id FROM follows WHERE follower_id = ?
            )
        '''
        posts_params.append(user_id)
    posts_query += ' ORDER BY p.created_at DESC, p.id DESC'
    posts = db.execute(posts_query, posts_params).fetchall()

    positive_reaction_weights = {
        'like': 1.0,
        'love': 1.5,
        'laugh': 0.75,
        'haha': 0.75,
        'wow': 0.75,
    }
    user_reactions = []
    if user_id is not None:
        user_reactions = db.execute(
            '''
            SELECT r.post_id, r.reaction_type, p.content
            FROM reactions r
            JOIN posts p ON p.id = r.post_id
            WHERE r.user_id = ?
            ''',
            (user_id,)
        ).fetchall()

    # Classify both eligible candidates and the posts that provide interest signals.
    topics_by_post = {
        post['id']: classify_post_topics(post['content'])
        for post in posts
    }
    reacted_post_ids = {reaction['post_id'] for reaction in user_reactions}
    topics_by_post.update({
        reaction['post_id']: classify_post_topics(reaction['content'])
        for reaction in user_reactions
    })

    # Positive reactions build a weighted profile; neutral/negative reaction types add nothing.
    topic_weights = collections.defaultdict(float)
    for reaction in user_reactions:
        reaction_weight = positive_reaction_weights.get(reaction['reaction_type'], 0)
        if not reaction_weight:
            continue
        for topic in topics_by_post.get(reaction['post_id'], ()):
            topic_weights[topic] += reaction_weight

    total_topic_weight = sum(topic_weights.values())
    if total_topic_weight:
        topic_affinity = {
            topic: weight / total_topic_weight
            for topic, weight in topic_weights.items()
        }
    else:
        topic_affinity = {}

    # Exclude already-reacted posts and compare topic overlap; broader multi-topic posts
    # are lightly normalized so they do not win merely by matching many labels.
    ranked_posts = []
    for post in posts:
        if post['id'] in reacted_post_ids:
            continue

        topics = topics_by_post[post['id']]
        topic_score = sum(topic_affinity.get(topic, 0) for topic in topics)
        if topics:
            topic_score /= len(topics) ** 0.5
        ranked_posts.append((topic_score, post))

    ranked_posts.sort(
        key=lambda item: (
            item[0],
            str(item[1]['created_at']),
            item[1]['id'],
        ),
        reverse=True
    )
    # Choose the five best matches, then show that chosen set newest-first.
    selected_posts = [post for _, post in ranked_posts[:5]]
    selected_posts.sort(
        key=lambda post: (str(post['created_at']), post['id']),
        reverse=True
    )
    return selected_posts

if __name__ == '__main__':
    app.run(debug=True, port=8080)
