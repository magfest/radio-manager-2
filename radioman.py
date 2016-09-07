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
TEMPLATE_RADIO = "radio.jinja.html"
TEMPLATE_PERSON = "person.jinja.html"
TEMPLATE_DEPT = "dept.jinja.html"

BARCODE_RE = re.compile('^[A-Za-z0-9+=-]{6}$')

CHECKED_IN = 'CHECKED_IN'
CHECKED_OUT = 'CHECKED_OUT'
LOCKED = 'LOCKED'

ALLOW_MISSING_HEADSET = 'ALLOW_MISSING_HEADSET'
ALLOW_EXTRA_HEADSET = 'ALLOW_EXTRA_HEADSET'

ALLOW_DOUBLE_CHECKOUT = 'ALLOW_DOUBLE_CHECKOUT'
ALLOW_DOUBLE_RETURN = 'ALLOW_DOUBLE_RETURN'

ALLOW_LOCKED_CHECKOUT = 'ALLOW_LOCKED_CHECKOUT'

ALLOW_NEGATIVE_HEADSETS = 'ALLOW_NEGATIVE_HEADSETS'

ALLOW_DEPARTMENT_OVERDRAFT = 'ALLOW_DEPARTMENT_OVERDRAFT'

ALLOW_WRONG_PERSON = 'ALLOW_WRONG_PERSON'

RADIOS = {}

AUDIT_LOG = []
LAST_OPER = None

HEADSET_TOTAL = 0
HEADSETS = []
HEADSET_HISTORY = []

BATTERY_TOTAL = 0
BATTERIES = []
BATTERY_HISTORY = []

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

class RadioLocked(OverrideException):
    override = ALLOW_LOCKED_CHECKOUT

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

URLS = {
    'radio': '/radio/{}',
    'person': '/person/{}',
    'dept': '/dept/{}',
}


def link(key, type='radio', title=None):
    if key:
        prefix = 'Radio ' if type == 'radio' else ''

        return '<a{title} href="{url}">{prefix}{text}</a>'.format(title='title="{}"'.format(title) if title else '',
                                                                   url=URLS[type].format(key), prefix=prefix, text=key)
    else:
        return key
    
ENV.filters['fmt_date'] = fmt_date
ENV.filters['full_date'] = full_date
ENV.filters['rel_date'] = timesince
ENV.filters['link'] = link

def load_db():
    data = {}
    radiofile = CONFIG['db']
    try:
        with open(radiofile) as f:
            data = json.load(f)
        global HEADSETS, AUDIT_LOG, RADIOS

        RADIOS = data.get('radios', {})

        HEADSETS = data.get('headsets', [])
        if isinstance(HEADSETS, int):
            HEADSETS = []
        BATTERIES = data.get('batteries', [])

        AUDIT_LOG = data.get('audits', [])
    except FileNotFoundError:
        with open(radiofile, 'w') as f:
            json.dump({}, f)

def save_db():
    with open(CONFIG['db'], 'w') as f:
        json.dump({'radios': RADIOS, 'headsets': HEADSETS, 'audits': AUDIT_LOG, 'batteries': BATTERIES}, f)

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
    for headset in HEADSETS:
        if headset['department'] == dept:
            headset_count += 1
    return radio_count, headset_count


def filter_items(model, **kwargs):
    if model == "radios":
        return {id: radio for radio in RADIOS if all((attr in radio['checkout'] and radio['checkout'][attr] == val for attr, val in kwargs.items()))}
    if model == "headsets":
        target = HEADSETS
    elif model == "batteries":
        target = BATTERIES
    else:
        target = []
    return [item for item in target if all((attr in item and item[attr] == val for attr, val in kwargs.items()))]


def filter_radios(**kwargs):
    return filter_items("radios", **kwargs)


def filter_headsets(**kwargs):
    return filter_items("headsets", **kwargs)


def filter_batteries(**kwargs):
    return filter_items("batteries", **kwargs)


