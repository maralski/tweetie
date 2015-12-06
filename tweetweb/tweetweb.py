from flask import Flask, render_template, jsonify, url_for, Response, send_from_directory, abort, make_response, request, redirect
from random import Random
import redis
import json
import urlparse
import oauth2 as oauth
import uuid
import os
import logging
import time
import sys

# constants
request_token_url = os.getenv('REQUEST_TOKEN_URL', 'https://api.twitter.com/oauth/request_token')
access_token_url = os.getenv('ACCESS_TOKEN_URL', 'https://api.twitter.com/oauth/access_token')
authorize_url = os.getenv('AUTHORIZE_URL', 'https://api.twitter.com/oauth/authorize')

# Read in sensitive data from private json file
data_file = open('./private/data.json', 'r')
data = json.loads(data_file.read())
logging.debug("Read in json {}", str(data))
data_file.close()

consumer_key = data['CONSUMER_KEY']
consumer_secret = data['CONSUMER_SECRET']
callback_url = None

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format='%(asctime)s:%(module)s:%(levelname)s:(%(threadName)-9s) %(message)s')
logging.Formatter.converter = time.gmtime

# Random object
random = Random()

app = Flask(__name__, static_url_path="")

app.config["SECRET_KEY"] = uuid.uuid4().hex

@app.errorhandler(404)
def not_found(error):
    return make_response("Page not found", 404)

@app.route("/s/<myuuid>", methods=['GET'])
def spa(myuuid=None):

    # Get the twitter screen name for this uuid
    screen_name = json.loads(r.get("creds:" + myuuid))["screen_name"]

    return render_template("spa.html", myuuid=myuuid, twitter_login=screen_name)

@app.route("/", methods=['GET'])
def index():

    return render_template("index.html")

@app.route("/r", methods=['GET'])
def register():

    global consumer_key, consumer_secret, authorize_url, access_token_url, request_token_url, callback_url

    # Initialize oauth using consumer key and secret
    consumer = oauth.Consumer(consumer_key, consumer_secret)
    client = oauth.Client(consumer)

    # Generate unique UUID for this session
    myuuid = uuid.uuid4().hex

    # Register the UUID with the twitter API callback URL
    req_token_url = request_token_url + "?oauth_callback=" + callback_url + "/a/" + myuuid

    # Get a request token from twitter API
    resp, content = client.request(req_token_url, "GET")
    if resp['status'] != '200':
        message = "Could not connect to twitter to authenticates: {}\n{}".format(resp['status'], content)
        return render_template("error.html", message=message.decode('utf8'))

    # Parse response from twitter API
    request_token = dict(urlparse.parse_qsl(content))

    # Check if we received the tokens from twitter API
    if "oauth_token" not in request_token or "oauth_token_secret" not in request_token:
        message = "Twitter response malformed: %s\n" % str(request_token)
        return render_template("error.html", message=message.decode('utf8'))

    # Get tokens
    oauth_token = request_token['oauth_token']
    oauth_token_secret = request_token['oauth_token_secret']

    # Add credentials to redis using UUID in key
    r.set("creds:" + myuuid, json.dumps({
        'uuid': myuuid,
        'oauth_token': oauth_token,
        'oauth_token_secret': oauth_token_secret
    }))

    logging.debug("{} got request token {} with secret {}".format(myuuid, oauth_token, oauth_token_secret))

    # Authorize with twitter API
    redirect_url = "{}?oauth_token={}".format(authorize_url, oauth_token)

    logging.debug("Redirect URL is {}".format(redirect_url))

    return redirect(redirect_url)

# This route sets the twitter keyword to monitor
#
@app.route("/k/<myuuid>", methods=["POST"])
def keyword(myuuid=None):
    if not request.json or not "keyword" in request.json or myuuid is None:
        abort(400)

    keyword = request.json['keyword']

    logging.debug("{} got keyword {}".format(myuuid, keyword))

    # Put keyword at top of queue for worker to pickup
    r.lpush("request_queue", json.dumps({'keyword': keyword, 'uuid': myuuid}))

    return Response(response=json.dumps({'keyword': keyword}), status=200, mimetype="application/json")

# This route returns the twitter data associated with the keyword
#
@app.route("/d/<myuuid>", methods=['GET'])
def data(myuuid=None):
    result = []
    tqueue = "twitter:" + myuuid
    for i in range(r.llen(tqueue)):
        result.append(json.loads(r.lpop(tqueue)))
    logging.debug("{} tweet {}".format(myuuid, str(result)))
    return Response(response=json.dumps(result), status=200, mimetype="application/json")

@app.route("/a/<myuuid>", methods=['GET'])
def done(myuuid=None):

    global consumer_key, consumer_secret, oauth_token_secret_global, access_token_url, callback_url

    # Get our credentials from UUID
    credobj = json.loads(r.get("creds:" + myuuid))

    oauth_token = request.args.get('oauth_token')
    oauth_token_secret = credobj['oauth_token_secret'] # Twitter does not return this
    oauth_verifier = request.args.get('oauth_verifier')

    logging.debug("{} got auth token {} and verifier {} and secret {}".format(myuuid, oauth_token, oauth_verifier, oauth_token_secret))

    # Verify token with Twitter API
    token = oauth.Token(oauth_token, oauth_token_secret)
    token.set_verifier(oauth_verifier)
    client = oauth.Client(oauth.Consumer(consumer_key, consumer_secret), token)
    resp, content = client.request(access_token_url, "POST")

    if resp['status'] != '200':
        message = "Problem verifying token with twitter: {}\n{}".format(resp['status'], content)
        return render_template("error.html", message=message.decode('utf8'))

    # Parse response from twitter API
    access_token = dict(urlparse.parse_qsl(content))

    if "oauth_token_secret" not in access_token or "oauth_token" not in access_token:
        message = "Problem authenticating with twitter: {}\n{}".format(resp['status'], content)
        return render_template("error.html", message=message.decode('utf8'))

    # Update the token to match the new access token
    oauth_token = access_token["oauth_token"]
    oauth_token_secret = access_token["oauth_token_secret"]
    screen_name = access_token["screen_name"]

    logging.debug("{} got access token {} with secret {} for user {}".format(myuuid, oauth_token, oauth_token_secret, screen_name))

    # Reset credentials to redis using UUID in key
    r.set("creds:" + myuuid, json.dumps({
        'uuid': myuuid,
        'consumer_key': consumer_key,
        'consumer_secret': consumer_secret,
        'oauth_token': oauth_token,
        'oauth_token_secret': oauth_token_secret,
        'screen_name': screen_name
    }))

    return redirect("/s/{}".format(myuuid))

