from vaderSentiment.vaderSentiment import sentiment as vaderSentiment
from twitter import TwitterStream, OAuth
import redis
import re
import time
import json
import threading
import logging
import os
import sys

# Work queue to check for new requests
WORK_QUEUE = os.getenv('REQUEST_QUEUE', 'request_queue')

# How long to wait before reconnecting with the same credentials
RECONNECT_SLEEP = os.getenv('RECONNECT_SLEEP', 15)

# How often to check for new keyword from same user
KEYWORD_CHECK_INTERVAL = os.getenv('KEYWORD_CHECK_INTERVAL', 2)

# Don't store more than last 500 tweets in each queue
MAX_TWEETS = os.getenv('MAX_TWEETS', 500)

# Max times the tweet queue can become full consecutively after which we terminate the thread
MAX_CONSECUTIVE_FULL = os.getenv('MAX_CONSECUTIVE_FULL', 5)

# If queue has not been filled in this many seconds it will expire..
# There is a race here I have to fix..
QUEUE_EXPIRE_SECONDS = os.getenv('QUEUE_EXPIRE_SECONDS', 7200)

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format='%(asctime)s:%(module)s:%(levelname)s:(%(threadName)-9s) %(message)s')
logging.Formatter.converter = time.gmtime

class Worker(threading.Thread):
    """
    This thread will do the work pulling the twitter stream. Multple of these can be spawn.
    """
    def __init__(self, r, keyword, uuid, dosleep=0):
        threading.Thread.__init__(self)
        self.r = r
        self.keyword = keyword
        self.uuid = uuid
        self.kcheckint = KEYWORD_CHECK_INTERVAL
        self.dosleep = dosleep

    def run(self):

        logging.debug('Starting thread with paramaters dosleep {} keyword {} uuid {} kcheckint {}'.format(self.dosleep, self.keyword, self.uuid, self.kcheckint))

        if (self.dosleep > 0):
            time.sleep(self.dosleep)

        # Twitter queue
        tqueue = "twitter:" + self.uuid
        kqueue = "keyword:" + self.uuid

        # Initialize the queue and set expiration to 1 hour
        self.r.rpush(tqueue, json.dumps({}))
        self.r.expire(tqueue, QUEUE_EXPIRE_SECONDS)

        # Persist keyword for this uuid
        r.set("keyword:" + self.uuid, self.keyword)

        # Get twitter credentials for this uuid
        res = json.loads(self.r.get("creds:" + self.uuid))
        consumer_key = res["consumer_key"]
        consumer_secret = res["consumer_secret"]
        access_token = res["oauth_token"]
        access_secret = res["oauth_token_secret"]

        # Instantiate twitter object
        t = TwitterStream(auth=OAuth(access_token, access_secret, consumer_key, consumer_secret), timeout=self.kcheckint)
        tweets = t.statuses.filter(track=self.keyword, language="en")

        # Set timers
        ctime = time.time()
        rtime = ctime

        tweet_counter = 0
        consecutive_full = 0

        for tweet in tweets:
            rtime = time.time()

            # Non-blocking stream did not receive any tweets within timeout period or keyword check timer has expired
            if "timeout" in tweet or (rtime - ctime) > self.kcheckint:
                logging.debug("uuid {} keyword {} timer expired".format(self.uuid, self.keyword))
                # Look for new keyword and disconnect if new
                nkeyword = self.r.get(kqueue)
                if nkeyword is not None and self.keyword != nkeyword:
                    logging.debug("uuid {} got new keyword old {} new {} ".format(self.uuid, self.keyword, nkeyword))
                    self.keyword = nkeyword
                    break
                # Keyword has not changed. Reset check time
                ctime = time.time()
                continue

            # Not a tweet we care about
            if "text" not in tweet:
                continue

            message = tweet["text"]
            name = tweet["user"]["name"]
            location = tweet["user"]["location"]
            description = tweet["user"]["description"]
            screen_name = tweet["user"]["screen_name"]
            time_zone = tweet["user"]["time_zone"]
            favorite_count = tweet["favorite_count"]
            retweet_count = tweet["retweet_count"]
            followers_count = tweet["user"]["followers_count"]
            timestamp_ms = tweet["timestamp_ms"]
            profile_image_url = tweet["user"]["profile_image_url"]

            logging.debug("uuid %s raw tweet %s" % (self.uuid, str(tweet)))

            clean_message = " ".join(re.sub("(@[A-Za-z0-9]+)|(^RT )|(https?://\S+)"," ",message).split())
            vaderRes = vaderSentiment(clean_message.encode("utf-8"))
            sentiment = round(vaderRes["pos"] - vaderRes["neg"], 2)

            obj = {
                "sentiment": sentiment,
                "name" : name,
                "location": location,
                "description": description,
                "time_zone": time_zone,
                "screen_name": screen_name,
                "message": message,
                "favorite_count": favorite_count,
                "retweet_count": retweet_count,
                "followers_count": followers_count,
                "timestamp_ms": timestamp_ms,
                "profile_image_url": profile_image_url
            }
            # Add tweet with sentiment to uuid's twitter queue
            tweet_counter += 1
            self.r.rpush(tqueue, json.dumps(obj))

            # Now we detect if the queue is constantly filling.. If it is it means the client is not pulling from queue
            if tweet_counter % 10 == True:
                queue_len = self.r.llen(tqueue)
                if queue_len >= MAX_TWEETS:
                    consecutive_full += 1
                    keep_range = queue_len - MAX_TWEETS
                    self.r.ltrim(tqueue, keep_range, queue_len-1)
                    tweet_counter = 0
                    logging.debug("uuid {} consecutive full is now +1 to {}", uuid, consecutive_full)
                else:
                    consecutive_full -= 1
                    if (consecutive_full < 0):
                        consecutive_full = 0
                    self.r.expire(tqueue, QUEUE_EXPIRE_SECONDS)
                    logging.debug("uuid {} consecutive full is now -1 to {}", uuid, consecutive_full)


            # Check how long the queue is. If it is at MAX_TWEETS 5 times in a row the client is probably gone so
            # terminate
            if consecutive_full >= MAX_CONSECUTIVE_FULL:
                logging.debug("uuid {} reached consecutive full..", uuid)
                break


        # Cleanup if we can
        self.r.delete(tqueue)
        logging.debug("uuid {} disconnected..".format(uuid))

