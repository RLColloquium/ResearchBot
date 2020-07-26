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
import urllib
from functools import lru_cache
# from pprint import PrettyPrinter
import requests
from flask import Flask, request
from slackeventsapi import SlackEventAdapter
from slack import WebClient
from arxiv import query

# import translator # for another translation api


TRANSLATE_TEXT_CACHE_MAXSIZE = 512 # TODO: optimize memory vs hits rate

# pp = PrettyPrinter()

app = Flask(__name__)

@app.route('/')
def hello():
    return 'Hello!'


slack_events_adapter = SlackEventAdapter(os.environ['SLACK_SIGNING_SECRET'], '/slack/events', app)
slack_client = WebClient(os.environ['SLACK_BOT_TOKEN'])


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

@lru_cache(maxsize=TRANSLATE_TEXT_CACHE_MAXSIZE)
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
        print('translate_deepl_api: {:.5f} sec'.format(time.time() - start))
        return j['translations'][0]['text'] if 'translations' in j else None # TODO: need more check?
    else:
        print('Failed to translate: {}'.format(r.text))
        return None

def generate_response(r):
    arxiv_id = re.sub(r'https?://arxiv.org/abs/([0-9v\.]+)', r'\1', r['id'])
    arxiv_id_no_v = re.sub(r'v[0-9]+$', r'', arxiv_id)
    vanity_url = 'https://www.arxiv-vanity.com/papers/{}/'.format(arxiv_id_no_v)
    tweets_url = 'https://twitter.com/search?q=arxiv.org%2Fabs%2F{}&f=live'.format(arxiv_id_no_v)
    tags = ' | '.join(['{}'.format(t['term']) for t in r['tags']])
    u = r['updated_parsed']
    p = r['published_parsed']
    date = '{:04d}/{:02d}/{:02d}, {:04d}/{:02d}/{:02d}'.format(p.tm_year, p.tm_mon, p.tm_mday, u.tm_year, u.tm_mon, u.tm_mday)
    comment = r['arxiv_comment'] or ''
    vanity = '<{}|vanity>'.format(vanity_url)
    tweets = '<{}|{}>'.format(tweets_url, 'tweets')
    summary = re.sub(r'\n', r' ', r['summary'])
    translation = translate_text(summary)
    summary = translation if translation and len(translation) > 0 else summary
    # summary = '\n'.join([translation, summary])
    lines = [
        # r['id'],
        re.sub(r'\n', r' ', r['title']),
        ', '.join(r['authors']),
        ', '.join([date, vanity, tweets, tags, comment]),
        summary
    ]
    return '\n'.join(lines)

def is_retry_request(request):
    return request.headers.get('X-Slack-Retry-Num') and request.headers.get('X-Slack-Retry-Reason') == 'http_timeout'

def is_user(e):
    return e.get('user') and e.get('bot_id') is None

def get_arxiv_id(text):
    m = re.search(r'https?://arxiv\.org/(abs|pdf)/([0-9v\.]+)(\.pdf)?', text)
    if m and m.group(2):
        return m.group(2)
    #m = re.search(r'https?://arxiv\.org/pdf/([0-9v\.]+).pdf', text)
    #if m and m.group(1):
    #    return m.group(1)
    return None


@slack_events_adapter.on('message')
def handle_message(event):
    if is_retry_request(request): # flask.request
        # TODO: use asyncio or multithread or whatever to avoid retry events. it needs to respond "200 OK" within 3 seconds.
        # print('Slack Events API retry event: ignored')
        return
    e = event['event']
    if not is_user(e): # bot
        # print('Bot message: ignored')
        return
    arxiv_id = get_arxiv_id(e['text'])
    if arxiv_id is None: # not arXiv url
        # print('Not arXiv URL: ignored')
        return
    rs = query(id_list=[arxiv_id])
    if rs and len(rs) > 0:
        global user_id # set user_id global for effective lru_cache. see translate_text.
        user_id = e['user']
        text = generate_response(rs[0]) # generate_response(rs[0], user_id)
    else:
        text = 'No result found: {}'.format(arxiv_id)
    slack_client.chat_postMessage(channel=e['channel'], text=text, thread_ts=e['ts'])

@slack_events_adapter.on('error')
def handle_error(err):
    print('Error: {}'.format(str(err)))


if __name__ == '__main__':
    port = os.getenv('PORT') or 3000
    app.run(port=port)