def checkout_battery(dept, name=None, badge=None, barcode=None, overrides=[]):
    if len(BATTERIES) >= BATTERY_TOTAL:
        raise HeadsetUnavailable("No headsets left")

    if dept in LIMITS and department_total(dept)[1] >= LIMITS[dept]:
        raise DepartmentOverLimit("{} has already checked out too many batteries".format(dept))

    entry = {
        "department": dept,
        "borrower": name,
        "time": time.time(),
        "status": CHECKED_OUT,
        "badge": badge
    }
    BATTERIES.append(entry)

    BATTERY_HISTORY.append(entry)
    save_db()


def checkout_headset(dept, name=None, badge=None, barcode=None, overrides=[ALLOW_NEGATIVE_HEADSETS]):
    if len(HEADSETS) >= HEADSET_TOTAL and \
       ALLOW_NEGATIVE_HEADSETS not in overrides:
        raise HeadsetUnavailable("No headsets left")

    if dept in LIMITS and department_total(dept)[1] >= LIMITS[dept]:
        raise DepartmentOverLimit("{} has already checked out too many headsets".format(dept))

    entry = {
        "department": dept,
        "borrower": name,
        "time": time.time(),
        "status": CHECKED_OUT,
        "badge": badge
    }
    HEADSETS.append(entry)

    HEADSET_HISTORY.append(entry)
    save_db()


def checkout_radio(id, dept, name=None, badge=None, barcode=None, headset=False, overrides=[]):
    try:
        radio = RADIOS[id]

        if radio['status'] == LOCKED:
            raise RadioLocked("Radio is locked")

        if radio['status'] == CHECKED_OUT and \
           ALLOW_DOUBLE_CHECKOUT not in overrides:
            raise RadioUnavailable("Already checked out")

        if dept in LIMITS and \
           (LIMITS[dept] != UNLIMITED and
            department_total(dept)[1] >= LIMITS[dept]) and \
            ALLOW_DEPARTMENT_OVERDRAFT not in overrides:
            raise DepartmentOverLimit("Department would exceed checkout limit")

        if headset:
            checkout_headset(dept, name=name, badge=badge, barcode=barcode, overrides=overrides + [ALLOW_NEGATIVE_HEADSETS])

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

        log(CHECKED_OUT, radio['last_activity'], id, name, badge, dept, headset)
        save_db()
    except IndexError:
        raise RadioNotFound("Radio does not exist")

def return_battery(barcode=None, name=None, badge=None, dept=None, overrides=[]):
    global BATTERIES

    for i, headset in enumerate(BATTERIES):
        if headset['borrower'] == name:
            del BATTERIES[i]
            BATTERY_HISTORY.append({
                'status': CHECKED_IN,
                'borrower': name,
                'time': time.time(),
                'department': dept,
                'badge': badge,
                'barcode': barcode,
            })
            save_db()
            return
    raise HeadsetUnavailable("No battery was found to check in")

def return_headset(barcode=None, name=None, badge=None, dept=None, overrides=[]):
    global HEADSETS

    for i, headset in enumerate(HEADSETS):
        if headset['borrower'] == name:
            del HEADSETS[i]
            HEADSET_HISTORY.append({
                'status': CHECKED_IN,
                'borrower': name,
                'time': time.time(),
                'department': dept,
                'badge': badge,
                'barcode': barcode,
            })
            save_db()
            return
    raise HeadsetUnavailable("No headset was found to check in")

