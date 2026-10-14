from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import sqlite3
import hashlib
import re
import heapq
import random
import time
from urllib.parse import quote
from datetime import datetime

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'

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


# New functionality for the course. Work in progress...11.9.2026

def init_task_db():
    """Creates tables for tasks, watchlist, and direct-ask requests."""
    db = get_db()
    db.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            category TEXT NOT NULL,
            assigned_user_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (assigned_user_id) REFERENCES users (id)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS watchlist (
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, task_id),
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS task_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            from_user_id INTEGER NOT NULL,
            to_user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending, accepted, declined, cancelled
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            responded_at TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE,
            FOREIGN KEY (from_user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (to_user_id) REFERENCES users (id) ON DELETE CASCADE
        )
    ''')
    # Design Claim 4: shared group identity 
    # Giving people a name/indicator that marks them as part of a specific community.
    # specific community (rather than just an anonymous task-doer) is meant
    # to increase identity-based commitment to the board itself, not just
    # to the individual task they happen to be working on.

    db.execute('''
        CREATE TABLE IF NOT EXISTS task_board_members (
            user_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'Fixer',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
    ''')
    
    # Create a "category". Badge for the "Fixer" for example. Identity should track
    # to topic they care about.

   
    db.execute('''
        CREATE TABLE IF NOT EXISTS task_category_members (
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, category),
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
    ''')
    # Design Claim 25: off-topic talk + personalized filtering 
    # Off-topic chatter isn't removed for everyone (that would lose the
    # bonding/identity benefit entirely) and it isn't forced on everyone
    # either (that would annoy people who only want the task list). Instead
    # each person filters it for themselves via show_offtopic.
    db.execute('''
        CREATE TABLE IF NOT EXISTS task_board_chatter (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
    ''')
    db.commit()

    # task_board_members may already exist from before this column was
    # added; ALTER TABLE ... ADD COLUMN is proper way to extend it
    # in place without losing existing membership rows.
    try:
        db.execute('''
            ALTER TABLE task_board_members
            ADD COLUMN show_offtopic INTEGER NOT NULL DEFAULT 1
        ''')
        db.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


# The shared name for the group  this is the "indicator" from Design 
# Claim 4. Everyone who engages with the board is a "Fixer" in "The Fixers,"
# not just a person who happened to post or accept one task.

COMMUNITY_NAME = "The Fixers"
MEMBER_TITLE = "Fixer"

def get_or_create_member(user_id):
    """Ensures the user carries the community's membership badge.

    Returns (member_row, is_new_member). is_new_member is True the first
    time this user is seen, so the caller can welcome them into the group
    by name rather than silently tracking them.
    """
    existing = query_db('SELECT * FROM task_board_members WHERE user_id = ?',
                        (user_id,), one=True)
    if existing:
        return existing, False

    db = get_db()
    db.execute('INSERT INTO task_board_members (user_id, title) VALUES (?, ?)',
              (user_id, MEMBER_TITLE))
    db.commit()
    new_member = query_db('SELECT * FROM task_board_members WHERE user_id = ?',
                          (user_id,), one=True)
    return new_member, True


# Category titles 

CATEGORY_TITLES = {
    'diy': 'DIY Crew',
    'coding': 'Code Wranglers',
    'errands': 'Errand Runners',
    'tutoring': 'Tutor Circle',
    'moving': 'Moving Crew',
    'general': 'All-Rounders',
}


def category_title(category):
    key = (category or '').strip().lower()
    if key in CATEGORY_TITLES:
        return CATEGORY_TITLES[key]
    label = (category or 'General').strip() or 'General'
    return f'{label} Crew'


def get_or_create_category_member(user_id, category):
    """Grants a category-specific badge the first time a user actively
    participates (posts, asks, or accepts) within that category. This is
    layered on top of the platform-wide 'Fixer' badge, not a replacement
    for it someone can be a Fixer and a DIY Crew member and a Code
    Wrangler all at once. Note to myself...Maybe implementation could be based on completed tasks or based on interest?
    """
    existing = query_db('''
        SELECT * FROM task_category_members WHERE user_id = ? AND category = ?
    ''', (user_id, category), one=True)
    if existing:
        return existing, False

    title = category_title(category)
    db = get_db()
    db.execute('''
        INSERT INTO task_category_members (user_id, category, title)
        VALUES (?, ?, ?)
    ''', (user_id, category, title))
    db.commit()
    new_member = query_db('''
        SELECT * FROM task_category_members WHERE user_id = ? AND category = ?
    ''', (user_id, category), one=True)
    return new_member, True


@app.route('/')
def feed():
    # So the task-board membership/badge tables exist even if this user
    # has never visited /tasks -- we look badges up below.
    init_task_db()

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
    params = []

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
                   IFNULL(r.total_reactions, 0) as total_reactions
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
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p
            JOIN users u ON p.user_id = u.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_list = list(posts)

    # Pre-fetch comments for every post on this page, and collect every
    # poster's and commenter's user id up front, so we can look up their
    # community badge (Design Claim 4) in ONE batched query instead of
    # one query per person per post.
    comments_by_post = {}
    relevant_user_ids = set()
    for post in posts_list:
        relevant_user_ids.add(post['user_id'])
        comments_raw = query_db('''
            SELECT c.id, c.content, c.created_at, u.username, u.id as user_id
            FROM comments c JOIN users u ON c.user_id = u.id
            WHERE c.post_id = ? ORDER BY c.created_at ASC
        ''', (post['id'],))
        comments_by_post[post['id']] = comments_raw
        for comment in comments_raw:
            relevant_user_ids.add(comment['user_id'])

    badge_lookup = {}
    if relevant_user_ids:
        placeholders = ','.join('?' for _ in relevant_user_ids)
        badge_rows = query_db(f'''
            SELECT user_id, title FROM task_board_members
            WHERE user_id IN ({placeholders})
        ''', list(relevant_user_ids)) or []
        badge_lookup = {row['user_id']: row['title'] for row in badge_rows}

    posts_data = []
    for post in posts_list:
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
        comments_raw = comments_by_post[post['id']]
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        post_dict['member_title'] = badge_lookup.get(post['user_id'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comment_dict['member_title'] = badge_lookup.get(comment['user_id'])
            comments_moderated.append(comment_dict)
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated
        })

    # Design Claim 5 (Ch.5): if this member is one of the community's most
    # influential, the feed nudges them to recruit friends. Never let this
    # optional nudge break the feed itself.
    influencer_info = None
    if current_user_id:
        try:
            influencer_info = next((i for i in get_influencer_analysis()['influencers']
                                    if i['user_id'] == current_user_id), None)
        except Exception as e:
            print(f"Influencer analysis failed: {e}")

    #  4. Render Template with Pagination Info 
    return render_template('feed.html.j2', 
                           posts=posts_data, 
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           influencer_info=influencer_info)

@app.route('/tasks')
def task_list():
    """Task board: open tasks, my watchlist, and my incoming direct-ask requests."""
    init_task_db()
    user_id = session.get('user_id')

    # Design Claim 4: grant/surface the shared group identity. Simply
    # showing up to the board (logged in) is enough to become a named
    # member of the community, not just a one-off task participant.
    my_membership = None
    if user_id:
        my_membership, is_new_member = get_or_create_member(user_id)
        if is_new_member:
            flash(f'🎉 Welcome to {COMMUNITY_NAME}! You\'re now a "{MEMBER_TITLE}" '
                  f'-- that badge will show up next to your name anywhere you '
                  f'pitch in on the board.', 'success')

    member_count = query_db('SELECT COUNT(*) as c FROM task_board_members', one=True)['c']

    # Show anything not finished. 'assigned' tasks stay visible so people
    # can see who's on it; only 'done' tasks drop off the board. Each
    # assignee's community title is joined in so their badge travels with
    # their name wherever it's shown.
    open_tasks = query_db('''
        SELECT tasks.*, users.username as assigned_to, tbm.title as assigned_to_title,
               tcm.title as assigned_to_category_title
        FROM tasks
        LEFT JOIN users ON tasks.assigned_user_id = users.id
        LEFT JOIN task_board_members tbm ON tasks.assigned_user_id = tbm.user_id
        LEFT JOIN task_category_members tcm
               ON tcm.user_id = tasks.assigned_user_id AND tcm.category = tasks.category
        WHERE tasks.status != 'done'
        ORDER BY tasks.created_at DESC
        ''') or []

    # Which of these tasks does the current user already have a pending
    # outgoing request on? Used to hide/relabel the "Direct Ask" form so
    # people don't spam the same task repeatedly.
    my_pending_task_ids = set()
    if user_id and open_tasks:
        task_ids = [t['id'] for t in open_tasks]
        placeholders = ','.join('?' for _ in task_ids)
        rows = query_db(f'''
            SELECT task_id FROM task_requests
            WHERE from_user_id = ? AND status = 'pending' AND task_id IN ({placeholders})
        ''', [user_id] + task_ids) or []
        my_pending_task_ids = {r['task_id'] for r in rows}

    # Design Claim 3: target people who are interested and able
    # Instead of the asker having to guess a username at random, suggest
    # candidates who've already shown a signal of interest/ability for
    # THIS task: people watching it strongest signal/interested in
    # this exact task, then people holding this task's category badge
    # interested/able in this domain generally. 
    suggested_candidates = {}
    if user_id and open_tasks:
        for t in open_tasks:
            if t['status'] != 'open' or t['id'] in my_pending_task_ids:
                continue  # no point suggesting candidates for a form we won't show

            watcher_rows = query_db('''
                SELECT users.username FROM watchlist
                JOIN users ON watchlist.user_id = users.id
                WHERE watchlist.task_id = ? AND watchlist.user_id != ?
            ''', (t['id'], user_id)) or []

            category_rows = query_db('''
                SELECT users.username FROM task_category_members tcm
                JOIN users ON tcm.user_id = users.id
                WHERE tcm.category = ? AND tcm.user_id != ?
                ORDER BY tcm.joined_at DESC
            ''', (t['category'], user_id)) or []

            names, seen = [], set()
            for row in list(watcher_rows) + list(category_rows):
                if row['username'] not in seen:
                    seen.add(row['username'])
                    names.append(row['username'])
            if names:
                suggested_candidates[t['id']] = names[:5]

    # List current user's watchlist
    watchlist = []
    if user_id:
        watchlist = query_db('''
            SELECT tasks.* FROM tasks
            JOIN watchlist ON tasks.id = watchlist.task_id
            WHERE watchlist.user_id = ?
            ORDER BY tasks.created_at DESC
            ''', (user_id,)) or []

    # Incoming direct-ask requests addressed to ME, awaiting my response.
    # These are privatemonly the recipient sees them here, not the
    # whole board unlike simply broadcasting every ask publicly.
    incoming_requests = []
    if user_id:
        incoming_requests = query_db('''
            SELECT task_requests.id as request_id,
                   task_requests.created_at as requested_at,
                   tasks.id as task_id, tasks.title, tasks.description, tasks.category,
                   users.username as from_username, tbm.title as from_user_title,
                   tcm.title as from_user_category_title
            FROM task_requests
            JOIN tasks ON task_requests.task_id = tasks.id
            JOIN users ON task_requests.from_user_id = users.id
            LEFT JOIN task_board_members tbm ON task_requests.from_user_id = tbm.user_id
            LEFT JOIN task_category_members tcm
                   ON tcm.user_id = task_requests.from_user_id AND tcm.category = tasks.category
            WHERE task_requests.to_user_id = ? AND task_requests.status = 'pending'
            ORDER BY task_requests.created_at DESC
            ''', (user_id,)) or []

    # All of the current user's category badges, oldest first, for the
    # identity banner for example Fixer + DIY Crew + Code Wrangler.
    my_category_badges = []
    if user_id:
        my_category_badges = query_db('''
            SELECT category, title FROM task_category_members
            WHERE user_id = ? ORDER BY joined_at ASC
        ''', (user_id,)) or []

    return render_template('tasks.html.j2',
                           tasks=open_tasks,
                           watchlist=watchlist,
                           incoming_requests=incoming_requests,
                           my_pending_task_ids=my_pending_task_ids,
                           community_name=COMMUNITY_NAME,
                           member_title=MEMBER_TITLE,
                           my_membership=my_membership,
                           member_count=member_count,
                           my_category_badges=my_category_badges,
                           suggested_candidates=suggested_candidates)


@app.route('/tasks/new', methods=['POST'])
def create_task():
    """Lets any logged-in user post a new task to the board."""
    init_task_db()
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to post a task.', 'danger')
        return redirect(url_for('login'))

    title = (request.form.get('title') or '').strip()
    description = (request.form.get('description') or '').strip()
    # Title-case so "diy", "DIY", "Diy" all collapse to one category --
    # otherwise each variant would fragment into its own separate badge.
    category = ((request.form.get('category') or 'General').strip() or 'General').title()

    if not title or not description:
        flash('Please fill in both a title and a description for the task.', 'warning')
        return redirect(url_for('task_list'))

    get_or_create_member(user_id)
    db = get_db()
    db.execute('''
        INSERT INTO tasks (title, description, category, status)
        VALUES (?, ?, ?, 'open')
    ''', (title, description, category))
    db.commit()

    # Posting a task is active participation in that domain, so it earns
    # the category-specific badge on top of the platform-wide one.
    cat_member, is_new_cat = get_or_create_category_member(user_id, category)
    if is_new_cat:
        flash(f'🏷️ You\'ve earned the "{cat_member["title"]}" badge for posting a {category} task!', 'success')

    flash('Your task was posted to the board.', 'success')
    return redirect(url_for('task_list'))


@app.route('/tasks/<int:task_id>/watch', methods=['POST'])
def watch_task(task_id):
    """Toggle adding/removing a task from the user's watchlist."""
    init_task_db()
    user_id = session.get('user_id')
    if not user_id:
        flash('Please log in to manage your watchlist.', 'danger')
        return redirect(url_for('login'))

    task = query_db('SELECT 1 FROM tasks WHERE id = ?', (task_id,), one=True)
    if not task:
        flash('That task no longer exists.', 'warning')
        return redirect(url_for('task_list'))

    db = get_db()
    existing = query_db('SELECT 1 FROM watchlist WHERE user_id = ? AND task_id = ?', 
                        (user_id, task_id), one=True)
    
    if existing:
        db.execute('DELETE FROM watchlist WHERE user_id = ? AND task_id = ?', (user_id, task_id))
        flash('Task removed from your watchlist.', 'info')
    else:
        db.execute('INSERT INTO watchlist (user_id, task_id) VALUES (?, ?)', (user_id, task_id))
        flash('Task added to your watchlist!', 'success')
        
    db.commit()
    return redirect(url_for('task_list'))


@app.route('/tasks/<int:task_id>/request', methods=['POST'])
def request_contributor(task_id):
    """Send a PRIVATE direct-ask request to a user. Does not assign the
    task by itself -- the recipient has to accept it first."""
    init_task_db()
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to request contributions.', 'danger')
        return redirect(url_for('login'))

    task = query_db('SELECT * FROM tasks WHERE id = ?', (task_id,), one=True)
    if not task:
        flash('That task no longer exists.', 'warning')
        return redirect(url_for('task_list'))

    if task['status'] != 'open':
        flash('This task is no longer open for requests.', 'warning')
        return redirect(url_for('task_list'))

    target_username = (request.form.get('username') or '').strip()
    target_user = query_db('SELECT id, username FROM users WHERE username = ?', 
                           (target_username,), one=True)
    
    if not target_user:
        flash(f'User "{target_username}" not found.', 'warning')
        return redirect(url_for('task_list'))

    if target_user['id'] == user_id:
        flash("You can't send a direct request to yourself.", 'warning')
        return redirect(url_for('task_list'))

    already_pending = query_db('''
        SELECT 1 FROM task_requests
        WHERE task_id = ? AND to_user_id = ? AND status = 'pending'
    ''', (task_id, target_user['id']), one=True)

    if already_pending:
        flash(f'You already have a pending request to @{target_user["username"]} for this task.', 'info')
        return redirect(url_for('task_list'))

    db = get_db()
    db.execute('''
        INSERT INTO task_requests (task_id, from_user_id, to_user_id, status)
        VALUES (?, ?, ?, 'pending')
    ''', (task_id, user_id, target_user['id']))
    db.commit()

    # Reaching out within a category is active participation in that
    # domain too, so it earns the asker that category's badge.
    get_or_create_member(user_id)
    cat_member, is_new_cat = get_or_create_category_member(user_id, task['category'])
    if is_new_cat:
        flash(f'🏷️ You\'ve earned the "{cat_member["title"]}" badge for getting involved in {task["category"]}!', 'success')

    flash(f'Request sent to @{target_user["username"]} — they can accept or decline it.', 'success')
    return redirect(url_for('task_list'))


@app.route('/tasks/requests/<int:request_id>/respond', methods=['POST'])
def respond_to_request(request_id):
    """The recipient of a direct-ask request accepts or declines it."""
    init_task_db()
    user_id = session.get('user_id')
    if not user_id:
        flash('Please log in.', 'danger')
        return redirect(url_for('login'))

    decision = request.form.get('decision')  # 'accept' or 'decline'
    if decision not in ('accept', 'decline'):
        flash('Invalid response.', 'danger')
        return redirect(url_for('task_list'))

    req = query_db('SELECT * FROM task_requests WHERE id = ?', (request_id,), one=True)
    if not req or req['to_user_id'] != user_id:
        flash('Request not found.', 'danger')
        return redirect(url_for('task_list'))

    if req['status'] != 'pending':
        flash('This request has already been handled.', 'info')
        return redirect(url_for('task_list'))

    db = get_db()

    if decision == 'decline':
        db.execute('''
            UPDATE task_requests SET status = 'declined', responded_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (request_id,))
        db.commit()
        flash('You declined the request.', 'info')
        return redirect(url_for('task_list'))

    # decision == 'accept'
    task = query_db('SELECT * FROM tasks WHERE id = ?', (req['task_id'],), one=True)
    if not task or task['status'] != 'open':
        db.execute('''
            UPDATE task_requests SET status = 'cancelled', responded_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (request_id,))
        db.commit()
        flash('That task is no longer open.', 'warning')
        return redirect(url_for('task_list'))

    db.execute('''
        UPDATE tasks SET assigned_user_id = ?, status = 'assigned' WHERE id = ?
    ''', (user_id, req['task_id']))
    db.execute('''
        UPDATE task_requests SET status = 'accepted', responded_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (request_id,))
    # Any other pending asks for this task are now moot -- close them out
    # so other requesters aren't left waiting on a task that's taken.
    db.execute('''
        UPDATE task_requests SET status = 'cancelled', responded_at = CURRENT_TIMESTAMP
        WHERE task_id = ? AND status = 'pending' AND id != ?
    ''', (req['task_id'], request_id))
    db.commit()

    # Actually doing a task in a category is the clearest form of
    # participation in that domain, so it earns the accepter that
    # category's badge on top of their platform-wide "Fixer" badge.
    get_or_create_member(user_id)
    cat_member, is_new_cat = get_or_create_category_member(user_id, task['category'])
    if is_new_cat:
        flash(f'🏷️ You\'ve earned the "{cat_member["title"]}" badge for taking on a {task["category"]} task!', 'success')

    flash('You accepted the task — it now shows as assigned to you.', 'success')
    return redirect(url_for('task_list'))


@app.route('/chat')
def offtopic_chat():
    """Dedicated off-topic chatter page (Design Claim 25), separate from
    the task board since this chatter was never really task-specific.

    Personalized filtering still governs it: each person's own
    show_offtopic preference decides whether messages are shown to them
    here -- nothing is ever removed for anyone, only each viewer's
    exposure changes.
    """
    init_task_db()
    user_id = session.get('user_id')

    my_membership = None
    if user_id:
        my_membership, is_new_member = get_or_create_member(user_id)
        if is_new_member:
            flash(f'🎉 Welcome to {COMMUNITY_NAME}! You\'re now a "{MEMBER_TITLE}".', 'success')

    show_offtopic = bool(my_membership['show_offtopic']) if my_membership else False
    offtopic_messages = []
    if user_id and show_offtopic:
        offtopic_messages = query_db('''
            SELECT task_board_chatter.id, task_board_chatter.content,
                   task_board_chatter.created_at,
                   users.username, tbm.title as user_title
            FROM task_board_chatter
            JOIN users ON task_board_chatter.user_id = users.id
            LEFT JOIN task_board_members tbm ON task_board_chatter.user_id = tbm.user_id
            ORDER BY task_board_chatter.created_at DESC
            LIMIT 50
        ''') or []

    return render_template('chat.html.j2',
                           community_name=COMMUNITY_NAME,
                           my_membership=my_membership,
                           show_offtopic=show_offtopic,
                           offtopic_messages=offtopic_messages)


@app.route('/chat/post', methods=['POST'])
def post_offtopic():
    """Post an off-topic message to the community chatter feed.

    Design Claim 25: letting people go off-topic together is meant to
    build bonds-based commitment (they get to know each other, not just
    the tasks or posts) alongside the site's main purpose.
    """
    init_task_db()
    user_id = session.get('user_id')
    if not user_id:
        flash('Please log in to chat.', 'danger')
        return redirect(url_for('login'))

    content = (request.form.get('content') or '').strip()
    if not content:
        flash('Say something first!', 'warning')
        return redirect(url_for('offtopic_chat'))

    get_or_create_member(user_id)  # chatting is community participation too
    db = get_db()
    db.execute('INSERT INTO task_board_chatter (user_id, content) VALUES (?, ?)',
              (user_id, content))
    db.commit()
    return redirect(url_for('offtopic_chat'))


@app.route('/chat/toggle', methods=['POST'])
def toggle_offtopic_visibility():
    """Personalized filtering (Design Claim 25): each person decides for
    themselves whether to see off-topic chatter, rather than the site
    moderating it away for everyone, or forcing it on everyone regardless
    of interest. The messages always exist either way -- only each
    viewer's exposure to them changes.
    """
    init_task_db()
    user_id = session.get('user_id')
    if not user_id:
        flash('Please log in.', 'danger')
        return redirect(url_for('login'))

    member, _ = get_or_create_member(user_id)
    new_value = 0 if member['show_offtopic'] else 1
    db = get_db()
    db.execute('UPDATE task_board_members SET show_offtopic = ? WHERE user_id = ?',
              (new_value, user_id))
    db.commit()
    return redirect(url_for('offtopic_chat'))


@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Pass the user's content through the moderation function
    moderated_content = content

    # Basic validation to ensure post is not empty
    if moderated_content and moderated_content.strip():
        db = get_db()
        db.execute('INSERT INTO posts (user_id, content) VALUES (?, ?)',
                   (user_id, moderated_content))
        db.commit()
        flash('Your post was successfully created!', 'success')
    else:
        # This will catch empty posts or posts that were fully censored
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

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    posts_raw = query_db('SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
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
                           is_following=is_currently_following)
    

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
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
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

        # Design Claim 4 (Ch.5): who invited this visitor? (set by capture_referral)
        ref_user_id = session.get('ref_user_id')
        ref_post_id = session.get('ref_post_id')

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

            # Credit the member whose invite/shared post brought this person in.
            inviter_name = None
            if ref_user_id and ref_user_id != new_user_id:
                init_growth_db()
                cur.execute('INSERT OR IGNORE INTO referrals (inviter_id, invitee_id, post_id) VALUES (?, ?, ?)',
                            (ref_user_id, new_user_id, ref_post_id))
                db.commit()
                inviter_row = cur.execute('SELECT username FROM users WHERE id = ?', (ref_user_id,)).fetchone()
                inviter_name = inviter_row['username'] if inviter_row else None

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            welcome = f'Welcome, {username}! Your account has been created.'
            if inviter_name:
                welcome += f' You joined through @{inviter_name}\'s invitation.'
            flash(welcome, 'success')
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
        db = get_db()
        db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        db.commit()
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
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

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
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

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
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        author_created_dt = post_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC -- Order by ID for consistent pagination before risk sort
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

    comments.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring


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
    
    user = query_db('SELECT profile, created_at FROM users WHERE id = ?', (user_id,), one=True)
    if not user:
        return 0.0

    def average_content_score(rows):
        # Average Content Score over the rows, or 0 when there are none
        if not rows:
            return 0.0
        return sum(moderate_content(r['content'])[1] for r in rows) / len(rows)

    # Steps 1-3: profile score, average post score, average comment score
    profile_score = moderate_content(user['profile'])[1]
    average_post_score = average_content_score(
        query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,)) or [])
    average_comment_score = average_content_score(
        query_db('SELECT content FROM comments WHERE user_id = ?', (user_id,)) or [])

    # Step 4: combine (posts weigh most)
    content_risk_score = (profile_score * 1) + (average_post_score * 3) + (average_comment_score * 1)

    # Step 5: newer accounts are riskier
    created = user['created_at']
    if isinstance(created, str):
        created = datetime.strptime(created[:19], '%Y-%m-%d %H:%M:%S')
    account_age_days = (datetime.utcnow() - created).days
    if account_age_days < 7:
        user_risk_score = content_risk_score * 1.5
    elif account_age_days < 30:
        user_risk_score = content_risk_score * 1.2
    else:
        user_risk_score = content_risk_score

    # Step 6: cap at 5.0
    return min(5.0, user_risk_score)

    
