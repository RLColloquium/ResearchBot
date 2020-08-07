# -*- coding: utf-8 -*-

# Copyright 2020 Susumu OTA
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import time
import json
import urllib
from operator import attrgetter
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
# from pprint import PrettyPrinter
import requests
import pandas as pd
import numpy as np
from flask import Flask, request
from slackeventsapi import SlackEventAdapter
from slack import WebClient
import arxiv
import tweepy

# import translator # for another translation api


# pp = PrettyPrinter()

app = Flask(__name__)

@app.route('/')
def hello():
    return 'Hello!'


slack_events_adapter = SlackEventAdapter(os.environ['SLACK_SIGNING_SECRET'], '/slack/events', app)
slack_client = WebClient(os.environ['SLACK_BOT_TOKEN'])

def get_twitter_api():
    if os.getenv('TWITTER_API_KEY') and os.getenv('TWITTER_API_SECRET_KEY'):
        auth = tweepy.AppAuthHandler(os.environ['TWITTER_API_KEY'], os.environ['TWITTER_API_SECRET_KEY'])
        return tweepy.API(auth, wait_on_rate_limit=True, wait_on_rate_limit_notify=True)
    else:
        return None

twitter_api = get_twitter_api()

def is_valid_slack_user_id(user_id):
    # https://github.com/slackapi/slack-api-specs/blob/master/web-api/slack_web_openapi_v2.json
    # "defs_user_id": { "pattern": "^[UW][A-Z0-9]{2,}$", ... }
    return re.match(r'^[UW][A-Z0-9]{2,10}$', user_id) # TODO: upper limit seems like 10 but not sure

def get_deepl_auth_key(user_id):
    if is_valid_slack_user_id(user_id) and os.getenv('DEEPL_AUTH_KEY_{}'.format(user_id)):
        # print('Found a user specific deepl auth key: {}'.format(user_id))
        return os.getenv('DEEPL_AUTH_KEY_{}'.format(user_id)) # user specific auth key
    else:
        return os.getenv('DEEPL_AUTH_KEY') # default auth key
        # return None # or just reject

@lru_cache(maxsize=128)
def translate_text(text, target_lang='JA'): # drop user_id to increase cache hits rate
    global user_id # set user_id global for effective lru_cache. see handle_message.
    deepl_auth_key = get_deepl_auth_key(user_id)
    if deepl_auth_key:
        return translate_deepl_api(text, deepl_auth_key, target_lang=target_lang)
    else:
        # return translator.translate_another_api(text, target_lang=target_lang) # for another translation api
        return None

def translate_deepl_api(text, auth_key, target_lang='JA'):
    # https://www.deepl.com/docs-api/translating-text/
    start = time.time()
    params = {
        'auth_key': auth_key,
        'text': text,
        'target_lang': target_lang
    }
    r = requests.post('https://api.deepl.com/v2/translate', data=params)
    if r.status_code == requests.codes.ok:
        j = r.json()
        # pp.pprint(j)
        print('translate_deepl_api: {:.6f} sec'.format(time.time() - start))
        return j['translations'][0]['text'] if 'translations' in j else None # TODO: need more check?
    else:
        print('Failed to translate: {}'.format(r.text))
        return None

