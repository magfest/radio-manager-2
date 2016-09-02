
#!/usr/bin/env python2
import re
import json
import time
import flask
import jinja2
import datetime
from flask import request
from rpctools.jsonrpc import ServerProxy

APP = flask.Flask(__name__)
ENV = jinja2.Environment(loader=jinja2.FileSystemLoader('./templates'))

CONFIG = {
    'db': 'radios.json',
}

LIMITS = {}

UNLIMITED = None

TEMPLATE_INDEX = "index.jinja.html"

BARCODE_RE = re.compile('^[A-Za-z0-9+=-]{6}$')

CHECKED_IN = 'CHECKED_IN'
CHECKED_OUT = 'CHECKED_OUT'

ALLOW_MISSING_HEADSET = 'ALLOW_MISSING_HEADSET'
ALLOW_EXTRA_HEADSET = 'ALLOW_EXTRA_HEADSET'

ALLOW_DOUBLE_CHECKOUT = 'ALLOW_DOUBLE_CHECKOUT'
ALLOW_DOUBLE_RETURN = 'ALLOW_DOUBLE_RETURN'

ALLOW_NEGATIVE_HEADSETS = 'ALLOW_NEGATIVE_HEADSETS'

ALLOW_DEPARTMENT_OVERDRAFT = 'ALLOW_DEPARTMENT_OVERDRAFT'

ALLOW_WRONG_PERSON = 'ALLOW_WRONG_PERSON'

RADIOS = {}

AUDIT_LOG = []
LAST_OPER = None

HEADSETS = 0

UBER = None


class RadioNotFound(Exception):
    pass

class CreateRadioException(Exception):
    pass

class RadioExists(CreateRadioException):
    pass

class InvalidID(CreateRadioException):
    pass

class OverrideException(Exception):
    override = None

class RadioUnavailable(OverrideException):
    override = ALLOW_DOUBLE_CHECKOUT

class HeadsetUnavailable(OverrideException):
    override = ALLOW_NEGATIVE_HEADSETS

class HeadsetRequired(OverrideException):
    override = ALLOW_MISSING_HEADSET

class UnexpectedHeadset(OverrideException):
    override = ALLOW_EXTRA_HEADSET

class NotCheckedOut(OverrideException):
    override = ALLOW_DOUBLE_RETURN

class DepartmentOverLimit(OverrideException):
    override = ALLOW_DEPARTMENT_OVERDRAFT

class WrongPerson(OverrideException):
    override = ALLOW_WRONG_PERSON

def fmt_date(timestamp):
    return datetime.datetime.fromtimestamp(timestamp).strftime('%H:%M %a') if timestamp else '&hellip;'

def full_date(timestamp):
    return datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')