# URL detection for Rule 1.2.2. Matches (a) links with a scheme or "www." and
# (b) bare domains such as example.com, including "defanged" ones scammers use to dodge
# filters, e.g. example[.]com, example(.)com or example[dot]com. Bare domains are limited
# to common TLDs, must not be part of an e-mail address, and the TLD must end the word, so
# ordinary text like "e.g.", "3.5" or "file.txt" is not flagged.
_LINK_TLDS = 'com|net|org|info|biz|io|xyz|club|online|site|shop|link|click|vip|store|tech|top|ru|cn|tk|ml|ga|cf'
_LINK_DOT = r'(?:\.|\[\.\]|\(\.\)|\[dot\]|\(dot\))'
LINK_PATTERN = re.compile(
    r'(?:https?://|www\.)[^\s<>"\']+'
    r'|(?<![\w@.\-/])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?' + _LINK_DOT + r')+(?:' + _LINK_TLDS + r')'
    r'(?![a-z0-9@-])(?:/[^\s<>"\']*)?',
    re.IGNORECASE)

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

    content = content or ''

    def whole(term):
        # case-insensitive WHOLE word/phrase match (works even if the term starts/ends with punctuation)
        return re.compile(r'(?<!\w)' + re.escape(term) + r'(?!\w)', re.IGNORECASE)

    # ---- Stage 1.1: severe violations -> remove everything, maximum score ----
    # Rule 1.1.1: Tier 1 words
    for word in TIER1_WORDS:
        if whole(word).search(content):
            return '[content removed due to severe violation]', 5.0
    # Rule 1.1.2: Tier 2 phrases
    for phrase in TIER2_PHRASES:
        if whole(phrase).search(content):
            return '[content removed due to spam/scam policy]', 5.0

    # ---- Stage 1.2: scored violations & filtering ----
    moderated = content
    score = 0.0

    # Rule 1.2.1: Tier 3 words -> asterisks of the same length, +2.0 per match
    for word in TIER3_WORDS:
        moderated, count = whole(word).subn(lambda m: '*' * len(m.group(0)), moderated)
        score += 2.0 * count

    # Rule 1.2.2: external links -> "[link removed]", +2.0 per link
    def strip_link(match):
        nonlocal score
        score += 2.0
        url = match.group(0)
        trailing = url[len(url.rstrip('.,!?;:)]}')):]   # keep sentence punctuation after the link
        return '[link removed]' + trailing
    moderated = LINK_PATTERN.sub(strip_link, moderated)

    # Rule 1.2.3: excessive capitalization -> +0.5, content NOT modified
    letters = [c for c in content if c.isalpha()]
    if len(letters) > 15 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        score += 0.5

    return moderated, score