def generate_response(r):
    arxiv_id = get_arxiv_id(r['id'])
    arxiv_id_no_v = get_arxiv_id_no_v(arxiv_id)
    vanity_url = 'https://www.arxiv-vanity.com/papers/{}/'.format(arxiv_id_no_v)
    tweets_url = 'https://twitter.com/search?q=arxiv.org%2Fabs%2F{}%20OR%20arxiv.org%2Fpdf%2F{}.pdf%20&f=live'.format(arxiv_id_no_v, arxiv_id_no_v)
    tags = ' | '.join(['{}'.format(t['term']) for t in r['tags']])
    u = r['updated_parsed']
    p = r['published_parsed']
    date = '{:04d}/{:02d}/{:02d}, {:04d}/{:02d}/{:02d}'.format(p.tm_year, p.tm_mon, p.tm_mday, u.tm_year, u.tm_mon, u.tm_mday)
    comment = r['arxiv_comment'] or ''
    vanity = '<{}|vanity>'.format(vanity_url)
    tweets = '<{}|{} tweets>'.format(tweets_url, r['num_tweets'] if 'num_tweets' in r else '?')
    summary = re.sub(r'\n', r' ', r['summary'])
    translation = translate_text(summary)
    summary = translation if translation and len(translation) > 0 else summary
    # summary = '\n'.join([translation, summary])
    lines = [
        r['id'],
        re.sub(r'\n', r' ', r['title']),
        ', '.join(r['authors']),
        ', '.join([date, vanity, tweets, tags, comment]),
        summary
    ]
    return '\n'.join(lines)

def is_retry_request(request): # flask.request
    return request.headers.get('X-Slack-Retry-Num') and request.headers.get('X-Slack-Retry-Reason') == 'http_timeout'

def is_user(e):
    return e.get('user') and e.get('bot_id') is None

def get_arxiv_id(text):
    m = re.search(r'https?://arxiv\.org/(abs|pdf)/([0-9]+\.[0-9v]+)(\.pdf)?', text)
    return m.group(2) if m and m.group(2) else None

def find_all_unique_arxiv_ids(text):
    m = re.findall(r'https?://arxiv\.org/(abs|pdf)/([0-9]+\.[0-9v]+)(\.pdf)?', text)
    return list(set([get_arxiv_id_no_v(item[1]) for item in m])) if m else []

def get_arxiv_id_no_v(arxiv_id):
    return re.sub(r'v[0-9]+$', r'', arxiv_id)

def handle_arxiv_url(e):
    arxiv_id = get_arxiv_id(e['text']) # arxiv_id was already checked so it should not be None
    # rs = arxiv.query(id_list=[arxiv_id])
    rs = arxiv_query(id_list_str=list_to_str([arxiv_id]))
    if rs and len(rs) > 0:
        r = rs[0]
        arxiv_id_no_v = get_arxiv_id_no_v(arxiv_id)
        arxiv_id_counts = get_tweeted_arxiv_id_counts('"arxiv.org/abs/{}" OR "arxiv.org/pdf/{}.pdf"'.format(arxiv_id_no_v, arxiv_id_no_v))
        r['arxiv_id_no_v'] = arxiv_id_no_v
        r['num_tweets'] = 0 if len(arxiv_id_counts) < 1 else arxiv_id_counts[0]
        text = generate_response(r) # generate_response(r, user_id)
    else:
        text = 'No result found: {}'.format(arxiv_id)
    slack_client.chat_postMessage(channel=e['channel'], text=text, thread_ts=e['ts'])


def get_toptweets_args(text, default_max_results=5):
    m = re.match(r'^toptweets(\s+([0-9]+))?$', text)
    return (int(m.group(2)) if m.group(2) else default_max_results) if m else None

@lru_cache(maxsize=128)
def get_tweeted_arxiv_id_counts(q):
    start = time.time()
    i = 0
    arxiv_ids = []
    try:
        # https://developer.twitter.com/en/docs/basics/rate-limits
        for status in tweepy.Cursor(twitter_api.search, q=q, count=100, result_type='recent', tweet_mode='extended').items(100*100):
            ids = find_all_unique_arxiv_ids(str(status._json)) # TODO: _json is a private member
            arxiv_ids.extend(ids)
            print(i) if i % 100 == 0 else None
            i += 1
    except Exception as e:
        print('Exception: {}'.format(str(e)))
    df = pd.DataFrame(arxiv_ids, columns=['arxiv_id'])
    counts = df['arxiv_id'].value_counts()
    print(len(df), len(counts))
    print('get_tweeted_arxiv_id_counts: {:.6f} sec'.format(time.time() - start))
    return counts

