import requests
from datetime import datetime
import simplejson
from urllib.parse import unquote as urldecode

import flask

from config import config
from misc import parse_datetime, render_datetime, \
                 hash_password, \
                 service_uris


app = flask.Flask(__name__)
app.config['DEBUG'] = config['debug']


class Session(dict, flask.sessions.SessionMixin):
    def __init__(self, json):
        self.id = json['id']
        self.user_id = json['user_id']
        self.data = {item['key']: item['value'] for item in json['data_items']}

    @property
    def data(self):
        return self

    @data.setter
    def data(self, data):
        self.clear()
        self.update(data)

    def to_json(self):
        return {
            'user_id': self.user_id,
            'last_used_at': render_datetime(datetime.now()),
            'data_items': [{'key': key, 'value': value} for key, value in self.data.items()]
        }
       
class SessionInterface(flask.sessions.SessionInterface):
    def open_session(self, app, request):
        try:
            if 'session_id' in request.cookies:
                session_response = requests.get(service_uris['sessions'] + '/' + request.cookies['session_id'])
                if session_response.status_code == 200:
                    session = session_response.json()
                    if parse_datetime(session['last_used_at']) + config['website']['session_expires_after'] > datetime.now():
                        return Session(session)

            session_response = requests.post(service_uris['sessions'], json={
                'last_used_at': render_datetime(datetime.now()),
            })
            if session_response.status_code == 201:
                session = session_response.json()
                return Session(session)
        except requests.exceptions.RequestException:
            return Session({
                'id': None,
                'user_id': None,
                'data_items': [],
            })

    def save_session(self, app, session, response):
        if session.id is None:
            response.set_cookie('session_id', '', expires=0)
            return

        try:
            session_response = requests.patch(service_uris['sessions'] + '/' + str(session.id), json=session.to_json())
            if session_response.status_code == 200:
                response.set_cookie('session_id', str(session.id))
        except requests.exceptions.RequestException:
            pass

app.session_interface = SessionInterface()


@app.route('/', methods=['GET'])
def index():
    return flask.redirect('/foods/')


@app.route('/register', methods=['GET'])
def register():
    if 'redirect_to' in flask.request.args:
        flask.session['redirect_to'] = urldecode(flask.request.args['redirect_to'])

    if flask.session.user_id is not None:
        return flask.redirect('/me')

    return flask.render_template('register.html')

@app.route('/register', methods=['POST'])
def post_to_register():
    try:
        user_response = requests.post(service_uris['users'], json={
            'login': flask.request.form['login'],
            'password_hash': hash_password(flask.request.form['password']),
            'name': flask.request.form.get('name', None),
            'phone': flask.request.form.get('phone', None),
            'email': flask.request.form.get('email', None),
        })
    except requests.exceptions.RequestException:
        return flask.render_template('error.html', reason='Users backend is unavailable'), 500

    if user_response.status_code == 201:
        user = user_response.json()
        flask.session.user_id = user['id']
        return flask.redirect(flask.session.pop('redirect_to', '/me'), code=303)

    return flask.render_template('error.html', reason=user_response.json()), 500


@app.route('/sign_in', methods=['GET'])
def sign_in():
    if 'redirect_to' in flask.request.args:
        flask.session['redirect_to'] = urldecode(flask.request.args['redirect_to'])

    if flask.session.user_id is not None:
        return flask.redirect('/me')

    return flask.render_template('sign_in.html')

@app.route('/sign_in', methods=['POST'])
def post_to_sign_in():
    try:
        user_response = requests.get(service_uris['users'], params={
            'q': simplejson.dumps({
                'filters': [
                    {'name': 'login', 'op': '==', 'val': flask.request.form['login']},    
                    {'name': 'password_hash', 'op': '==', 'val': hash_password(flask.request.form['password'])},    
                ],
                'single': True,
            }),
        })
    except requests.exceptions.RequestException:
        return flask.render_template('error.html', reason='Users backend is unavailable'), 500

    if user_response.status_code == 200:
        user = user_response.json()
        flask.session.user_id = user['id']
        return flask.redirect(flask.session.pop('redirect_to', '/me'), code=303)

    return flask.render_template('error.html', reason=user_response.json()), 500


@app.route('/me', methods=['GET'])
def me():
    if flask.session.user_id is None:
        flask.session['redirect_to'] = '/me'
        return flask.redirect('/sign_in')

    try:
        user_response = requests.get(service_uris['users'] + '/' + str(flask.session.user_id))
        assert user_response.status_code == 200
        user = user_response.json()
    except requests.exceptions.RequestException:
        user = None

    return flask.render_template('me.html', user=user)