# Coding Assignment #3
# Assignment 3.1
def recommend(user_id, filter_following):
    """
    Args:
        user_id: The ID of the current user.
        filter_following: Boolean, True if we only want to see recommendations from followed users.

    Returns:
        A list of 5 recommended posts, in reverse-chronological order.

    Algorithm: simple content-based filtering (TF-IDF + cosine similarity).
      1. Build an "interest profile" from words in (a) posts the user reacted to
         positively (like/love/laugh/wow) at full weight and (b) posts written by
         users they follow at half weight.
      2. Score every candidate post (not own, not already reacted to) by cosine
         similarity to that profile, plus a small bonus when its author is someone
         the user follows or has positively reacted to.
      3. Take the top 5 and return them newest-first.
      4. Cold start (new/anonymous user, no signals): fall back to the most
         reacted-to posts so newcomers still see something useful.

    To test whether your recommendation algorithm works, let's pretend we like the DIY topic. Here are some users that often post DIY comment and a few example posts. Make sure your account did not engage with anything else. You should test your algorithm with these and see if your recommendation algorithm picks up on your interest in DIY and starts showing related content.
    
    Users: @starboy99, @DancingDolphin, @blogger_bob
    Posts: 1810, 1875, 1880, 2113
    
    Materials: 
    - https://www.nvidia.com/en-us/glossary/recommendation-system/
    - http://www.configworks.com/mz/handout_recsys_sac2010.pdf
    - https://www.researchgate.net/publication/227268858_Recommender_Systems_Handbook
    """
    import math

    NUM_RECOMMENDATIONS = 5
    POSITIVE_REACTIONS = ('like', 'love', 'laugh', 'wow')  # sad/angry are not interest signals
    FOLLOWED_POST_WEIGHT = 0.5   # following someone is a weaker signal than reacting to a post
    AUTHOR_BONUS = 0.1           # small nudge toward authors the user already likes
    STOPWORDS = {
        'the', 'a', 'an', 'and', 'or', 'but', 'is', 'are', 'was', 'were', 'be', 'to', 'of',
        'in', 'on', 'at', 'for', 'with', 'it', 'its', 'this', 'that', 'i', 'you', 'my', 'me',
        'we', 'so', 'just', 'have', 'has', 'had', 'do', 'did', 'not', 'no', 'as', 'by', 'from',
        'up', 'out', 'if', 'about', 'what', 'all', 'can', 'will', 'your', 'im', 'now', 'get',
    }

    def tokenize(text):
        # lowercase words of 3+ letters, minus stopwords
        return [w for w in re.findall(r"[a-z']+", (text or '').lower())
                if len(w) > 2 and w not in STOPWORDS]

    def popular_posts(exclude_ids, followed_ids, limit):
        # Fallback ranking: most reactions first (newest breaks ties)
        rows = query_db('''
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   (SELECT COUNT(*) FROM reactions r WHERE r.post_id = p.id) as total_reactions
            FROM posts p JOIN users u ON p.user_id = u.id
            ORDER BY total_reactions DESC, p.created_at DESC
        ''') or []
        picked = []
        for row in rows:
            if row['id'] in exclude_ids or row['user_id'] == user_id:
                continue
            if filter_following and row['user_id'] not in followed_ids:
                continue
            picked.append(dict(row))
            if len(picked) == limit:
                break
        return picked

    # ---- Gather the user's signals -------------------------------------
    followed_ids, liked_post_ids, liked_author_ids = set(), set(), set()
    reacted_post_ids = set()
    if user_id:
        followed_ids = {r['followed_id'] for r in
                        (query_db('SELECT followed_id FROM follows WHERE follower_id = ?', (user_id,)) or [])}
        for r in (query_db('''
                SELECT r.post_id, r.reaction_type, p.user_id as author_id
                FROM reactions r JOIN posts p ON r.post_id = p.id
                WHERE r.user_id = ?''', (user_id,)) or []):
            reacted_post_ids.add(r['post_id'])
            if r['reaction_type'] in POSITIVE_REACTIONS:
                liked_post_ids.add(r['post_id'])
                liked_author_ids.add(r['author_id'])

    all_posts = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
        FROM posts p JOIN users u ON p.user_id = u.id
    ''') or []

    # Cold start: nothing to learn from yet
    if not user_id or not (liked_post_ids or followed_ids):
        result = popular_posts(reacted_post_ids, followed_ids, NUM_RECOMMENDATIONS)
        return sorted(result, key=lambda p: p['created_at'], reverse=True)

    # ---- TF-IDF over the whole corpus ----------------------------------
    tokens_by_post = {p['id']: tokenize(p['content']) for p in all_posts}
    doc_freq = collections.Counter()
    for toks in tokens_by_post.values():
        doc_freq.update(set(toks))
    n_docs = len(all_posts)

    def vectorize(toks):
        tf = collections.Counter(toks)
        return {w: c * math.log((1 + n_docs) / (1 + doc_freq[w])) for w, c in tf.items()}

    # ---- Interest profile ----------------------------------------------
    profile = collections.defaultdict(float)
    for p in all_posts:
        if p['id'] in liked_post_ids:
            weight = 1.0
        elif p['user_id'] in followed_ids:
            weight = FOLLOWED_POST_WEIGHT
        else:
            continue
        for w, v in vectorize(tokens_by_post[p['id']]).items():
            profile[w] += weight * v
    profile_norm = math.sqrt(sum(v * v for v in profile.values()))

    # ---- Score candidates ----------------------------------------------
    scored = []
    for p in all_posts:
        if p['user_id'] == user_id or p['id'] in reacted_post_ids:
            continue  # don't recommend own posts or ones already reacted to
        if filter_following and p['user_id'] not in followed_ids:
            continue
        vec = vectorize(tokens_by_post[p['id']])
        vec_norm = math.sqrt(sum(v * v for v in vec.values()))
        similarity = 0.0
        if profile_norm and vec_norm:
            similarity = sum(profile.get(w, 0.0) * v for w, v in vec.items()) / (profile_norm * vec_norm)
        if similarity <= 0:
            continue  # no topical overlap at all
        if p['user_id'] in followed_ids or p['user_id'] in liked_author_ids:
            similarity += AUTHOR_BONUS
        scored.append((similarity, p['created_at'], dict(p)))

    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    result = [s[2] for s in scored[:NUM_RECOMMENDATIONS]]

    # Not enough relevant posts? Top up with popular ones so we still return 5.
    if len(result) < NUM_RECOMMENDATIONS:
        taken = reacted_post_ids | {p['id'] for p in result}
        result += popular_posts(taken, followed_ids, NUM_RECOMMENDATIONS - len(result))

    return sorted(result, key=lambda p: p['created_at'], reverse=True)


# CHAPTER 5 (DEALING WITH NEWCOMERS): VIRAL SHARING + INFLUENCER RECRUITING

# Design Claim 4 (Ch.5): making it easy to share community content with
# friends (email / Twitter / Facebook / copy-link) raises the community's
# visibility among members' friends and so the chance they join.
#  

INFLUENCER_COUNT = 10           # how many "top connectors" we identify
INFLUENCE_PROBABILITY = 0.15    # chance a follower acts on what a followed user shares
INFLUENCE_SAMPLES = 100         # Monte-Carlo samples of the cascade
INFLUENCE_POOL_SIZE = 40        # greedy only considers the 40 best-connected members
INFLUENCE_CACHE_SECONDS = 300   # recompute at most every 5 minutes
SHARE_CHANNELS = ('email', 'twitter', 'facebook', 'whatsapp', 'instagram')

_influence_cache = {'computed_at': 0.0, 'result': None}


def init_growth_db():
    """Tables for referral tracking (Claim 4) and share analytics."""
    db = get_db()
    db.execute('''
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inviter_id INTEGER NOT NULL,
            invitee_id INTEGER NOT NULL UNIQUE,   -- a member is recruited by at most one person
            post_id INTEGER,                      -- the shared post that brought them in (if any)
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (inviter_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (invitee_id) REFERENCES users (id) ON DELETE CASCADE
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS share_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,                      -- NULL when an anonymous visitor shares
            post_id INTEGER,                      -- NULL for a general invitation
            channel TEXT NOT NULL,                -- email / twitter / facebook
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL
        )
    ''')
    db.commit()


@app.before_request
def capture_referral():
    """Remember who invited a not-yet-registered visitor.

    Any URL carrying ?ref=<username> (shared post links, invite links) stores
    the inviter in the session, so the credit survives until the visitor signs up.
    First click wins; logged-in members are ignored.
    """
    ref_username = request.args.get('ref')
    if not ref_username or session.get('user_id') or 'ref_user_id' in session:
        return
    inviter = query_db('SELECT id, username FROM users WHERE username = ?',
                       (ref_username,), one=True)
    if inviter:
        session['ref_user_id'] = inviter['id']
        if request.endpoint == 'post_detail' and request.view_args:
            session['ref_post_id'] = request.view_args.get('post_id')
        flash(f'@{inviter["username"]} invited you to join Mini Social. '
              f'Sign up to take part!', 'info')


# ---------------------------------------------------------------------
# Claim 4: sharing
# ---------------------------------------------------------------------
def build_share_url(channel, link, text):
    """Builds the outgoing share URL for a channel (email/twitter/facebook)."""
    if channel == 'twitter':
        return 'https://twitter.com/intent/tweet?text=%s&url=%s' % (quote(text), quote(link, safe=''))
    if channel == 'facebook':
        return 'https://www.facebook.com/sharer/sharer.php?u=%s' % quote(link, safe='')
    if channel == 'whatsapp':
        return 'https://wa.me/?text=%s' % quote(text + '\n' + link)
    if channel == 'instagram':
        # Instagram has no web "share this link" URL, so the page copies the link to the
        # clipboard (see the template) and we just open Instagram to paste it into a
        # story, DM or bio.
        return 'https://www.instagram.com/'
    # email
    return 'mailto:?subject=%s&body=%s' % (quote('Join me on Mini Social'),
                                           quote(text + '\n\n' + link))


def log_share(channel, post_id=None):
    init_growth_db()
    db = get_db()
    db.execute('INSERT INTO share_events (user_id, post_id, channel) VALUES (?, ?, ?)',
               (session.get('user_id'), post_id, channel))
    db.commit()


@app.route('/share/post/<int:post_id>/<channel>')
def share_post(post_id, channel):
    """Logs the share, then sends the member to the chosen channel with a
    link that carries their username as ?ref= so new signups can be credited."""
    if channel not in SHARE_CHANNELS:
        flash('Unknown share channel.', 'warning')
        return redirect(url_for('feed'))
    post = query_db('SELECT content FROM posts WHERE id = ?', (post_id,), one=True)
    if not post:
        flash('That post no longer exists.', 'warning')
        return redirect(url_for('feed'))

    link = url_for('post_detail', post_id=post_id, ref=session.get('username'), _external=True)
    snippet, _ = moderate_content(post['content'])   # never share unmoderated text
    snippet = snippet if len(snippet) <= 100 else snippet[:97] + '...'
    log_share(channel, post_id)
    return redirect(build_share_url(channel, link, f'Check this out on Mini Social: "{snippet}"'))


@app.route('/share/invite/<channel>')
def share_invite(channel):
    """Same as share_post, but for a general invitation to the community."""
    if channel not in SHARE_CHANNELS:
        flash('Unknown share channel.', 'warning')
        return redirect(url_for('invite'))
    if not session.get('user_id'):
        flash('Please log in to invite friends.', 'danger')
        return redirect(url_for('login'))

    link = url_for('signup', ref=session.get('username'), _external=True)
    log_share(channel)
    return redirect(build_share_url(channel, link,
                                    "I'm on Mini Social and thought you'd enjoy it. Come join the community!"))


@app.route('/invite')
def invite():
    """Personal invite page: invite link, one-click share buttons, and the
    member's recruitment stats (friends who joined through them)."""
    user_id = session.get('user_id')
    if not user_id:
        flash('Please log in to invite friends.', 'danger')
        return redirect(url_for('login'))
    init_growth_db()
    username = session.get('username')

    recruits = query_db('''
        SELECT u.username, r.created_at, r.post_id
        FROM referrals r JOIN users u ON r.invitee_id = u.id
        WHERE r.inviter_id = ? ORDER BY r.created_at DESC
    ''', (user_id,)) or []
    shares = query_db('SELECT COUNT(*) as c FROM share_events WHERE user_id = ?',
                      (user_id,), one=True)
    analysis = get_influencer_analysis()
    my_influence = next((i for i in analysis['influencers'] if i['user_id'] == user_id), None)

    return render_template('invite.html.j2',
                           invite_link=url_for('signup', ref=username, _external=True),
                           recruits=recruits,
                           share_count=shares['c'] if shares else 0,
                           my_influence=my_influence)

# Coding assignment 3
# Claim 5: finding the most influential members

def build_influence_graph():
    """Directed graph from the follow relationships.

    Influence flows from a followed user to their followers (followers see
    what the user posts/shares), so adjacency[author] = [their followers].
    """
    names = {u['id']: u['username'] for u in (query_db('SELECT id, username FROM users') or [])}
    adjacency = {uid: [] for uid in names}
    for row in (query_db('SELECT follower_id, followed_id FROM follows') or []):
        if row['followed_id'] in adjacency and row['follower_id'] in names:
            adjacency[row['followed_id']].append(row['follower_id'])
    return names, adjacency


def sample_live_edge_graphs(adjacency, probability, runs, seed=2026):
    """Independent-cascade model, sampled up front ("live-edge" snapshots).

    In each sample every follow edge is independently kept with the given
    probability. Whoever is reachable from the seed members in a snapshot is
    someone the message would reach. Averaging over many snapshots estimates
    each member's expected reach (Kempe, Kleinberg & Tardos 2003).
    """
    rng = random.Random(seed)   # fixed seed -> stable, reproducible rankings
    return [{u: [v for v in nbrs if rng.random() < probability]
             for u, nbrs in adjacency.items()} for _ in range(runs)]


def _reach(graph, start, blocked):
    """Members newly reachable from `start` (DFS), not walking into `blocked`
    members who are already covered by earlier seeds."""
    if start in blocked:
        return set()
    seen, stack = {start}, [start]
    while stack:
        u = stack.pop()
        for v in graph.get(u, ()):
            if v not in seen and v not in blocked:
                seen.add(v)
                stack.append(v)
    return seen


def expected_spread(graphs, seeds):
    """Average number of members reached (seeds included) by this seed set."""
    total = 0
    for g in graphs:
        covered = set()
        for s in seeds:
            covered |= _reach(g, s, covered)
        total += len(covered)
    return total / len(graphs)


def greedy_influencers(adjacency, graphs, k, pool_size=INFLUENCE_POOL_SIZE):
    """Greedy influence maximization with lazy evaluation (CELF).

    Repeatedly adds the member whose MARGINAL gain in expected reach is
    largest, given the members already chosen. Returns [(user_id, gain), ...].
    Because expected reach is submodular, greedy is provably within ~63% of
    the optimal seed set (Kempe et al. 2003).
    """
    pool = sorted(adjacency, key=lambda u: len(adjacency[u]), reverse=True)[:pool_size]
    pool = [u for u in pool if adjacency[u]]          # must have at least one follower
    covered = [set() for _ in graphs]

    def gain(u):
        return sum(len(_reach(g, u, covered[i])) for i, g in enumerate(graphs)) / len(graphs)

    heap = [(-gain(u), u, 0) for u in pool]            # (-gain, member, round it was computed in)
    heapq.heapify(heap)
    chosen = []
    while heap and len(chosen) < k:
        neg_gain, u, computed_round = heapq.heappop(heap)
        if computed_round == len(chosen):              # gain is up to date -> it's the best
            chosen.append((u, -neg_gain))
            for i, g in enumerate(graphs):
                covered[i] |= _reach(g, u, covered[i])
        else:                                          # stale -> recompute and put back
            heapq.heappush(heap, (-gain(u), u, len(chosen)))
    return chosen


def get_influencer_analysis(force=False):
    """The most influential members, plus how they compare with simply
    asking the most-followed members or asking members at random.
    Cached for a few minutes because the simulation is not free."""
    now = time.time()
    if (not force and _influence_cache['result'] is not None
            and now - _influence_cache['computed_at'] < INFLUENCE_CACHE_SECONDS):
        return _influence_cache['result']

    names, adjacency = build_influence_graph()
    graphs = sample_live_edge_graphs(adjacency, INFLUENCE_PROBABILITY, INFLUENCE_SAMPLES)
    chosen = greedy_influencers(adjacency, graphs, INFLUENCER_COUNT)
    ids = [u for u, _ in chosen]
    k = len(ids)

    by_followers = sorted(adjacency, key=lambda u: len(adjacency[u]), reverse=True)[:k]
    rng = random.Random(7)
    random_runs = [expected_spread(graphs, rng.sample(list(names), k)) for _ in range(30)] if k else [0]

    result = {
        'influencers': [{'rank': i + 1, 'user_id': u, 'username': names[u],
                         'followers': len(adjacency[u]), 'marginal_reach': round(gain, 2)}
                        for i, (u, gain) in enumerate(chosen)],
        'ids': set(ids),
        'k': k,
        'members': len(names),
        'edges': sum(len(v) for v in adjacency.values()),
        'greedy_spread': round(expected_spread(graphs, ids), 2) if k else 0,
        'degree_spread': round(expected_spread(graphs, by_followers), 2) if k else 0,
        'random_spread': round(sum(random_runs) / len(random_runs), 2),
    }
    _influence_cache.update(computed_at=now, result=result)
    return result


@app.route('/influencers')
def influencers():
    """Shows who the community's top connectors are, how much more reach they
    give than random members, and how well influencers actually recruit."""
    init_growth_db()
    analysis = get_influencer_analysis()

    recruits = {r['inviter_id']: r['c'] for r in
                (query_db('SELECT inviter_id, COUNT(*) as c FROM referrals GROUP BY inviter_id') or [])}
    inf_ids = analysis['ids']
    others = analysis['members'] - len(inf_ids)
    inf_total = sum(recruits.get(i, 0) for i in inf_ids)
    other_total = sum(c for uid, c in recruits.items() if uid not in inf_ids)
    stats = {
        'influencer_total': inf_total,
        'other_total': other_total,
        'influencer_avg': round(inf_total / len(inf_ids), 2) if inf_ids else 0,
        'other_avg': round(other_total / others, 2) if others > 0 else 0,
    }
    return render_template('influencers.html.j2', analysis=analysis, stats=stats,
                           probability=INFLUENCE_PROBABILITY)


if __name__ == '__main__':
    app.run(debug=True, port=8080)