if __name__ == "__main__":

    # CF stuff
    vcap_services = os.getenv("VCAP_SERVICES")
    vs = json.loads(vcap_services)
    redis_hostname = vs["rediscloud"][0]["credentials"]["hostname"]
    redis_password = vs["rediscloud"][0]["credentials"]["password"]
    redis_port = vs["rediscloud"][0]["credentials"]["port"]

    vcap_application = os.getenv("VCAP_APPLICATION")
    vs = json.loads(vcap_application)
    callback_url = "http://" + vs["uris"][0]
    logging.debug("Callback url set to {}".format(callback_url))
    logging.debug("Connecting to redis at {} on port {}".format(redis_hostname, redis_port))

    # Initialize handle to Redis
    r = redis.Redis(host=redis_hostname, port=redis_port, password=redis_password)

    # Start flask app
    app.run(port=int(os.getenv('PORT', 12000)), host="0.0.0.0", debug=False)


# Twitter response
#
# {u'contributors': None,
#  u'coordinates': None,
#  u'created_at': u'Mon Nov 23 04:11:48 +0000 2015',
#  u'entities': {u'hashtags': [],
#                u'symbols': [],
#                u'urls': [],
#                u'user_mentions': [{u'id': 880357369,
#                                    u'id_str': u'880357369',
#                                    u'indices': [0, 13],
#                                    u'name': u'#RugbyBarbarians',
#                                    u'screen_name': u'RugbyBaaBaas'},
#                                   {u'id': 880474278,
#                                    u'id_str': u'880474278',
#                                    u'indices': [14, 24],
#                                    u'name': u'#RugbyArgentina',
#                                    u'screen_name': u'RugbyARG_'}]},
#  u'favorite_count': 0,
#  u'favorited': False,
#  u'filter_level': u'low',
#  u'geo': None,
#  u'id': 668643084985610240,
#  u'id_str': u'668643084985610240',
#  u'in_reply_to_screen_name': u'RugbyBaaBaas',
#  u'in_reply_to_status_id': 668310781876899840,
#  u'in_reply_to_status_id_str': u'668310781876899840',
#  u'in_reply_to_user_id': 880357369,
#  u'in_reply_to_user_id_str': u'880357369',
#  u'is_quote_status': False,
#  u'lang': u'en',
#  u'place': None,
#  u'retweet_count': 0,
#  u'retweeted': False,
#  u'source': u'<a href="http://twitter.com/download/android" rel="nofollow">Twitter for Android</a>',
#  u'text': u"@RugbyBaaBaas @RugbyARG_ they're going to rock Super Rugby!",
#  u'timestamp_ms': u'1448251908905',
#  u'truncated': False,
#  u'user': {u'contributors_enabled': False,
#            u'created_at': u'Fri Jul 04 10:29:10 +0000 2014',
#            u'default_profile': True,
#            u'default_profile_image': False,
#            u'description': u'South Coast born and raised, now living in Secunda and working in Finance. Passionate about rugby. Amateur rugby scribe.',
#            u'favourites_count': 2,
#            u'follow_request_sent': None,
#            u'followers_count': 183,
#            u'following': None,
#            u'friends_count': 800,
#            u'geo_enabled': False,
#            u'id': 2603203266,
#            u'id_str': u'2603203266',
#            u'is_translator': False,
#            u'lang': u'en',
#            u'listed_count': 3,
#            u'location': u'Secunda',
#            u'name': u'Stephen Smith',
#            u'notifications': None,
#            u'profile_background_color': u'C0DEED',
#            u'profile_background_image_url': u'http://abs.twimg.com/images/themes/theme1/bg.png',
#            u'profile_background_image_url_https': u'https://abs.twimg.com/images/themes/theme1/bg.png',
#            u'profile_background_tile': False,
#            u'profile_banner_url': u'https://pbs.twimg.com/profile_banners/2603203266/1440152262',
#            u'profile_image_url': u'http://pbs.twimg.com/profile_images/570155019274424320/1d8TGtF9_normal.jpeg',
#            u'profile_image_url_https': u'https://pbs.twimg.com/profile_images/570155019274424320/1d8TGtF9_normal.jpeg',
#            u'profile_link_color': u'0084B4',
#            u'profile_sidebar_border_color': u'C0DEED',
#            u'profile_sidebar_fill_color': u'DDEEF6',
#            u'profile_text_color': u'333333',
#            u'profile_use_background_image': True,
#            u'protected': False,
#            u'screen_name': u'StevePaul84',
#            u'statuses_count': 291,
#            u'time_zone': None,
#            u'url': None,
#            u'utc_offset': None,
#            u'verified': False}}