def return_radio(id, headset, barcode=None, name=None, badge=None, overrides=[ALLOW_MISSING_HEADSET, ALLOW_EXTRA_HEADSET, ALLOW_WRONG_PERSON]):
    try:
        radio = RADIOS[id]

        if radio['status'] == CHECKED_IN and \
           ALLOW_DOUBLE_RETURN not in overrides:
            raise NotCheckedOut("Radio was already checked in")
        elif name != radio['checkout']['borrower'] and \
             ALLOW_WRONG_PERSON not in overrides:
            raise WrongPerson("Radio was checked out by '{}'".format(radio['checkout']['borrower']))

        radio['status'] = CHECKED_IN
        radio['last_activity'] = time.time()

        radio['checkout'] = {
            'status': radio['status'],
            'time': radio['last_activity'],
            'borrower': name or radio['checkout']['borrower'],
            'department': radio['checkout']['department'],
            'badge': badge or radio['checkout']['badge'],
            'headset': None,
            'barcode': barcode or radio['checkout']['barcode'],
        }

        if headset:
            return_headset(barcode=radio['checkout']['barcode'],
                           name=radio['checkout']['borrower'],
                           badge=radio['checkout']['badge'],
                           dept=radio['checkout']['department'])

        radio['history'].append(radio['checkout'])

        RADIOS[id] = radio

        log(CHECKED_IN, radio['last_activity'], id, '', '', '', headset)
        save_db()

        _, headsets_out, batteries_out = get_totals(get_person_history(name))

        return headsets_out, batteries_out
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

@APP.route('/headsetin', methods=['POST'])
def headset_in():
    args = request.form

    if args.get('borrower'):
        try:
            return_headset(name=args.get('borrower'), dept=args.get('department'))
            return flask.redirect(request.args.get('page', '/') + '?ok')
        except OverrideException as e:
            return flask.redirect('/?err=' + str(e.args[0].replace(' ', '+')))
    else:
        return flask.redirect('/?err=Name+and+department+are+required')

@APP.route('/headsetout', methods=['POST'])
def headset_out():
    args = request.form

    if args.get('borrower') and args.get('department'):
        try:
            checkout_headset(name=args.get('borrower'), dept=args.get('department'))
            return flask.redirect(request.args.get('page', '/') + '?ok')
        except OverrideException as e:
            return flask.redirect('/?err=' + str(e.args[0].replace(' ', '+')))
    else:
        return flask.redirect('/?err=Name+and+department+are+required')

@APP.route('/batteryin', methods=['POST'])
def battery_in():
    args = request.form

    if args.get('borrower'):
        try:
            return_headset(name=args.get('borrower'), dept=args.get('department'))
            return flask.redirect(request.args.get('page', '/') + '?ok')
        except OverrideException as e:
            return flask.redirect('/?err=' + str(e.args[0].replace(' ', '+')))
    else:
        return flask.redirect('/?err=Name+and+department+are+required')

@APP.route('/batteryout', methods=['POST'])
def battery_out():
    args = request.form

    if args.get('borrower') and args.get('department'):
        try:
            checkout_headset(name=args.get('borrower'), dept=args.get('department'))
            return flask.redirect(request.args.get('page', '/') + '?ok')
        except OverrideException as e:
            return flask.redirect('/?err=' + str(e.args[0].replace(' ', '+')))
    else:
        return flask.redirect('/?err=Name+and+department+are+required')


@APP.route('/checkin', methods=['POST'])
def checkin():
    args = request.form

    print(args)
    if args.get('id'):
        try:
            has_headset, has_battery = return_radio(args.get('id'), False)
            if has_headset or has_battery:
                return flask.redirect('/?check')
            return flask.redirect(request.args.get('page', '/') + '?ok')
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
    return flask.redirect(request.args.get('page', '/') + '?ok')

def set_locked(radio, locked):
    pass

@APP.route('/lock', methods=['POST'])
def lock():
    args = request.form

    radio = args.get('id', '')


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

    return flask.redirect(request.args.get('page', '/') + '?ok')

@APP.route('/radio/<id>')
def radios(id):
    if id not in RADIOS:
        return flask.redirect('/?err=Radio+does+not+exist')

    radio = RADIOS[str(id)]

    template = ENV.get_template(TEMPLATE_RADIO)
    return template.render(
        id=id,
        radio=radio,
    )

