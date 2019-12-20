# -*- coding:utf-8 -*-
import sys
from os.path import getmtime
import logging
import requests
from time import sleep
import datetime
import schedule
import re
import numpy as np

from market_maker.market_maker import OrderManager, XBt_to_XBT
from market_maker.settings import settings
from market_maker.utils import log, constants, errors, math
from telegram_msg import tg_send_message, tg_send_important_message, tg_get_updates, tg_get_important_updates

# Used for reloading the bot - saves modified times of key files
import os
watched_files_mtimes = [(f, getmtime(f)) for f in settings.WATCHED_FILES]

STOP_SIZE = 70
START_SIZE_MAGNIFICATION = 100

#
# Helpers
#
logger = logging.getLogger('root')

class CustomOrderManager(OrderManager):

    def reset(self):
        self.exchange.cancel_all_orders()
        self.sanity_check()
        self.print_status()
        self.position_grade = 0
        self.last_running_qty = 0
        self.market_tag = 0                #大波动状态时记录仓位信息:0没有仓位, 1多仓, 2空仓
        self.market_data_test_last_wave_coefficient = 0
        self.reset = True                   #设置初始化标记, 买卖单都变化
        self.restart_flag = False           #设置再循环标记, 只有True时才可以重新建仓, 否则等待
        self.suspend_trading_flag = False   #波动过大时, 设置取消买卖单标记, 用于重新建仓的标志
        self.over_wave_coefficient = False  #波动过大时, 设置标记判断是否需要调用tg_send_message()
        self.stop_order_price = None        #止损触发价格
        self.stop_market_maker_flag = False     #暂停所有交易, 取消平仓及止损以外所有挂单
        self.cancel_all_orders_flag = False     #取消所有挂单, 并暂停交易
        self.clear_position_flag = False        #清空所有仓位, 并暂停交易
        self.pin_buy_orders = []
        self.pin_sell_orders = []
        self.last10price_flag = False
        self.last10price_countdown = 60
        #计算插针建仓倒数, 超过60秒撤销挂单
        self.cycleclock = 30 // settings.LOOP_INTERVAL
        #仓位等级由0-6级, 按持仓量分级, 每大于order size增加1级, 最高6级
        #持仓方向通过self.running_qty来判断, 大于0为多仓, 小于0为空仓
        schedule.every().day.at("00:00").do(self.write_mybalance) #每天00:00执行一次
        schedule.every(5).seconds.do(self.set_MarkPriceList) #每5秒执行一次
        schedule.every().second.do(self.set_Last10PriceList) #每1秒执行一次
        schedule.every(5).seconds.do(self.check_tg_message) #每5秒执行一次检查来自telegram的消息
        schedule.every(5).seconds.do(self.check_double_order) #每5秒执行一次检测是否有重复挂单,发现立即删除
        self.MarkPriceList = []
        marketPrice = self.exchange.get_portfolio()['XBTUSD']['markPrice']
        self.LastPriceList10second = []
        self.MarkPriceList30min = []
        lastPrice = self.get_ticker()['last']
        for x in range(120):
            self.MarkPriceList.append(marketPrice)
        for x in range(10):
            self.LastPriceList10second.append(lastPrice)
        for x in range(360):
            self.MarkPriceList30min.append(lastPrice)
        # Create orders and converge.
        with open(r'/root/mybalance.txt', 'r') as f:
            lines = f.readlines()
            m1 = re.match(r'(\d{4}-\d{2}-\d{2})\s(\d{2}\:\d{2}\:\d{2})\s+([0-9\.]+)', lines[-1])
            self.yesterday_balance = float(m1.group(3))
            m2 = re.match(r'(\d{4}-\d{2}-\d{2})\s(\d{2}\:\d{2}\:\d{2})\s+([0-9\.]+)', lines[-2])
            self.before_yesterday_balance = float(m2.group(3))
        settings.ORDER_START_SIZE = self.start_XBt // 1000000 * START_SIZE_MAGNIFICATION    #新算法, 每次初始交易重新设定ORDER_START_SIZE
        self.place_orders()

    def write_mybalance(self):
        now = datetime.datetime.now()
        mybalance = '%.6f' % XBt_to_XBT(self.start_XBt)
        with open(r'/root/mybalance.txt', 'a') as f:
            f.write(now.strftime('%Y-%m-%d %H:%M:%S') + '   ' + mybalance + '\n')
        message = 'BitMEX今日交易统计\n' + \
                '时间：' + now.strftime('%Y-%m-%d %H:%M:%S') + '\n' + \
                '保证金余额：' + mybalance + '\n' + \
                '合约数量：' + str(self.running_qty) + '\n' + \
                '开仓价格：' + str(self.exchange.get_position()['avgCostPrice']) + '\n' + \
                '风险等级：' + str(self.position_grade) + '\n' + \
                '最新价格：' + str(self.get_ticker()['last']) + '\n' + \
                '指数价格：' + str(self.exchange.get_portfolio()['XBTUSD']['markPrice']) + '\n' + \
                '今日盈利：' + '%.6f' % (float(mybalance) - self.yesterday_balance) + '\n' + \
                '作日盈利：' + '%.6f' % (self.yesterday_balance - self.before_yesterday_balance)
        tg_send_important_message(message)
        self.before_yesterday_balance = self.yesterday_balance
        self.yesterday_balance = float(mybalance)

    def set_MarkPriceList(self):
        self.MarkPriceList.pop()
        self.MarkPriceList.insert(0, self.exchange.get_portfolio()['XBTUSD']['markPrice'])
        self.MarkPriceList30min.pop()
        self.MarkPriceList30min.insert(0, self.exchange.get_portfolio()['XBTUSD']['markPrice'])
        now = datetime.datetime.now()
        wave_coefficient = self.get_wave_coefficient()
        with open(r'/root/market_BXBT_data.txt', 'a') as f:
            f.write('%s    %s    %.2f\n' % (now.strftime('%Y-%m-%d %H:%M:%S'), self.exchange.get_portfolio()['XBTUSD']['markPrice'], wave_coefficient))

    def set_Last10PriceList(self):
        if (self.last10price_flag == True):
            self.last10price_countdown = self.last10price_countdown - 1
        self.LastPriceList10second.pop()
        self.LastPriceList10second.insert(0, self.get_ticker()['last'])

    def get_wave_coefficient(self):
        """求波动系数, 当前市场波动系数, 超过一定值取消挂单"""
        if (np.mean(self.MarkPriceList) > self.MarkPriceList[0]):      #10分钟内平均指数大于最新指数,下跌,返回负值
            return (min(self.MarkPriceList) - max(self.MarkPriceList))
        elif (np.mean(self.MarkPriceList) < self.MarkPriceList[0]):     #10分钟内平均指数小于最新指数,上涨,返回正值
            return (max(self.MarkPriceList) - min(self.MarkPriceList))
        else:
            return 0

    def get_wave_coefficient_1min(self):
        if (np.mean(self.MarkPriceList[0:12]) > self.MarkPriceList[0]):      #1分钟内平均指数大于最新指数,下跌,返回负值
            return (min(self.MarkPriceList[0:12]) - max(self.MarkPriceList[0:12]))
        elif (np.mean(self.MarkPriceList[0:12]) < self.MarkPriceList[0]):     #1分钟内平均指数小于最新指数,上涨,返回正值
            return (max(self.MarkPriceList[0:12]) - min(self.MarkPriceList[0:12]))
        else:
            return 0

    def get_wave_coefficient_30min(self):
        """求30分钟波动系数"""
        if (np.mean(self.MarkPriceList) > self.MarkPriceList[0]):      #30分钟内平均指数大于最新指数,下跌,返回负值
            return (min(self.MarkPriceList) - max(self.MarkPriceList))
        elif (np.mean(self.MarkPriceList) < self.MarkPriceList[0]):     #30分钟内平均指数小于最新指数,上涨,返回正值
            return (max(self.MarkPriceList) - min(self.MarkPriceList))
        else:
            return 0

    def get_wave_coefficient_last10price(self):
        """求10秒内最新价波动系数, 正数为上涨, 负数为下跌, 超过一定值插针挂单"""
        if ((sum(self.LastPriceList10second[0:5]) - sum(self.LastPriceList10second[5:10])) > 10 ):
            return (max(self.LastPriceList10second) - min(self.LastPriceList10second))
        elif ((sum(self.LastPriceList10second[0:5]) - sum(self.LastPriceList10second[5:10])) < 10 ):
            return (min(self.LastPriceList10second) - max(self.LastPriceList10second))
        else:
            return 0

    def check_tg_message(self):
        """检查是否有来自telegram的消息,并处理"""
        tg_message = tg_get_updates()
        if (tg_message == None):
            return
        elif (tg_message == '/new'):
            self.send_tg_message()
        elif (tg_message == '/order'):
            self.send_tg_order_message()
        elif (tg_message == '/wave_coefficient'):
            wave_coefficient = self.get_wave_coefficient()
            tg_send_message('wave_coefficient is %.2f now' % wave_coefficient)
        elif (tg_message == '/check_important'):
            ret = self.check_tg_important_message()
            if (ret != None):
                tg_send_message(ret)
            else:
                tg_send_message('未执行命令')
        else:
            return

    def check_tg_important_message(self):
        tg_important_message = tg_get_important_updates()
        if (tg_important_message == None):
            return None
        elif (tg_important_message == '/stop_market_maker'):
            self.stop_market_maker_flag = True
            self.suspend_trading_flag = True
            return '执行stop_market_maker'
        elif (tg_important_message == '/start_market_maker'):
            self.stop_market_maker_flag = False
            self.cancel_all_orders_flag = False
            self.clear_position_flag = False
            return '执行start_market_maker'
        elif (tg_important_message == '/cancel_all_orders'):
            self.cancel_all_orders_flag = True
            self.stop_market_maker_flag = True
            self.clear_position_flag = False
            self.suspend_trading_flag = True
            return '执行cancel_all_orders'
        elif (tg_important_message == '/clear_position'):
            self.clear_position_flag = True
            self.stop_market_maker_flag = True
            self.cancel_all_orders_flag = False
            self.suspend_trading_flag = True
            return '执行clear_position'
        else:
            return None

    def get_position_grade(self):
        """获取仓位等级"""
        self.position_grade = abs(self.running_qty) // settings.ORDER_START_SIZE
        if self.position_grade > 6:
            self.position_grade = 6
        return self.position_grade

    def get_price_offset2(self, index):
        """根据index依次设置每一个价格，这里为差价依次增大，分别为0.5, 1, 2, 3, 5, 7, 11, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120, 125, 130, 135, 140, 145, 150, 155"""
        #L = [0.5, 1, 2, 3, 5, 7, 11, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120, 125, 130, 135, 140, 145, 150, 155]
        L = [1, 3, 7, 13, 21, 29, 37, 45, 53, 61, 69, 77, 85, 93, 101, 109, 117, 125]
        if abs(index) > 20:
            logger.error("ORDER_PAIRS cannot over 5")
            self.exit()
        # Maintain existing spreads for max profit
        if ((index > 0 and self.start_position_sell < self.exchange.get_portfolio()['XBTUSD']['markPrice'] - 20) or (index < 0 and self.start_position_buy > self.exchange.get_portfolio()['XBTUSD']['markPrice'] + 20)):
        #卖单如果小于指数超过20不挂单, 买单如果大于指数超过20不挂单
            return None
        if settings.MAINTAIN_SPREADS:
            start_position = self.start_position_buy if index < 0 else self.start_position_sell
            # First positions (index 1, -1) should start right at start_position, others should branch from there
            index = index + 1 if index < 0 else index - 1
        else:
            # Offset mode: ticker comes from a reference exchange and we define an offset.
            start_position = self.start_position_buy if index < 0 else self.start_position_sell

            # If we're attempting to sell, but our sell price is actually lower than the buy,
            # move over to the sell side.
            if index > 0 and start_position < self.start_position_sell:
                start_position = self.start_position_sell
            # Same for buys.
            if index < 0 and start_position > self.start_position_buy:
                start_position = self.start_position_buy
        if (self.running_qty != 0):
            avgCostPrice = self.exchange.get_position()['avgCostPrice']
            if (avgCostPrice % 1 == 0.5):
                start_position = avgCostPrice
            else:
                start_position = avgCostPrice - 0.25 if index < 0 else avgCostPrice + 0.25
        if index > 0:
            if (start_position + L[index - 1] >= self.start_position_sell):  #卖单小于第一卖价不挂单
                return math.toNearest(start_position + L[index - 1], self.instrument['tickSize'])
            else:
                return None
        if index < 0:
            if (start_position - L[abs(index) - 1] <= self.start_position_buy):   #买单大于第一买价不挂单
                return math.toNearest(start_position - L[abs(index) - 1], self.instrument['tickSize'])
            else:
                return None
        if index == 0:
            return math.toNearest(start_position, self.instrument['tickSize'])

    def get_price_offset3(self, index):
        """按仓位等级来设置价格, 每0.5设置一个价格"""
        avgCostPrice = self.exchange.get_position()['avgCostPrice']
        if (abs(self.running_qty) <= settings.ORDER_START_SIZE):
            interval = 1
        else:
            interval = settings.INTERVAL2
        if (avgCostPrice % 0.5 == 0):
            start_position = avgCostPrice
        else:
            start_position = avgCostPrice - 0.25 if index < 0 else avgCostPrice + 0.25
        if (index > 0 and start_position < self.start_position_sell):
            start_position = self.start_position_sell + interval
        elif (index < 0 and start_position > self.start_position_buy):
            start_position = self.start_position_buy - interval
        elif index > 0:
            start_position = start_position + interval
        elif index < 0:
            start_position = start_position - interval
        if settings.MAINTAIN_SPREADS:
            # First positions (index 1, -1) should start right at start_position, others should branch from there
            index = index + 1 if index < 0 else index - 1
        print('start_position: %s ' % start_position)
        if index > 0:
            return math.toNearest(start_position + index * 0.5, self.instrument['tickSize'])
        if index < 0:
            return math.toNearest(start_position - abs(index) * 0.5, self.instrument['tickSize'])
        if index == 0:
            return math.toNearest(start_position, self.instrument['tickSize'])

    def market_data_test(self):
        """数据收集测试用, 不参与交易"""
        wave_coefficient = self.get_wave_coefficient()
        now = datetime.datetime.now()
        with open(r'/root/market_data_test.txt', 'a') as f:
            if (self.market_tag == 0):       #market_tag, 0没有仓位, 1多仓, 2空仓
                if((0 <= self.market_data_test_last_wave_coefficient < 20 and wave_coefficient > 20) or (self.market_data_test_last_wave_coefficient <= -40 and wave_coefficient > -40)):
                    self.market_tag = 1
                    f.write('%s    buy@%s\n' % (now.strftime('%Y-%m-%d %H:%M:%S'), self.start_position_sell))
                    self.last_data_test_price = self.start_position_sell
                if((-20 < self.market_data_test_last_wave_coefficient <= 0 and wave_coefficient < -20) or (self.market_data_test_last_wave_coefficient >= 40 and wave_coefficient < 40)):
                    self.market_tag = 2
                    f.write('%s    sell@%s\n' % (now.strftime('%Y-%m-%d %H:%M:%S'), self.start_position_buy))
                    self.last_data_test_price = self.start_position_buy
            elif (self.market_tag == 1):
                if ((self.market_data_test_last_wave_coefficient <= 40 and wave_coefficient > 40) or (self.market_data_test_last_wave_coefficient >= 0 and wave_coefficient < 0) or (self.market_data_test_last_wave_coefficient >= 20 and wave_coefficient < 20)):
                    self.market_tag = 0
                    f.write('%s    sell@%s    %f\n' % (now.strftime('%Y-%m-%d %H:%M:%S'), self.start_position_buy, self.start_position_buy-self.last_data_test_price))
            elif (self.market_tag == 2):
                if ((self.market_data_test_last_wave_coefficient >= -40 and wave_coefficient < 40) or (self.market_data_test_last_wave_coefficient <= 0 and wave_coefficient > 0) or (self.market_data_test_last_wave_coefficient <= -20 and wave_coefficient > -20)):
                    self.market_tag = 0
                    f.write('%s    buy@%s    %f\n' % (now.strftime('%Y-%m-%d %H:%M:%S'), self.start_position_sell, self.last_data_test_price-self.start_position_sell))
        self.market_data_test_last_wave_coefficient = wave_coefficient

    def place_orders(self):
        """Create order items for use in convergence."""
        buy_orders = []
        sell_orders = []
        buy_stop_order = {}
        sell_stop_order = {}
        order_status = 0
        """order_status参数说明
            0: running_qty为0, 维持原样
            1: self.running_qty > 0, 买卖都变化, 买单按照offset2, 卖单按照offset3
            2: 买单维持不变, 卖单按照offset3
            3: self.running_qty < 0, 买卖都变化, 买单按照offset3, 卖单按照offset2
            4: 卖单维持不变, 买单按照offset3
            5: 追加指定订单
            6: 取消指定订单
        """
        # Create orders from the outside in. This is intentional - let's say the inner order gets taken;
        # then we match orders from the outside in, ensuring the fewest number of orders are amended and only
        # a new order is created in the inside. If we did it inside-out, all orders would be amended
        # down and a new order would be created at the outside.
        position_grade = self.get_position_grade()
        avgCostPrice = self.exchange.get_position()['avgCostPrice']
        print ('position_grade: %s ' % position_grade)
        print ('running_qty: %s ' % self.running_qty)
        print ('ORDER_START_SIZE: %s ' % settings.ORDER_START_SIZE)
        schedule.run_pending()

        self.market_data_test()

        if (abs(self.last_running_qty) > abs(self.running_qty) and self.running_qty > settings.ORDER_START_SIZE):
            if (self.cycleclock == 30 // settings.LOOP_INTERVAL):
                self.send_tg_message()
            self.cycleclock = self.cycleclock - 1
            print('Countdown: %s' % self.cycleclock)
            if (self.cycleclock == 0):
                self.cycleclock = 30 // settings.LOOP_INTERVAL
            else:
                return
        wave_coefficient = self.get_wave_coefficient()
        print ('wave_coefficient: %s ' % wave_coefficient)
        
        if(self.stop_market_maker_flag == True and self.cancel_all_orders_flag == True):
            if (len(self.exchange.get_orders()) != 0):
                self.exchange.cancel_all_orders()
            logger.info("Cancel all orders")
        elif(self.stop_market_maker_flag == True and self.clear_position_flag == True):
            if(self.running_qty != 0):
                self.clear_position(buy_orders, sell_orders)
            else:
                if (len(self.exchange.get_orders()) != 0):
                    self.exchange.cancel_all_orders()
                logger.info("Market_maker has stopped. No orders, no positions now")
        elif(self.stop_market_maker_flag == True):
            if(self.running_qty > 0):
                if avgCostPrice != None:
                    sell_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice - STOP_SIZE, self.instrument['tickSize']), "Sell")
                order_status = 4
            elif(self.running_qty < 0):
                if avgCostPrice != None:
                    buy_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice + STOP_SIZE, self.instrument['tickSize']), "Buy")
                order_status = 2
            elif(self.running_qty == 0 and self.last_running_qty == 0):
                if (len(self.exchange.get_orders()) != 0):
                    self.exchange.cancel_all_orders()
                logger.info("Market_maker has stopped. No orders, no positions now")
        elif(self.running_qty == 0 and abs(wave_coefficient) < 8 and (self.last_running_qty != 0 or self.reset == True)):
            if (len(self.exchange.get_orders()) != 0):
                self.exchange.cancel_all_orders()
            if(self.restart_flag == False):
                sleep(10)
                self.restart_flag = True
                return
            self.restart_flag = False
            self.over_wave_coefficient = False
            settings.ORDER_START_SIZE = self.start_XBt // 1000000 * START_SIZE_MAGNIFICATION    #新算法, 每次初始交易重新设定ORDER_START_SIZE
            for i in reversed(range(1, 5)):
                if not self.long_position_limit_exceeded():
                    buy_orders.append(self.prepare_order(-i, order_status))
                if not self.short_position_limit_exceeded():
                    sell_orders.append(self.prepare_order(i, order_status))
        elif(self.running_qty == 0 and self.last_running_qty != 0):
            if (len(self.exchange.get_orders()) != 0):
                self.exchange.cancel_all_orders()
                self.send_tg_message()
            if(self.over_wave_coefficient == False):
                self.over_wave_coefficient = True
            settings.ORDER_START_SIZE = self.start_XBt // 1000000 * START_SIZE_MAGNIFICATION
            for i in reversed(range(1, 5)):
                if not (self.long_position_limit_exceeded() or wave_coefficient < -8 or wave_coefficient > 12):      #波动系数小于-8或大于12停止挂买单
                    buy_orders.append(self.prepare_order(-i, order_status))
                if not (self.short_position_limit_exceeded() or wave_coefficient > 8 or wave_coefficient < -12):     #波动系数大于8或小于-12停止挂卖单
                    sell_orders.append(self.prepare_order(i, order_status))
        elif(self.running_qty == 0 and self.last_running_qty == 0):
            settings.ORDER_START_SIZE = self.start_XBt // 1000000 * START_SIZE_MAGNIFICATION
            if(self.check_order_side_isneed_restart() == True):
                for i in reversed(range(1, 5)):
                    if not (self.long_position_limit_exceeded() or wave_coefficient < -8 or wave_coefficient > 12):  #波动系数小于-8或大于12停止挂买单
                        buy_orders.append(self.prepare_order(-i, order_status))
                    if not (self.short_position_limit_exceeded() or wave_coefficient > 8 or wave_coefficient < -12): #波动系数大于8或小于-12停止挂卖单
                        sell_orders.append(self.prepare_order(i, order_status))
            elif(abs(wave_coefficient) > 15):
                self.exchange.cancel_all_orders()
                return
            else:
                logger.info("Order has created.")
                return
        elif(self.running_qty > 0 and ((not(wave_coefficient < -8 and self.running_qty >= settings.ORDER_START_SIZE)) or self.running_qty > self.last_running_qty) and self.check_stop_order()):
            self.over_wave_coefficient = False
            cycles_sell = self.running_qty // (2 * settings.ORDER_START_SIZE) + 2 if self.running_qty <= 2 * settings.ORDER_START_SIZE else (self.running_qty - 2 * settings.ORDER_START_SIZE - 1) // (settings.ORDER_START_SIZE // 2) + 4
            cycles_buy = (self.running_qty - settings.ORDER_START_SIZE // 4) // (settings.ORDER_START_SIZE // 4) + 1
            cycles_buy_end = (self.running_qty - settings.ORDER_START_SIZE) // (settings.ORDER_START_SIZE // 4) + 5
            if (self.running_qty == self.last_running_qty and self.suspend_trading_flag == False):     #持仓不变
                return
            elif (self.running_qty > self.last_running_qty and self.last_running_qty >= 0 and self.reset == False and self.running_qty < settings.ORDER_START_SIZE):     #仓位小于ORDER_START_SIZE, 多仓增加,买单不变,卖单变化offset3
                if avgCostPrice != None:
                    sell_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice - STOP_SIZE, self.instrument['tickSize']), "Sell")         #设置止损单
                order_status = 2
                for i in reversed(range(1, cycles_sell)):
                    if not self.short_position_limit_exceeded():
                        sell_orders.append(self.prepare_order(i, order_status))
            elif (self.running_qty < self.last_running_qty and self.last_running_qty >= 0 and self.reset == False):     #多仓减少,卖单不变,买单变化offset2
                if avgCostPrice != None:
                    sell_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice - STOP_SIZE, self.instrument['tickSize']), "Sell")
                order_status = 4
                for i in reversed(range(cycles_buy, cycles_buy_end)):
                    if not self.long_position_limit_exceeded():
                        buy_orders.append(self.prepare_order(-i, order_status))
            elif (self.last_running_qty < 0 or (self.last_running_qty == 0 and self.reset == True) or self.suspend_trading_flag == True or (self.running_qty > self.last_running_qty and self.running_qty >= settings.ORDER_START_SIZE)):    #空转多(或重开有仓位时, 或仓位大于ORDER_START_SIZE多仓增加),买卖单都变化,买offset2卖offset3
                if avgCostPrice != None:
                    sell_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice - STOP_SIZE, self.instrument['tickSize']), "Sell")
                order_status = 1
                self.suspend_trading_flag = False
                for i in reversed(range(cycles_buy, cycles_buy_end)):
                    if not self.long_position_limit_exceeded():
                        buy_orders.append(self.prepare_order(-i, order_status))
                for i in reversed(range(1, cycles_sell)):
                    if not self.short_position_limit_exceeded():
                        sell_orders.append(self.prepare_order(i, order_status))
            else:
                logger.error('running_qty bug. running_qty: %s  last_running_qty: %s' % (self.running_qty, self.last_running_qty))
                self.exit()
        elif(self.running_qty < 0 and ((not(wave_coefficient > 8 and abs(self.running_qty) >= settings.ORDER_START_SIZE)) or abs(self.running_qty) > abs(self.last_running_qty)) and self.check_stop_order()):
            self.over_wave_coefficient = False
            cycles_buy = abs(self.running_qty) // (2 * settings.ORDER_START_SIZE) + 2 if abs(self.running_qty) <= 2 * settings.ORDER_START_SIZE else (abs(self.running_qty) - 2 * settings.ORDER_START_SIZE - 1) // (settings.ORDER_START_SIZE // 2) + 4
            cycles_sell = (abs(self.running_qty) - settings.ORDER_START_SIZE // 4) // (settings.ORDER_START_SIZE // 4) + 1
            cycles_sell_end = (abs(self.running_qty) - settings.ORDER_START_SIZE) // (settings.ORDER_START_SIZE // 4) + 5
            if (self.running_qty == self.last_running_qty and self.suspend_trading_flag == False):     #持仓不变
                return
            elif (abs(self.running_qty) > abs(self.last_running_qty) and self.last_running_qty <= 0 and self.reset == False and abs(self.running_qty) < settings.ORDER_START_SIZE):       #仓位小于ORDER_START_SIZE, 空仓增加,买单变化offset3,卖单不变
                if avgCostPrice != None:
                    buy_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice + STOP_SIZE, self.instrument['tickSize']), "Buy")               #设置止损单
                order_status = 4
                for i in reversed(range(1, cycles_buy)):
                    if not self.long_position_limit_exceeded():
                        buy_orders.append(self.prepare_order(-i, order_status))
            elif (abs(self.running_qty) < abs(self.last_running_qty) and self.last_running_qty <= 0 and self.reset == False):       #空仓减少,卖单变化offset2,买单不变
                if avgCostPrice != None:
                    buy_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice + STOP_SIZE, self.instrument['tickSize']), "Buy")
                order_status = 2
                for i in reversed(range(cycles_sell, cycles_sell_end)):
                    if not self.short_position_limit_exceeded():
                        sell_orders.append(self.prepare_order(i, order_status))
            elif (self.last_running_qty > 0 or (self.last_running_qty == 0 and self.reset == True) or self.suspend_trading_flag == True or (abs(self.running_qty) > abs(self.last_running_qty) and abs(self.running_qty) >= settings.ORDER_START_SIZE)):    #多转空(或重开有仓位时, 或仓位大于ORDER_START_SIZE空仓增加),买卖单都变化,买offset3卖offset2
                if avgCostPrice != None:
                    buy_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice + STOP_SIZE, self.instrument['tickSize']), "Buy")
                order_status = 3
                self.suspend_trading_flag = False
                for i in reversed(range(1, cycles_buy)):
                    if not self.long_position_limit_exceeded():
                        buy_orders.append(self.prepare_order(-i, order_status))
                for i in reversed(range(cycles_sell, cycles_sell_end)):
                    if not self.short_position_limit_exceeded():
                        sell_orders.append(self.prepare_order(i, order_status))
            else:
                logger.error('running_qty bug. running_qty: %s  last_running_qty: %s' % (self.running_qty, self.last_running_qty))
                self.exit()
        else:
            self.suspend_trading_flag = True
            if (self.running_qty > 0):  #波动过大, 买单撤销, 卖单维持不变
                if(self.running_qty == settings.ORDER_PAIRS * settings.ORDER_START_SIZE):      #已经最大值, 不需要撤单
                    return
                if(self.over_wave_coefficient == False):
                    self.over_wave_coefficient = True
                print('wave_coefficient(%.2f) is over 8, Canceling buy trading!' % wave_coefficient)
                if avgCostPrice != None:
                    sell_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice - STOP_SIZE, self.instrument['tickSize']), "Sell")
                order_status = 4
            elif (self.running_qty < 0):    #波动过大, 卖单撤销, 买单维持不变
                if(self.running_qty == -settings.ORDER_PAIRS * settings.ORDER_START_SIZE):      #已经最大值, 不需要撤单
                    return
                if(self.over_wave_coefficient == False):
                    self.over_wave_coefficient = True
                print('wave_coefficient(%.2f) is over 8, Canceling sell trading!' % wave_coefficient)
                if avgCostPrice != None:
                    buy_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice + STOP_SIZE, self.instrument['tickSize']), "Buy")
                order_status = 2
            else:
                logger.error('running_qty bug. running_qty: %s  last_running_qty: %s wave_coefficient: %s' % (self.running_qty, self.last_running_qty, wave_coefficient))
                self.exit()
        if(self.last_running_qty != self.running_qty):
            self.send_tg_message()
        self.last_running_qty = self.running_qty
        self.reset = False
        buy_orders = list(filter(None.__ne__, buy_orders))      #去除None
        sell_orders = list(filter(None.__ne__, sell_orders))    #去除None
        print(buy_orders)
        print(sell_orders)
        self.converge_stop_order(buy_stop_order, sell_stop_order)
        return self.converge_orders(buy_orders, sell_orders, order_status)

    def check_pin_program(self):
        """确认1分钟内下跌幅度超过40, 且10秒内下跌超过15, 进入接针程序"""
        if ((self.get_wave_coefficient_1min() < -40) and (self.get_wave_coefficient_last10price() < -15)):
            return True

    def find_pin(self):
        """近20笔交易内buy/sell数量大于1.2, 且20笔交易内buy总数超过40000"""
        trade_list = self.exchange.bitmex.get_last_trade('XBTUSD', 300)

    def place_order_pin(self, buy_orders, sell_orders, order_status):
        """设计挂单为最近交易内最低价+0.5"""
        ret = False
        wave_coefficient_last10price = self.get_wave_coefficient_last10price()
        if (wave_coefficient_last10price <= -20 and self.last10price_flag == False):
            self.last10price_flag = True
            order_status = 5
            buy_orders.append({'price': min(self.LastPriceList10second) - 5, 'orderQty': settings.ORDER_PIN_SIZE, 'side': "Buy"})
            buy_orders.append({'price': min(self.LastPriceList10second) - 10, 'orderQty': settings.ORDER_PIN_SIZE, 'side': "Buy"})
            self.pin_buy_orders = buy_orders
            ret = True
        elif (wave_coefficient_last10price >= 20 and self.last10price_flag == False):
            self.last10price_flag = True
            order_status = 5
            sell_orders.append({'price': max(self.LastPriceList10second) + 5, 'orderQty': settings.ORDER_PIN_SIZE, 'side': "Sell"})
            sell_orders.append({'price': max(self.LastPriceList10second) + 10, 'orderQty': settings.ORDER_PIN_SIZE, 'side': "Sell"})
            self.pin_sell_orders = sell_orders
            ret = True
        if (self.last10price_countdown <= 0):
            self.last10price_flag = False
            self.last10price_countdown = 60
            order_status = 6
            buy_orders = self.pin_buy_orders
            sell_orders = self.pin_sell_orders
            self.pin_buy_orders = []
            self.pin_sell_orders = []
            ret = True
        return ret

    def clear_position(self, buy_orders, sell_orders):
        """清空所有仓位"""
        if (self.running_qty > 0):
            sell_orders.append({'price': self.start_position_buy - 1, 'orderQty': self.running_qty, 'side': "Sell"})
        elif (self.running_qty < 0):
            buy_orders.append({'price': self.start_position_sell + 1, 'orderQty': abs(self.running_qty), 'side': "Buy"})

    def prepare_order(self, index, order_status):
        """Create an order object."""

        if(index == 1 or index == -1):
            if (((self.running_qty > 0 and order_status == 4) or (self.running_qty < 0 and order_status == 2))) and (abs(self.running_qty) % settings.ORDER_START_SIZE) != 0:  #多仓部分减少或空仓部分减少
                quantity = settings.ORDER_START_SIZE + (abs(self.running_qty) % settings.ORDER_START_SIZE) if settings.ORDER_START_SIZE < abs(self.running_qty) < 2 * settings.ORDER_START_SIZE else abs(self.running_qty) % settings.ORDER_START_SIZE
            elif((0 < self.running_qty < 2 * settings.ORDER_START_SIZE and (order_status == 2 or order_status == 1)) or (-2 * settings.ORDER_START_SIZE < self.running_qty < 0 and (order_status == 4 or order_status == 3))):
                quantity = abs(self.running_qty)   #仓位化整
            elif((self.running_qty > 2 * settings.ORDER_START_SIZE and (order_status == 2 or order_status == 1)) or (self.running_qty < -2 * settings.ORDER_START_SIZE and (order_status == 4 or order_status == 3))) and (abs(self.running_qty) % (settings.ORDER_START_SIZE // 2)) != 0:
                quantity = settings.ORDER_START_SIZE - (settings.ORDER_START_SIZE // 2 - abs(self.running_qty) % (settings.ORDER_START_SIZE // 2))
            elif(self.running_qty == 0):
                quantity = settings.ORDER_START_SIZE // 4   #波动距离,1/2ORDER_START_SIZE改小成1/4ORDER_START_SIZE
            else:
                quantity = settings.ORDER_START_SIZE
        elif((self.running_qty >= 2 * settings.ORDER_START_SIZE and index == 2) or (self.running_qty <= -2 * settings.ORDER_START_SIZE and index == -2)):
            quantity = settings.ORDER_START_SIZE
        elif((self.running_qty > 2 * settings.ORDER_START_SIZE and index > 2) or (self.running_qty < -2 * settings.ORDER_START_SIZE and index < -2)):
            quantity = settings.ORDER_START_SIZE // 2
        elif((self.running_qty <= 0 and index >= 2) or (self.running_qty >= 0 and index <= -2)):
            if ((settings.ORDER_START_SIZE // 2 + (abs(index)-5) * settings.ORDER_START_SIZE // 4) < abs(self.running_qty) < (settings.ORDER_START_SIZE // 2 + (abs(index)-4) * settings.ORDER_START_SIZE // 4)):
                quantity = settings.ORDER_START_SIZE // 4 - (abs(self.running_qty) - (settings.ORDER_START_SIZE // 2 + (abs(index)-5) * settings.ORDER_START_SIZE // 4))
            else:
                quantity = settings.ORDER_START_SIZE // 4
        else:
            logger.error('Choose quantity Error. index: %s  running_qty: %s' % (index, self.running_qty))
            self.exit()
        if((order_status == 0) or (order_status == 1 and index < 0) or (order_status == 3 and index > 0) or (order_status == 2 and self.running_qty < 0) or (order_status == 4 and self.running_qty > 0)):
            price = self.get_price_offset2(index)
        elif((order_status == 1 and index > 0) or (order_status == 3 and index < 0) or (order_status == 2 and self.running_qty > 0) or (order_status == 4 and self.running_qty < 0)):
            price = self.get_price_offset3(index)
        else:
            logger.error('Choose offset Error. order_status:%s index:%s self.running_qty:%s' % (order_status, index, self.running_qty))
            self.exit()
        if (price == None):
            return None
        else:
            return {'price': price, 'orderQty': quantity, 'side': "Buy" if index < 0 else "Sell"}

    def prepare_stop_order(self, price, side):
        if((price < self.get_ticker()['last']) and (side == 'Buy')):
            price = self.get_ticker()['last'] + 0.5
        elif((price > self.get_ticker()['last']) and (side == 'Sell')):
            price = self.get_ticker()['last'] - 0.5
        self.stop_order_price = price
        return {'stopPx': price, 'orderQty': abs(self.running_qty), 'side': side}

    def check_stop_order(self):
        """判断是否触发止损价格"""
        if(self.reset == True or self.stop_order_price == None or self.last_running_qty == 0):
            return True
        if (self.running_qty > 0 and self.get_ticker()['last'] < self.stop_order_price) or (self.running_qty < 0 and self.get_ticker()['last'] > self.stop_order_price):
            tg_send_important_message('触发止损价格: %s' % self.get_ticker()['last'])
            return False
        else:
            return True

    def check_order_side_isneed_restart(self):
        """检测是否单边挂单, 如果单边挂单再检测单边挂单的价格与最新起始价格相差是否大于2, 如果是需要重新挂单"""
        existing_orders = self.exchange.get_orders()
        if(len(existing_orders) == 0):      #没有订单重新挂单
            return True
        buy_side = False
        sell_side = False
        max_buy_price = 0
        min_sell_price = 9999999
        for order in existing_orders:
            if (order['side'] == 'Buy'):
                buy_side = True
                if (order['price'] > max_buy_price):
                    max_buy_price = order['price']
            elif (order['side'] == 'Sell'):
                sell_side = True
                if (order['price'] < min_sell_price):
                    min_sell_price = order['price']
            else:
                buy_side = True
                sell_side = True
        if (buy_side == True and sell_side == True):
            return False
        elif (buy_side == True):
            if (max_buy_price + 2 < self.start_position_buy):
                return True
        elif (sell_side == True):
            if (min_sell_price - 2 > self.start_position_sell):
                return True
        return False

    def check_double_order(self):
        """检测是否有重复挂单, 发现价格一样的重复挂单删除"""
        to_cancel = []
        def get_price(order):
            if(order['ordType'] == 'Stop'):
                return float(order['stopPx'])
            else:
                return float(order['price'])
        existing_orders = sorted(self.exchange.get_orders(), key=get_price, reverse=True)   #对订单进行排序
        if(len(existing_orders) == 0):
            return
        order_target = {'price' : 0, 'ordType' : '', 'side' : '', 'stopPx' : 0}
        for order in existing_orders:
            if (order['ordType'] == 'Limit' and order_target['price'] == order['price'] and order_target['ordType'] == order['ordType'] and order_target['side'] == order['side']):
                to_cancel.append(order)
            elif(order['ordType'] == 'Stop' and order_target['stopPx'] == order['stopPx'] and order_target['ordType'] == order['ordType'] and order_target['side'] == order['side']):
                to_cancel.append(order)
            order_target = order
        if len(to_cancel) > 0:
            logger.info("Canceling stop %d orders:" % (len(to_cancel)))
            self.exchange.cancel_bulk_orders(to_cancel)

    def converge_stop_order(self, buy_stop_order, sell_stop_order):
        tickLog = self.exchange.get_instrument()['tickLog']
        to_amend = []
        to_create = []
        to_cancel = []
        buys_matched = 0
        sells_matched = 0
        existing_orders = self.exchange.get_orders()
        for order in existing_orders:
            if order['ordType'] != 'Stop':
                continue
            try:
                if(order['side'] == 'Buy'):
                    if(len(buy_stop_order) == 0):
                        to_cancel.append(order)
                        continue
                    else:
                        desired_order = buy_stop_order
                        buys_matched += 1
                elif (order['side'] == 'Sell'):
                    if(len(sell_stop_order) == 0):
                        to_cancel.append(order)
                        continue
                    else:
                        desired_order = sell_stop_order
                        sells_matched += 1
                else:
                    continue
                if desired_order['orderQty'] != order['leavesQty'] or (desired_order['stopPx'] != order['stopPx']):
                    to_amend.append({'orderID': order['orderID'], 'orderQty': order['cumQty'] + desired_order['orderQty'], 'stopPx': desired_order['stopPx'], 'side': order['side']})
            except IndexError:
                # Will throw if there isn't a desired order to match. In that case, cancel it.
                to_cancel.append(order)
        if(len(buy_stop_order) > 0 and buys_matched < 1):
            self.exchange.bitmex.buy_stop(buy_stop_order['orderQty'], buy_stop_order['stopPx'])
        if(len(sell_stop_order) > 0 and sells_matched < 1):
            self.exchange.bitmex.sell_stop(sell_stop_order['orderQty'], sell_stop_order['stopPx'])

        if len(to_amend) > 0:
            for amended_order in reversed(to_amend):
                reference_order = [o for o in existing_orders if o['orderID'] == amended_order['orderID']][0]
                logger.info("Amending stop %4s: %d @ %.*f to %d @ %.*f (%+.*f)" % (
                    amended_order['side'],
                    reference_order['leavesQty'], tickLog, reference_order['stopPx'],
                    (amended_order['orderQty'] - reference_order['cumQty']), tickLog, amended_order['stopPx'],
                    tickLog, (amended_order['stopPx'] - reference_order['stopPx'])
                ))
            # This can fail if an order has closed in the time we were processing.
            # The API will send us `invalid ordStatus`, which means that the order's status (Filled/Canceled)
            # made it not amendable.
            # If that happens, we need to catch it and re-tick.
            try:
                self.exchange.amend_bulk_orders(to_amend)
            except requests.exceptions.HTTPError as e:
                errorObj = e.response.json()
                if errorObj['error']['message'] == 'Invalid ordStatus':
                    logger.warn("Amending failed. Waiting for order data to converge and retrying.")
                    sleep(0.5)
                    return self.place_orders()
                else:
                    logger.error("Unknown error on amend: %s. Exiting" % errorObj)
                    sys.exit(1)

        # Could happen if we exceed a delta limit
        if len(to_cancel) > 0:
            logger.info("Canceling stop %d orders:" % (len(to_cancel)))
            for order in reversed(to_cancel):
                logger.info("%4s %d @ %.*f" % (order['side'], order['leavesQty'], tickLog, order['stopPx']))
            self.exchange.cancel_bulk_orders(to_cancel)

                  
    def converge_orders(self, buy_orders, sell_orders, order_status):
        """Converge the orders we currently have in the book with what we want to be in the book.
           This involves amending any open orders and creating new ones if any have filled completely.
           We start from the closest orders outward."""

        tickLog = self.exchange.get_instrument()['tickLog']
        to_amend = []
        to_create = []
        to_cancel = []
        buys_matched = 0
        sells_matched = 0
        existing_orders = self.exchange.get_orders()

        # Check all existing orders and match them up with what we want to place.
        # If there's an open one, we might be able to amend it to fit what we want.
        for order in existing_orders:
            if order['ordType'] != 'Limit':
                continue
            try:
                if (order['side'] == 'Buy' and (order_status == 0 or order_status == 4 or order_status == 3 or order_status == 1)):
                    desired_order = buy_orders[buys_matched]
                    buys_matched += 1
                elif (order['side'] == 'Sell' and (order_status == 0 or order_status == 2 or order_status == 1 or order_status == 3)):
                    desired_order = sell_orders[sells_matched]
                    sells_matched += 1
                elif (order['price'] == buy_orders[buys_matched]['price'] and order_status == 6):
                    to_cancel.append(order)
                    buys_matched += 1
                    continue
                elif (order['price'] == sell_orders[sells_matched]['price'] and order_status == 6):
                    to_cancel.append(order)
                    sells_matched += 1
                    continue
                else:
                    continue

                # Found an existing order. Do we need to amend it?
                if desired_order['orderQty'] != order['leavesQty'] or (
                        # If price has changed, and the change is more than our RELIST_INTERVAL, amend.
                        desired_order['price'] != order['price'] and
                        abs((desired_order['price'] / order['price']) - 1) > settings.RELIST_INTERVAL):
                    to_amend.append({'orderID': order['orderID'], 'orderQty': order['cumQty'] + desired_order['orderQty'],
                                     'price': desired_order['price'], 'side': order['side']})
                # Found an stop existing order. Do we need to amend it?

            except IndexError:
                # Will throw if there isn't a desired order to match. In that case, cancel it.
                if ((order_status == 2 and order['side'] == 'Sell') or (order_status == 1 and self.running_qty > 0) or (order_status == 4 and order['side'] == 'Buy') or (order_status == 3 and self.running_qty < 0)):
                    to_cancel.append(order)

        if (order_status == 0 or order_status == 4 or order_status == 3 or order_status == 1 or order_status == 5):
            while buys_matched < len(buy_orders):
                to_create.append(buy_orders[buys_matched])
                buys_matched += 1
        if (order_status == 0 or order_status == 2 or order_status == 1 or order_status == 3 or order_status == 5):
            while sells_matched < len(sell_orders):
                to_create.append(sell_orders[sells_matched])
                sells_matched += 1

        if len(to_amend) > 0:
            for amended_order in reversed(to_amend):
                reference_order = [o for o in existing_orders if o['orderID'] == amended_order['orderID']][0]
                logger.info("Amending %4s: %d @ %.*f to %d @ %.*f (%+.*f)" % (
                    amended_order['side'],
                    reference_order['leavesQty'], tickLog, reference_order['price'],
                    (amended_order['orderQty'] - reference_order['cumQty']), tickLog, amended_order['price'],
                    tickLog, (amended_order['price'] - reference_order['price'])
                ))
            # This can fail if an order has closed in the time we were processing.
            # The API will send us `invalid ordStatus`, which means that the order's status (Filled/Canceled)
            # made it not amendable.
            # If that happens, we need to catch it and re-tick.
            try:
                self.exchange.amend_bulk_orders(to_amend)
            except requests.exceptions.HTTPError as e:
                errorObj = e.response.json()
                if errorObj['error']['message'] == 'Invalid ordStatus':
                    logger.warn("Amending failed. Waiting for order data to converge and retrying.")
                    sleep(0.5)
                    return self.place_orders()
                else:
                    logger.error("Unknown error on amend: %s. Exiting" % errorObj)
                    sys.exit(1)

        if len(to_create) > 0:
            logger.info("Creating %d orders:" % (len(to_create)))
            for order in reversed(to_create):
                logger.info("%4s %d @ %.*f" % (order['side'], order['orderQty'], tickLog, order['price']))
            self.exchange.create_bulk_orders(to_create)

        # Could happen if we exceed a delta limit
        if len(to_cancel) > 0:
            logger.info("Canceling %d orders:" % (len(to_cancel)))
            for order in reversed(to_cancel):
                logger.info("%4s %d @ %.*f" % (order['side'], order['leavesQty'], tickLog, order['price']))
            self.exchange.cancel_bulk_orders(to_cancel)

    def send_tg_message(self):
        now = datetime.datetime.now()
        mybalance = '%.6f' % XBt_to_XBT(self.start_XBt)
        message = 'BitMEX交易状态\n' + ('暂停交易\n' if self.stop_market_maker_flag == True else '') + \
            '时间：' + now.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S') + '\n' + \
            '保证金余额：' + mybalance + '\n' + \
            '合约数量：' + str(self.running_qty) + '\n' + \
            '开仓价格：' + str(self.exchange.get_position()['avgCostPrice']) + '\n' + \
            '风险等级：' + str(self.position_grade) + '\n' + \
            '最新价格：' + str(self.get_ticker()['last']) + '\n' + \
            '指数价格：' + str(self.exchange.get_portfolio()['XBTUSD']['markPrice']) + '\n' + \
            '今日盈利：' + '%.6f' % (float(mybalance) - self.yesterday_balance) + '\n' + \
            '作日盈利：' + '%.6f' % (self.yesterday_balance - self.before_yesterday_balance)
        tg_send_message(message)
        if self.position_grade > 3:
            tg_send_important_message(message)

    def send_tg_order_message(self):
        def get_price(order):
            if(order['ordType'] == 'Stop'):
                return float(order['stopPx'])
            else:
                return float(order['price'])

        message = 'BitMEX委托状态\n'
        existing_orders = sorted(self.exchange.get_orders(), key=get_price, reverse=True)
        for order in existing_orders:
            if (order['ordType'] == 'Stop'):
                message = message + '%s %d @ %s %s\n' % (order['side'], order['leavesQty'], order['stopPx'], order['ordType'])
            else:
                message = message + '%s %d @ %s %s\n' % (order['side'], order['leavesQty'], order['price'], order['ordType'])
        tg_send_message(message)

    def exit(self):
        logger.info("Shutting down. All open orders will be cancelled.")
        now = datetime.datetime.now()
        message = 'BitMEX交易机器人异常退出\n' + \
            '时间：' + now.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S') + '\n' + \
            '合约数量：' + str(self.running_qty) + '\n' + \
            '开仓价格：' + str(self.exchange.get_position()['avgCostPrice']) + '\n' + \
            '风险等级：' + str(self.position_grade) + '\n' + \
            '最新价格：' + str(self.get_ticker()['last']) + '\n' + \
            '指数价格：' + str(self.exchange.get_portfolio()['XBTUSD']['markPrice'])
        tg_send_important_message(message)
        try:
            self.exchange.cancel_all_orders()
            self.exchange.bitmex.exit()
        except errors.AuthenticationError as e:
            logger.info("Was not authenticated; could not cancel orders.")
        except Exception as e:
            logger.info("Unable to cancel orders: %s" % e)

        sys.exit()


def run() -> None:
    order_manager = CustomOrderManager()

    # Try/except just keeps ctrl-c from printing an ugly stacktrace
    try:
        order_manager.run_loop()
    except (KeyboardInterrupt, SystemExit):
        sys.exit()