if __name__ == "__main__":

    # CF stuff
    vcap_services = os.getenv("VCAP_SERVICES")
    vs = json.loads(vcap_services)
    redis_hostname = vs["rediscloud"][0]["credentials"]["hostname"]
    redis_password = vs["rediscloud"][0]["credentials"]["password"]
    redis_port = vs["rediscloud"][0]["credentials"]["port"]

    logging.debug("Connecting to redis at {} on port {}".format(redis_hostname, redis_port))

    # Initialize handle to Redis
    r = redis.Redis(host=redis_hostname, port=redis_port, password=redis_password)

    uuids = []
    workers = []

    while True:
        logging.debug("Blocking on {} for a request".format(WORK_QUEUE))

        # Look for requests on the request queue. This will block
        work = r.brpop(WORK_QUEUE)[1]
        if (work is None):
            continue

        logging.debug(str(work))
        res = json.loads(work)

        # Check its a valid entry
        if "keyword" not in res or "uuid" not in res:
            logging.debug("Received a malformed request that will be ignored: {}".format(str(res)))
            continue

        uuid = res["uuid"]
        keyword = res["keyword"]

        logging.debug("Got keyword %s with uuid %s from request queue".format(keyword, uuid))

        # If we have a thread for this uuid already sleep for amount of time before connecting
        dosleep = 0
        for i in uuids:
            if i == uuid:
                dosleep = RECONNECT_SLEEP
                break

        # Spawn a worker thread to deal with this request
        t = Worker(r, keyword, uuid, dosleep)
        workers.append(t)
        uuids.append(uuid)
        workerid = threading.activeCount()
        logging.debug("Starting worker for uuid {} with id {}".format(uuid, workerid))
        r.set("keyword:" + uuid, keyword)
        t.setDaemon(True)
        t.start()

        # Cleanup any completed threads and uuid references
        alive_threads = []
        alive_uuids = []
        for i, t in enumerate(workers):
            if t.isAlive():
                alive_threads.append(t)
                alive_uuids.append(uuids[i])
                logging.debug("uuid {} thread alive".format(uuids[i]))
            else:
                logging.debug("uuid {} thread dead".format(uuids[i]))

        workers = alive_threads
        uuids = alive_uuids


