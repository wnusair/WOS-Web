from flask import Flask, render_template, request, redirect, url_for, send_from_directory, abort, flash
import os
from werkzeug.utils import secure_filename
import zipfile
from flask_socketio import SocketIO, join_room
import threading
import shutil
import sqlite3

app = Flask(__name__)
app.secret_key = 'you-chose'  # Needed for flashing messages

# Initialize SocketIO with eventlet for asynchronous operation
socketio = SocketIO(app, async_mode='eventlet')

UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Database setup
DATABASE = 'folders.db'

# Ensure the upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def init_db():
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_name TEXT NOT NULL,
            folder_path TEXT NOT NULL UNIQUE,
            password TEXT
        )''')
        conn.commit()

init_db()


def get_file_list(current_path):
    items = []
    with os.scandir(current_path) as it:
        for entry in it:
            items.append({
                'name': entry.name,
                'is_file': entry.is_file(),
                'is_dir': entry.is_dir()
            })
    items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
    return items

def is_safe_path(basedir, path, follow_symlinks=True):
    # Resolve symbolic links and check if the path is within the basedir
    if follow_symlinks:
        return os.path.realpath(path).startswith(os.path.realpath(basedir))
    return os.path.abspath(path).startswith(os.path.abspath(basedir))

def safe_extract(zip_ref, path, sid):
    try:
        total_files = len(zip_ref.namelist())
        extracted_files = 0
        for member in zip_ref.namelist():
            member_path = os.path.join(path, member)
            if not is_safe_path(path, member_path):
                raise Exception("Attempted Path Traversal in Zip File")
            zip_ref.extract(member, path)
            extracted_files += 1
            progress = (extracted_files / total_files) * 100
            # Emit progress update to the specific client
            socketio.emit('unzip_progress', {'progress': progress}, room=sid)
        socketio.emit('unzip_complete', room=sid)
    except Exception as e:
        socketio.emit('unzip_error', {'error': str(e)}, room=sid)
        raise

def extract_zip_in_background(zip_path, extract_to, sid):
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            safe_extract(zip_ref, extract_to, sid)
    except Exception as e:
        socketio.emit('unzip_error', {'error': str(e)}, room=sid)

@app.route('/upload', methods=['GET', 'POST'])
@app.route('/upload/<path:req_path>', methods=['GET', 'POST'])
def upload_file(req_path=''):
    current_path = os.path.join(app.config['UPLOAD_FOLDER'], req_path)
    if not is_safe_path(app.config['UPLOAD_FOLDER'], current_path):
        abort(400, 'Unsafe path detected')

    if request.method == 'POST':
        sid = request.form.get('sid')  # Get the socket id
        if 'file' not in request.files:
            return 'No file part', 400
        file = request.files['file']
        if file.filename == '':
            return 'No selected file', 400
        filename = secure_filename(file.filename)
        file_path = os.path.join(current_path, filename)
        file.save(file_path)

        if filename.lower().endswith('.zip'):
            # Create a folder with the same name as the zip file (without .zip extension)
            folder_name = os.path.splitext(filename)[0]
            folder_path = os.path.join(current_path, folder_name)
            os.makedirs(folder_path, exist_ok=True)
            try:
                # Start unzipping in a background thread
                threading.Thread(target=extract_zip_in_background, args=(file_path, folder_path, sid)).start()
                return 'Zip file uploaded and extraction started', 200
            except Exception as e:
                return f'An error occurred while starting the extraction: {e}', 500
        else:
            return 'File uploaded successfully', 200
    return render_template('upload.html', current_path=req_path)


@app.route('/new_folder', methods=['GET', 'POST'])
@app.route('/new_folder/<path:req_path>', methods=['GET', 'POST'])
def new_folder(req_path=''):
    current_path = os.path.join(app.config['UPLOAD_FOLDER'], req_path)
    if not is_safe_path(app.config['UPLOAD_FOLDER'], current_path):
        abort(400, 'Unsafe path detected')

    if request.method == 'POST':
        folder_name = request.form['folder_name']
        password = request.form.get('password')  # Password can be optional
        new_folder_path = os.path.join(current_path, secure_filename(folder_name))
        os.makedirs(new_folder_path, exist_ok=True)

        # Save folder with password to database
        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO folders (folder_name, folder_path, password) VALUES (?, ?, ?)',
                           (folder_name, new_folder_path, password))
            conn.commit()

        flash('Folder created successfully')
        return redirect(url_for('index', req_path=req_path))
    return render_template('new_folder.html', current_path=req_path)

@app.route('/move/<path:req_path>', methods=['GET', 'POST'])
def move(req_path):
    source_path = os.path.join(app.config['UPLOAD_FOLDER'], req_path)
    if not is_safe_path(app.config['UPLOAD_FOLDER'], source_path):
        abort(400, 'Unsafe source path detected')
    if not os.path.exists(source_path):
        abort(404)

    if request.method == 'POST':
        dest_relative_path = request.form['destination']
        dest_path = os.path.join(app.config['UPLOAD_FOLDER'], dest_relative_path)
        if not is_safe_path(app.config['UPLOAD_FOLDER'], dest_path):
            abort(400, 'Unsafe destination path detected')
        if not os.path.isdir(dest_path):
            flash('Destination does not exist or is not a directory')
            return redirect(request.url)
        dest_file_path = os.path.join(dest_path, os.path.basename(source_path))
        try:
            shutil.move(source_path, dest_file_path)  # Move directory or file
            flash('Moved successfully')
        except Exception as e:
            flash(f'Error moving file: {e}')
        parent_path = '/'.join(req_path.strip('/').split('/')[:-1])
        return redirect(url_for('index', req_path=parent_path))

    # On GET, show form to select destination
    all_dirs = []
    for root, dirs, files in os.walk(app.config['UPLOAD_FOLDER']):
        for dir_name in dirs:
            dir_path = os.path.relpath(os.path.join(root, dir_name), app.config['UPLOAD_FOLDER'])
            all_dirs.append(dir_path)
    all_dirs.sort()
    return render_template('move.html', req_path=req_path, all_dirs=all_dirs)

@app.route('/delete/<path:req_path>', methods=['POST'])
def delete(req_path):
    path = os.path.join(app.config['UPLOAD_FOLDER'], req_path)
    if not is_safe_path(app.config['UPLOAD_FOLDER'], path):
        abort(400, 'Unsafe path detected')

    try:
        if os.path.isdir(path):
            shutil.rmtree(path)  # Recursively delete directory and contents
        elif os.path.isfile(path):
            os.remove(path)
        else:
            abort(404)
        flash('Deleted successfully')
    except Exception as e:
        flash(f'Error deleting file: {e}')
    parent_path = '/'.join(req_path.strip('/').split('/')[:-1])
    return redirect(url_for('index', req_path=parent_path))

@app.route('/preview/<path:req_path>')
def preview_file(req_path):
    path = os.path.join(app.config['UPLOAD_FOLDER'], req_path)
    if not is_safe_path(app.config['UPLOAD_FOLDER'], path):
        abort(400, 'Unsafe path detected')

    if os.path.exists(path):
        return send_from_directory(app.config['UPLOAD_FOLDER'], req_path)
    else:
        abort(404)

@app.route('/rename/<path:req_path>', methods=['GET', 'POST'])
def rename(req_path):
    old_path = os.path.join(app.config['UPLOAD_FOLDER'], req_path)
    if not is_safe_path(app.config['UPLOAD_FOLDER'], old_path):
        abort(400, 'Unsafe path detected')

    if request.method == 'POST':
        new_name = request.form['new_name']
        parent_dir = os.path.dirname(old_path)
        new_path = os.path.join(parent_dir, secure_filename(new_name))
        if not is_safe_path(app.config['UPLOAD_FOLDER'], new_path):
            abort(400, 'Unsafe new path detected')
        try:
            os.rename(old_path, new_path)
            flash('Renamed successfully')
        except Exception as e:
            flash(f'Error renaming file: {e}')
        parent_path = '/'.join(req_path.strip('/').split('/')[:-1])
        return redirect(url_for('index', req_path=parent_path))
    return render_template('rename.html', current_name=os.path.basename(req_path), req_path=req_path)

@app.route('/search', methods=['GET'])
def search():
    query = request.args.get('q', '')
    matches = []
    for root, dirs, files in os.walk(app.config['UPLOAD_FOLDER']):
        for name in files + dirs:
            if query.lower() in name.lower():
                rel_dir = os.path.relpath(root, app.config['UPLOAD_FOLDER'])
                rel_file = os.path.join(rel_dir, name)
                matches.append(rel_file)
    matches.sort()
    return render_template('search_results.html', matches=matches, query=query)

@app.route('/', defaults={'req_path': ''})
@app.route('/<path:req_path>')
def index(req_path):
    current_path = os.path.join(app.config['UPLOAD_FOLDER'], req_path)
    if not is_safe_path(app.config['UPLOAD_FOLDER'], current_path):
        abort(400, 'Unsafe path detected')
    if not os.path.exists(current_path):
        return abort(404)
    if os.path.isfile(current_path):
        return send_from_directory(app.config['UPLOAD_FOLDER'], req_path)
    files = get_file_list(current_path)
    return render_template('index.html', files=files, current_path=req_path)

@app.route('/lock', methods=['POST'])
def lock_folder():
    folder_path = request.form['folder_path']
    password = request.form['password']

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE folders SET password = ? WHERE folder_path = ?', (password, folder_path))
        conn.commit()

    flash('Folder locked successfully')
    return redirect(url_for('index'))

@app.route('/unlock', methods=['POST'])
def unlock_folder():
    folder_path = request.form['folder_path']
    input_password = request.form['password']

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT password FROM folders WHERE folder_path = ?', (folder_path,))
        result = cursor.fetchone()

    if result and result[0] == input_password:
        cursor.execute('UPDATE folders SET password = NULL WHERE folder_path = ?', (folder_path,))
        conn.commit()
        flash('Folder unlocked successfully')
    else:
        flash('Incorrect password')

    return redirect(url_for('index'))

@socketio.on('join')
def handle_join(data):
    sid = data['sid']
    join_room(sid)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=8000, debug=True)
