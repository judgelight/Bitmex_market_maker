# -*- coding:utf-8 -*-
import sys
import numpy as np

from market_maker.market_maker import OrderManager

class CustomOrderManager(OrderManager):

    def reset(self):
        self.sanity_check()
        self.print_status()
        self.place_orders()

    def place_orders(self) -> None:
        print('TestOrderManager Test')
        self.BXBT_list_30min = []
        trade_list = self.exchange.bitmex.get_last_trade('.BXBT', 30)
        for trade in trade_list:
            print('time: %s price: %s' % (trade['timestamp'], trade['price']))
            self.BXBT_list_30min.append(trade['price'])
        self.BXBT_MA30 = np.mean(self.BXBT_list_30min)
        print('BXBT_MA30: %s' % self.BXBT_MA30)
        self.exit()

    def exit(self):
        print("TestOrderManager Over, do nothing")
        sys.exit()

def run() -> None:
    order_manager = CustomOrderManager()

    # Try/except just keeps ctrl-c from printing an ugly stacktrace
    try:
        order_manager.run_loop()
    except (KeyboardInterrupt, SystemExit):
        sys.exit()
