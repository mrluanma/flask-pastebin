from datetime import datetime
from flask import Flask, request, url_for, redirect, g, session, flash, \
     abort, render_template
from flask.ext.sqlalchemy import SQLAlchemy
from rauth.service import OAuth2Service
from juggernaut import Juggernaut


app = Flask(__name__)
app.config.from_pyfile('config.cfg')
db = SQLAlchemy(app)
jug = Juggernaut()


wb = OAuth2Service(
    name='wb',
    consumer_key=app.config['WB_CLIENT_ID'],
    consumer_secret=app.config['WB_CLIENT_SECRET'],
    access_token_url='https://api.weibo.com/oauth2/access_token',
    authorize_url='https://api.weibo.com/oauth2/authorize',
)


def url_for_other_page(page):
    args = request.view_args.copy()
    args['page'] = page
    return url_for(request.endpoint, **args)
app.jinja_env.globals['url_for_other_page'] = url_for_other_page


class Paste(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.Text)
    pub_date = db.Column(db.DateTime)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    parent_id = db.Column(db.Integer, db.ForeignKey('paste.id'))
    parent = db.relationship('Paste', lazy=True, backref='children',
                             uselist=False, remote_side=[id])

    def __init__(self, user, code, parent=None):
        self.user = user
        self.code = code
        self.pub_date = datetime.utcnow()
        self.parent = parent


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    display_name = db.Column(db.String(120))
    wb_id = db.Column(db.String(30), unique=True)
    pastes = db.relationship(Paste, lazy='dynamic', backref='user')


def send_new_paste_notifications(paste, reply):
    """Notifies clients about new pastes."""
    user = None
    user_id = None
    if paste.user:
        user = paste.user.display_name
        user_id = paste.user.id
    data = {'paste_id': paste.id, 'reply_id': reply.id, 'user': user}
    jug.publish('paste-replies:%d' % paste.id, data)
    if user_id is not None:
        jug.publish('user-replies:%d' % user_id, data)


@app.before_request
def check_user_status():
    g.user = None
    if 'user_id' in session:
        g.user = User.query.get(session['user_id'])


@app.route('/', methods=['GET', 'POST'])
def new_paste():
    parent = None
    reply_to = request.args.get('reply_to', type=int)
    if reply_to is not None:
        parent = Paste.query.get(reply_to)
    if request.method == 'POST' and request.form['code']:
        paste = Paste(g.user, request.form['code'], parent=parent)
        db.session.add(paste)
        db.session.commit()
        if parent is not None:
            send_new_paste_notifications(parent, paste)
        return redirect(url_for('show_paste', paste_id=paste.id))
    return render_template('new_paste.html', parent=parent)


@app.route('/<int:paste_id>')
def show_paste(paste_id):
    paste = Paste.query.options(db.eagerload('children')).get_or_404(paste_id)
    return render_template('show_paste.html', paste=paste)


@app.route('/<int:paste_id>/delete', methods=['GET', 'POST'])
def delete_paste(paste_id):
    paste = Paste.query.get_or_404(paste_id)
    if g.user is None or g.user != paste.user:
        abort(401)
    if request.method == 'POST':
        if 'yes' in request.form:
            db.session.delete(paste)
            db.session.commit()
            flash('Paste was deleted')
            return redirect(url_for('new_paste'))
        else:
            return redirect(url_for('show_paste', paste_id=paste.id))
    return render_template('delete_paste.html', paste=paste)


@app.route('/my-pastes/', defaults={'page': 1})
@app.route('/my-pastes/page/<int:page>')
def my_pastes(page):
    if g.user is None:
        return redirect(url_for('login', next=request.url))
    pagination = Paste.query.filter_by(user=g.user).paginate(page)
    return render_template('my_pastes.html', pagination=pagination)


@app.route('/login')
def login():
    url = wb.get_authorize_url(
        redirect_uri=url_for('wb_authorized', _external=True),
        response_type='code'
    )
    return redirect(url)


@app.route('/logout')
def logout():
    session.clear()
    flash('You were logged out')
    return redirect(url_for('new_paste'))


@app.route('/login/authorized')
def wb_authorized():
    next_url = request.args.get('next') or url_for('new_paste')
    if 'error' in request.args:
        flash('You denied the login')
        return redirect(next_url)

    data = dict(
        code=request.args['code'],
        redirect_uri=url_for('wb_authorized', _external=True),
    )
    token = wb.get_access_token('POST', data=data).content

    params = dict(
        source=wb.consumer_key,
        access_token=token['access_token'],
        uid=token['uid'],
    )
    me = wb.get('https://api.weibo.com/2/users/show.json', params=params)
    user = User.query.filter_by(wb_id=me.content['id']).first()
    if user is None:
        user = User()
        user.wb_id = me.content['id']
        db.session.add(user)

    user.display_name = me.content['name']
    db.session.commit()
    session['user_id'] = user.id

    flash('You are now logged in as %s' % user.display_name)
    return redirect(next_url)