@app.route('/me', methods=['POST'])
def patch_me():
    user = {}
    if 'password' in flask.request.form and flask.request.form['password']:
        user['password_hash'] = hash_password(flask.request.form['password'])
    if 'name' in flask.request.form:
        user['name'] = flask.request.form['name'] or None
    if 'phone' in flask.request.form:
        user['phone'] = flask.request.form['phone'] or None
    if 'email' in flask.request.form:
        user['email'] = flask.request.form['email'] or None

    try:
        user_response = requests.patch(service_uris['users'] + '/' + str(flask.session.user_id), json=user)
    except requests.exceptions.RequestException:
        return flask.render_template('error.html', reason='Users backend is unavailable'), 500

    if user_response.status_code == 200:
        user = user_response.json()
        return flask.render_template('me.html', user=user)

    return flask.render_template('error.html', reason=user_response.json()), 500


@app.route('/foods/', methods=['GET'])
def foods():
    user_name = None
    if flask.session.user_id is not None:
        try:
            user_response = requests.get(service_uris['users'] + '/' + str(flask.session.user_id))
            if user_response.status_code != 200:
                return flask.render_template('error.html', reason=user_response.json()), 500

            user = user_response.json()
            user_name = user['name']
        except requests.exceptions.RequestException:
            pass

    try:
        per_page = int(flask.request.args.get('per_page', 20))
        page = int(flask.request.args.get('page', 1))

        foods_response = requests.get(service_uris['foods'], params={
            'results_per_page': per_page,
            'page': page,
        })
        assert foods_response.status_code == 200

        foods = foods_response.json()

        if 'cart' not in flask.session:
            flask.session['cart'] = {}
        for food in foods['objects']:
            food['quantity'] = flask.session['cart'].get(str(food['id']), 0)

        pages = foods['total_pages']
        page_foods = foods['objects']
    except requests.exceptions.RequestException:
        pages = 0
        page_foods = None

    return flask.render_template('foods.html', user_name=user_name,
                                               page_foods=page_foods,
                                               per_page=per_page,
                                               page=page,
                                               pages=pages)

@app.route('/foods/', methods=['POST'])
def post_to_foods():
    food_id = flask.request.form['food_id']
    action = flask.request.form['action']

    cart = flask.session['cart']

    if food_id not in cart:
        cart[food_id] = 0

    if action == '+':
        cart[food_id] += 1 
    elif action == '-' and cart.get(food_id, 0) > 0:
        cart[food_id] -= 1 
        if cart[food_id] == 0:
            del cart[food_id]

    return flask.redirect(flask.request.url, code=303)

@app.route('/order', methods=['GET'])
def order():
    foods = []
    if 'cart' not in flask.session:
        flask.session['cart'] = {}
    for food_id, quantity in flask.session['cart'].items():
        try:
            food_response = requests.get(service_uris['foods'] + '/' + food_id)
            assert food_response.status_code == 200

            food = food_response.json()
            foods.append({
                'name': food['name'],
                'price': food['price'],
                'quantity': quantity,
            })
        except requests.exceptions.RequestException:
            pass

    return flask.render_template('order.html', foods=foods)

@app.route('/order', methods=['POST'])
def post_to_order():
    order_items = []
    for food_id, quantity in flask.session['cart'].items():
        order_items.append({
            'food_id': int(food_id),
            'quantity': quantity,
        })

    try:
        order_response = requests.post(service_uris['orders'], json={
            'user_id': flask.session.user_id,
            'opened_at': render_datetime(datetime.now()),
            'items': order_items,
            'deliver_to': flask.request.form['deliver_to'],
        })
        assert order_response.status_code == 201

        del flask.session['cart']
    except requests.exceptions.RequestException:
        pass

    return flask.redirect('/orders/', code=303)

@app.route('/orders/', methods=['GET'])
def orders():
    if flask.session.user_id is None:
        flask.session['redirect_to'] = '/orders/'
        return flask.redirect('/sign_in')

    user_name = None
    if flask.session.user_id is not None:
        try:
            user_response = requests.get(service_uris['users'] + '/' + str(flask.session.user_id))
            if user_response.status_code != 200:
                return flask.render_template('error.html', reason=user_response.json()), 500

            user = user_response.json()
            user_name = user['name']
        except requests.exceptions.RequestException:
            pass

    try:
        orders_response = requests.get(service_uris['orders'], params={
            'q': simplejson.dumps({
                'filters': [
                    {'name': 'user_id', 'op': '==', 'val': flask.session.user_id},    
                ],
            }),
        })
        assert orders_response.status_code == 200
        orders = orders_response.json()
        orders = orders['objects']
    except requests.exceptions.RequestException:
        orders = None

    return flask.render_template('orders.html', user_name=user_name,
                                                orders=orders)

if __name__ == '__main__':
    app.run(port=config['website']['port'])

