#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import telebot

TOKEN = '765047482:AAEvSaWe7etm7vP6kxQu03WRY9zNycb7Pcc'
NOTICE_TOKEN = '704593545:AAGEauPlgPergENS6915Ke8W980fJ-SNo8M'
USER_ID = 480588693

bot = telebot.TeleBot(TOKEN)
bot2 = telebot.TeleBot(NOTICE_TOKEN)
chat_id = USER_ID

def check_user_id(message):
    if(message.chat.id == chat_id):
        return True
    else:
        return False

@bot.message_handler(commands=['help'])
def send_welcome(message):
    bot.send_message(reply_to_message_id=message.message_id, chat_id=message.chat.id, text='这是一个私人服务器通知专用机器人')

@bot.message_handler(commands=['new'])
def send_tg_message_now(message):
    if check_user_id(message):
        from custom_strategy import CustomOrderManager
        CustomOrderManager.send_tg_message()
    else:
        print('Unauthenticated ID')
        bot.send_message(reply_to_message_id=message.message_id, chat_id=message.chat.id, text='您没有操作权限')


@bot2.message_handler(commands=['start_market_maker'])
def start_market_maker(message):
    if check_user_id(message):
        bot2.send_message(reply_to_message_id=message.message_id, chat_id=message.chat.id, text='不支持从telegram启动，请远程登陆服务器执行marketmaker_custom XBTUSD')
    else:
        bot2.send_message(reply_to_message_id=message.message_id, chat_id=message.chat.id, text='您没有操作权限')
        
@bot2.message_handler(commands=['stop_market_maker'])
def stop_market_maker(message):
    if check_user_id(message):
        from custom_strategy import CustomOrderManager
        CustomOrderManager.exit()
    else:
        bot2.send_message(reply_to_message_id=message.message_id, chat_id=message.chat.id, text='您没有操作权限')

def run_polling():
    bot.polling()

if __name__ == '__main__':
    bot.polling()