def timesince(dt, default="just now"):
    """
Returns string representing "time since" e.g.
3 days ago, 5 hours ago etc.
    """

    now = datetime.datetime.now()
    diff = now - datetime.datetime.fromtimestamp(dt)

    periods = (
        (diff.days // 365, "year", "years"),
        (diff.days // 30, "month", "months"),
        (diff.days // 7, "week", "weeks"),
        (diff.days, "day", "days"),
        (diff.seconds // 3600, "hour", "hours"),
        (diff.seconds // 60, "minute", "minutes"),
        (diff.seconds, "second", "seconds"),
    )

    for period, singular, plural in periods:
        if period:
            return "%d %s ago" % (period, singular if period == 1 else plural)

    return default
    
ENV.filters['fmt_date'] = fmt_date
ENV.filters['full_date'] = full_date
ENV.filters['rel_date'] = timesince

def load_db():
    data = {}
    radiofile = CONFIG['db']
    try:
        with open(radiofile) as f:
            data = json.load(f)
        global HEADSETS, AUDIT_LOG, RADIOS

        RADIOS = data.get('radios', {})

        HEADSETS = data.get('headsets', 0)
        AUDIT_LOG = data.get('audits', [])
    except FileNotFoundError:
        with open(radiofile, 'w') as f:
            json.dump({}, f)

def save_db():
    with open(CONFIG['db'], 'w') as f:
        json.dump({'radios': RADIOS, 'headsets': HEADSETS, 'audits': AUDIT_LOG}, f)

def get_blank_radio():
    return {
        'status': CHECKED_IN,
        'last_activity': 0,
        'history': [{'status': CHECKED_IN,
                     'department': None,
                     'borrower': None,
                     'badge': None,
                     'headset': None,
                     'time': 0,
        }],
        'checkout': {
            'status': CHECKED_IN,
            'department': None,
            'borrower': None,
            'badge': None,
            'headset': None,
            'time': 0,
        },
    }

def configure(f):
    global CONFIG, RADIOS
    with open(f) as conf:
        CONFIG.update(json.load(conf))

    load_db()

    for radio in CONFIG.get('radios', []):
        if str(radio) not in RADIOS:
            RADIOS[radio] = get_blank_radio()

    for name, dept in CONFIG.get('departments', {}).items():
        LIMITS[name] = dept.get('limit', UNLIMITED)

    save_db()

    global UBER
    if 'uber' in CONFIG:
        uber = CONFIG.get('uber', {})
        key = uber.get('key', './client.key')
        cert = uber.get('cert', './client.crt')
        uri = uber.get('uri', 'https://magfest.uber.org/jsonrpc')

        if uber.get('auth', False):
            UBER = ServerProxy(uri=uri,
                               key_file=key,
                               cert_file=cert)
        else:
            UBER = ServerProxy(uri)
    else:
        print('Security not configured, probably won\'t be able to use barcodes')

def log(*fields):
    logfile = CONFIG.get('log', 'radios.log')
    with open(logfile, 'a+') as f:
        f.write(','.join((str(f) for f in fields)) + '\n')

def log_audit(*fields):
    logfile = CONFIG.get('audit_log', 'audits.log')
    with open(logfile, 'a+') as f:
        f.write(','.join((str(f) for f in fields)) + '\n')

def department_total(dept):
    radio_count = 0
    headset_count = 0
    for radio in RADIOS.values():
        if radio['status'] == CHECKED_OUT and \
           radio['checkout']['department'] == dept:
            radio_count += 1
            if radio['headset']:
                headset_count += 1
    return (radio_count, headset_count)
        
def checkout_radio(id, dept, name=None, badge=None, barcode=None, headset=False, overrides=[]):
    global HEADSETS
    try:
        radio = RADIOS[id]

        if radio['status'] == CHECKED_OUT and \
           ALLOW_DOUBLE_CHECKOUT not in overrides:
            raise RadioUnavailable("Already checked out")

        if headset and HEADSETS <= 0 and \
           ALLOW_NEGATIVE_HEADSETS not in overrides:
            raise HeadsetUnavailable("No headsets left")

        if dept in LIMITS and \
           (LIMITS[dept] != UNLIMITED and
            department_total(dept)[1] >= LIMITS[dept]) and \
            ALLOW_DEPARTMENT_OVERDRAFT not in overrides:
            raise DepartmentOverLimit("Department would exceed checkout limit")

        radio['status'] = CHECKED_OUT
        radio['last_activity'] = time.time()
        radio['checkout'] = {
            'status': radio['status'],
            'time': radio['last_activity'],
            'borrower': name,
            'department': dept,
            'badge': badge,
            'barcode': barcode,
            'headset': headset,
        }
        radio['history'].append(radio['checkout'])

        if headset:
            HEADSETS -= 1

        log(CHECKED_OUT, radio['last_activity'], id, name, badge, dept, headset)
        save_db()
    except IndexError:
        raise RadioNotFound("Radio does not exist")

def return_radio(id, headset, barcode=None, name=None, badge=None, overrides=[ALLOW_MISSING_HEADSET, ALLOW_EXTRA_HEADSET, ALLOW_WRONG_PERSON]):
    try:
        radio = RADIOS[id]

        if radio['status'] == CHECKED_IN and \
           ALLOW_DOUBLE_RETURN not in overrides:
            raise NotCheckedOut("Radio was already checked in")
        elif radio['checkout']['headset'] and not headset and \
           ALLOW_MISSING_HEADSET not in overrides:
            raise HeadsetRequired("Radio was checked out with headset")
        elif headset and not radio['checkout']['headset'] and \
             ALLOW_EXTRA_HEADSET not in overrides:
            raise UnexpectedHeadset("Radio was not checked out with headset")
        elif name != radio['checkout']['borrower'] and \
             ALLOW_WRONG_PERSON not in overrides:
            raise WrongPerson("Radio was checked out by '{}'".format(radio['checkout']['borrower']))

        radio['status'] = CHECKED_IN
        radio['last_activity'] = time.time()

        radio['checkout'] = {
            'status': radio['status'],
            'time': radio['last_activity'],
            'borrower': name,
            'department': None,
            'badge': badge,
            'headset': None,
            'barcode': barcode,
        }

        radio['history'].append(radio['checkout'])

        RADIOS[id] = radio

        if headset:
            global HEADSETS
            HEADSETS += 1

        log(CHECKED_IN, radio['last_activity'], id, '', '', '', headset)
        save_db()
    except IndexError:
        raise RadioNotFound("Radio does not exist")

def lookup_badge(barcode):
    if UBER:
        res = UBER.barcode.lookup_attendee_from_barcode(barcode_value=barcode)
        if 'error' in res:
            raise ValueError(res['error'])
        return res['full_name'], res['badge_num']
    else:
        raise ValueError('Uber not set up')

def get_person_info():
    barcode, name, badge = None, None, None

    data = get_person()
    if BARCODE_RE.match(data.strip()):
        barcode = data
        while True:
            try:
                name, badge = lookup_badge(barcode)
                break
            except Exception as e:
                print(e)
                barcode = None
                name = data
                break
    else:
        name = data

    return barcode, name, badge

configure('config.json')

@APP.route('/checkin', methods=['POST'])
def checkin():
    args = request.form

    print(args)
    if args.get('id'):
        try:
            return_radio(args.get('id'), True)
            return flask.redirect('/?ok')
        except OverrideException as e:
            if e.args:
                return flask.redirect('/?err=' + str(e.args[0]).replace(' ', '+'))
    else:
        return flask.redirect('/?err=Stop+messing+with+stuff')

@APP.route('/checkout', methods=['POST'])
def checkout():
    args = request.form
    print(args)

    id = args.get('id', '')
    name = args.get('name', '')
    dept = args.get('department', '')
    headset = args.get('headset', '')

    if not name:
        return flask.redirect('/?err=Name+is+required')

    if not id:
        return flask.redirect('/?err=Stop+messing+with+stuff')

    try:
        checkout_radio(id, dept, name=name, headset=bool(headset), overrides=[])
    except OverrideException as e:
        if e.args:
            return flask.redirect('/?err=' + str(e.args[0]).replace(' ', '+'))
    return flask.redirect('/?ok')

@APP.route('/newradio', methods=['POST'])
def newradio():
    args = request.form
    print(args)

    radio = args.get('id', '')

    if not radio:
        return flask.redirect('/?err=ID+is+required')

    try:
        try:
            assert int(radio) > 0
        except:
            raise InvalidID("Invalid radio ID")

        if str(radio) in RADIOS:
            raise RadioExists("Radio {} already exists".format(radio))

        RADIOS[radio] = get_blank_radio()
        save_db()
    except (OverrideException, CreateRadioException) as e:
        if e.args:
            return flask.redirect('/?err=' + str(e.args[0]).replace(' ', '+'))

    return flask.redirect('/?ok')

@APP.route('/')
def index():
    args = request.args

    msg = None
    err = None

    if 'ok' in args:
        msg="Success"

    if 'err' in args:
        err = args['err']
    
    template = ENV.get_template(TEMPLATE_INDEX)
    return template.render(
        msg=msg, error=err,
        radios=sorted(RADIOS.items(), key=lambda k:int(k[0])),
        headsets=HEADSETS
    )

APP.run('0.0.0.0', port=8080, debug=True)
