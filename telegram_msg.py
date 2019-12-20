#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import telegram
import json
import time
from datetime import datetime
from telegram.error import (TelegramError, Unauthorized, BadRequest, TimedOut, ChatMigrated, NetworkError, RetryAfter)

TOKEN = '765047482:AAEvSaWe7etm7vP6kxQu03WRY9zNycb7Pcc'
NOTICE_TOKEN = '704593545:AAGEauPlgPergENS6915Ke8W980fJ-SNo8M'
RAILGUN_TOKEN = '865990567:AAGu1LVZk017G-VqoQptZl6xBSJXSdDcXx8'
USER_ID = 480588693

bot = telegram.Bot(token=TOKEN)
bot2 = telegram.Bot(token=NOTICE_TOKEN)
bot3 = telegram.Bot(token=RAILGUN_TOKEN)
chat_id = USER_ID
last_message_id = 0

def tg_send_message(content):
    """发送给telegram自己消息"""
    try:
        bot.send_message(chat_id=chat_id, text=content)
    except TimedOut as e:
        time.sleep(5)
        bot.send_message(chat_id=chat_id, text=content)
    except RetryAfter as e:
        time.sleep(5)
        bot.send_message(chat_id=chat_id, text=content)

def tg_send_railgun_message(content):
    """发送给telegram自己消息"""
    try:
        bot3.send_message(chat_id=chat_id, text=content)
    except TimedOut as e:
        time.sleep(5)
        bot3.send_message(chat_id=chat_id, text=content)
    except RetryAfter as e:
        time.sleep(5)
        bot3.send_message(chat_id=chat_id, text=content)

def tg_send_important_message(content):
    """发送给telegram自己消息"""
    try:
        bot2.send_message(chat_id=chat_id, text=content)
    except TimedOut as e:
        time.sleep(5)
        bot2.send_message(chat_id=chat_id, text=content)
    except RetryAfter as e:
        time.sleep(5)
        bot2.send_message(chat_id=chat_id, text=content)

def tg_get_updates():
    """接收bot消息"""
    try:
        tg_date = bot.get_updates(offset = -1)
    except TimedOut as e:
        time.sleep(5)
        tg_date = bot.get_updates(offset = -1)
    except RetryAfter as e:
        time.sleep(5)
        tg_date = bot.get_updates(offset = -1)
    if len(tg_date) == 0:
        return None
    temp_chat_id = tg_date[-1]['message']['chat']['id']
    message = tg_date[-1]['message']['text']
    message_date = tg_date[-1]['message']['date'].timestamp()
    message_id = tg_date[-1]['message']['message_id']
    global last_message_id

    if(temp_chat_id != chat_id):
        return None
    if (abs(time.time() - message_date) < 6 and last_message_id != message_id):     #5秒内的消息才会处理
        last_message_id = message_id
        return message
    else:
        return None

def tg_get_railgun_updates():
    """接收bot3消息"""
    try:
        tg_date = bot3.get_updates(offset = -1)
    except TimedOut as e:
        time.sleep(5)
        tg_date = bot3.get_updates(offset = -1)
    except RetryAfter as e:
        time.sleep(5)
        tg_date = bot3.get_updates(offset = -1)
    if len(tg_date) == 0:
        return None
    temp_chat_id = tg_date[-1]['message']['chat']['id']
    message = tg_date[-1]['message']['text']
    message_date = tg_date[-1]['message']['date'].timestamp()
    message_id = tg_date[-1]['message']['message_id']
    global last_message_id

    if(temp_chat_id != chat_id):
        return None
    if (abs(time.time() - message_date) < 6 and last_message_id != message_id):     #5秒内的消息才会处理
        last_message_id = message_id
        return message
    else:
        return None

def tg_get_important_updates():
    """接收bot2重要消息"""
    try:
        tg_date = bot2.get_updates(offset = -1)
    except TimedOut as e:
        time.sleep(5)
        tg_date = bot2.get_updates(offset = -1)
    except RetryAfter as e:
        time.sleep(5)
        tg_date = bot2.get_updates(offset = -1)
    if len(tg_date) == 0:
        return None
    temp_chat_id = tg_date[-1]['message']['chat']['id']
    message = tg_date[-1]['message']['text']
    message_date = tg_date[-1]['message']['date'].timestamp()
    message_id = tg_date[-1]['message']['message_id']
    global last_message_id

    if(temp_chat_id != chat_id):
        return None
    if (abs(time.time() - message_date) < 20 and last_message_id != message_id):     #20秒内的消息才会处理
        last_message_id = message_id
        return message
    else:
        return None