def list_to_str(lst): # just a tiny hack to enable lru_cache for arxiv_query
    return json.dumps(lst)

def str_to_list(s): # just a tiny hack to enable lru_cache for arxiv_query
    return json.loads(s)

@lru_cache(maxsize=128)
def arxiv_query(id_list_str='', q='', max_chunk_id_list=200): # list is unhashable, so it needs to convert from list to string to enable lru_cache
    start = time.time()
    id_list = str_to_list(id_list_str)
    rs = []
    cdr = id_list
    try:
        for i in range(1+len(id_list)//max_chunk_id_list): # avoid "HTTP Error 414 in query" (URI Too Long)
            car = cdr[:max_chunk_id_list]
            cdr = cdr[max_chunk_id_list:]
            print(len(car), len(''.join(car)))
            r = arxiv.query(id_list=car, query=q) # this will automatically sleep
            rs.extend(r)
    except Exception as e:
        print('Exception: {}'.format(str(e)))
    print('arxiv_query: {:.6f} sec'.format(time.time() - start))
    return rs

def handle_popular_arxiv(e):
    max_results = get_toptweets_args(e['text'], default_max_results=5)
    max_results = 10 if max_results > 10 else max_results
    max_results = 1 if max_results < 1 else max_results
    arxiv_id_counts = get_tweeted_arxiv_id_counts('"arxiv.org"')
    if arxiv_id_counts is None or len(arxiv_id_counts) < 1:
        text = 'No twitter result found'
        slack_client.chat_postMessage(channel=e['channel'], text=text, thread_ts=e['ts'])
        return
    id_list = arxiv_id_counts.keys().tolist()
    q = 'cat:cs.CV OR cat:cs.AI OR cat:cs.LG OR cat:cs.CL OR cat:cs.NE OR cat:stat.ML'
    rs = arxiv_query(id_list_str=list_to_str(id_list), q=q, max_chunk_id_list=200)
    print(len(id_list), len(rs))
    if len(rs) < 1:
        text = 'No arXiv result found'
        slack_client.chat_postMessage(channel=e['channel'], text=text, thread_ts=e['ts'])
        return
    i = 0
    for r in rs:
        arxiv_id = get_arxiv_id(r['id'])
        arxiv_id_no_v = get_arxiv_id_no_v(arxiv_id)
        r['arxiv_id_no_v'] = arxiv_id_no_v
        r['num_tweets'] = arxiv_id_counts[arxiv_id_no_v] if arxiv_id_no_v in arxiv_id_counts else 0
        print(i, r['arxiv_id_no_v'], r['num_tweets']) if r['num_tweets'] == 0 else None
        i += 1
    rs.sort(key=attrgetter('num_tweets'), reverse=True)
    rs = rs[:max_results]
    for r in rs:
        text = generate_response(r)
        slack_client.chat_postMessage(channel=e['channel'], text=text, thread_ts=e['ts'])


@slack_events_adapter.on('message')
def handle_message(event):
    if is_retry_request(request): # flask.request
        # Slack Events API needs to respond "200 OK" within 3 seconds.
        # if it came here, previous request should not be handled properly.
        print('Retry event request')
        # return
    e = event['event']
    if not is_user(e):
        # print('Bot message: ignored')
        return
    global user_id # set user_id global for effective lru_cache. see translate_text.
    user_id = e['user']
    if get_arxiv_id(e['text']):
        executor = ThreadPoolExecutor()
        future = executor.submit(handle_arxiv_url, e) # non blocking
        executor.shutdown(wait=False) # non blocking
        return
    if get_toptweets_args(e['text']):
        executor = ThreadPoolExecutor()
        future = executor.submit(handle_popular_arxiv, e) # non blocking
        executor.shutdown(wait=False) # non blocking
        return
    # print('No keyword: ignored')
    return

@slack_events_adapter.on('error')
def handle_error(err):
    print('Error: {}'.format(str(err)))


if __name__ == '__main__':
    app.run(port=int(os.getenv('PORT') or 3000))