def get_person_history(name):
    evts = []

    for id, radio in RADIOS.items():
        for evt in radio['history']:
            if evt['borrower'] == name:
                newevt = {'type': 'radio', 'id': id}
                newevt.update(evt)
                evts.append(newevt)

    for evt in HEADSET_HISTORY:
        if evt['borrower'] == name:
            newevt = {'type': 'headset'}
            newevt.update(evt)
            evts.append(newevt)

    for evt in BATTERY_HISTORY:
        if evt['borrower'] == name:
            newevt = {'type': 'battery'}
            newevt.update(evt)
            evts.append(newevt)

    return evts

def get_dept_history(name):
    evts = []

    for id, radio in RADIOS.items():
        for evt in radio['history']:
            if evt['department'] == name:
                newevt = {'type': 'radio', 'id': id}
                newevt.update(evt)
                evts.append(newevt)

    for evt in HEADSET_HISTORY:
        if evt['department'] == name:
            newevt = {'type': 'headset'}
            newevt.update(evt)
            evts.append(newevt)

    for evt in BATTERY_HISTORY:
        if evt['department'] == name:
            newevt = {'type': 'battery'}
            newevt.update(evt)
            evts.append(newevt)

    return evts


def get_totals(evts):
    radios, headsets, batteries = 0, 0, 0

    for evt in evts:
        if evt['type'] == 'radio':
            if evt['status'] == CHECKED_IN:
                radios -= 1
            elif evt['status'] == CHECKED_OUT:
                radios += 1
        elif evt['type'] == 'headset':
            if evt['status'] == CHECKED_IN:
                headsets -= 1
            elif evt['status'] == CHECKED_OUT:
                radios += 1
        elif evt['type'] == 'battery':
            if evt['status'] == CHECKED_IN:
                batteries -= 1
            elif evt['status'] == CHECKED_OUT:
                batteries += 1

    return radios, headsets, batteries

@APP.route('/person/<name>')
def person(name):
    evts = get_person_history(name)

    radios, headsets, batteries = get_totals(evts)

    out_radios = []

    for id, radio in RADIOS.items():
        if radio['status'] == CHECKED_OUT and radio['checkout']['borrower'] == name:
            out_radios.append((id, radio['checkout']))

    template = ENV.get_template(TEMPLATE_PERSON)
    return template.render(
        name=name,
        history=evts,
        radios=radios,
        headsets=headsets,
        batteries=batteries,
        out_radios=out_radios,
    )


@APP.route('/dept/<name>')
def dept(name):
    evts = get_dept_history(name)

    radios, headsets, batteries = 0, 0, 0

    out_radios = {}

    for evt in evts:
        if evt['type'] == 'radio':
            if evt['status'] == CHECKED_IN:
                radios -= 1
            elif evt['status'] == CHECKED_OUT:
                radios += 1
        elif evt['type'] == 'headset':
            if evt['status'] == CHECKED_IN:
                headsets -= 1
            elif evt['status'] == CHECKED_OUT:
                radios += 1
        elif evt['type'] == 'battery':
            if evt['status'] == CHECKED_IN:
                batteries -= 1
            elif evt['status'] == CHECKED_OUT:
                batteries += 1

    template = ENV.get_template(TEMPLATE_DEPT)
    return template.render(
        name=name,
        history=evts,
        radios=radios,
        headsets=headsets,
        batteries=batteries,
    )


@APP.route('/')
def index():
    args = request.args

    msg = None
    err = None
    warn = None

    if 'ok' in args:
        msg = "Success"

    if 'err' in args:
        err = args['err']

    if 'check' in args:
        warn = "Success! Please check in user&apos;s remaining accessories."

    print(HEADSETS)
    
    template = ENV.get_template(TEMPLATE_INDEX)
    return template.render(
        msg=msg, error=err, warn=warn,
        radios=sorted(RADIOS.items(), key=lambda k:int(k[0])),
        headsets=HEADSET_TOTAL-len(HEADSETS),
        batteries=BATTERY_TOTAL-len(BATTERIES),
        headsets_out=HEADSETS,
        batteries_out=BATTERIES,
    )

APP.run('0.0.0.0', port=8080, debug=True)
