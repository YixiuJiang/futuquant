# -*- coding: utf-8 -*-
"""
Examples for use the python functions: get push data
"""

from futuquant import *
from time import sleep
from futuquant.common.ft_logger import logger
import multiprocessing as mp
from threading import Thread, RLock


"""
    简介：
        1.futu api一个牛牛号的默认定阅额度是500, 逐笔的权重是5, 故最多只能定阅100支股票
        2.港股全市场正股约2300支， 需要启动23个进程（建议在centos上运行)
        3.本脚本创建多个对象多个进程来定阅ticker, 达到收集尽可能多的股票逐笔数据的目的
        4.仅供参考学习
    接口调用：
        1. ..
        2....
"""


class FullTickerHandleBase(object):
    def on_recv_rsp(self, data_dict):
        print(data_dict)


class SubscribeFullTick(object):
    # 逐笔的权重
    TICK_WEIGHT = 5
    # 配置信息
    DEFAULT_SUB_CONFIG = {
        "sub_max": 3000,                                            # 最多定阅多少支股票(需要依据定阅额度和进程数作一个合理预估）
        "sub_stock_type_list": [SecurityType.STOCK],                # 选择要定阅的股票类型
        "sub_market_list": [Market.US],                             # 要定阅的市场
        "ip": "127.0.0.1",                                          # FutuOpenD运行IP
        "port_begin": 11113,                                        # port FutuOpenD开放的第一个端口号
        "port_count": 30,                                            # 启动了多少个FutuOPenD进程，每个进程的port在port_begin上递增
        "sub_one_size": 100,                                        # 最多向一个FutuOpenD定阅多少支股票
        "is_adjust_sub_one_size": True,                             # 依据当前剩余定阅量动态调整一次的定阅量(测试白名单不受定阅额度限制可置Flase)
        'one_process_ports': 3,                                     # 用多进程提高性能，一个进程处理多少个端口
    }
    def __init__(self):
        self.__sub_config = copy(self.DEFAULT_SUB_CONFIG)
        self.__mp_manage = mp.Manager()
        self.__share_sub_codes = self.__mp_manage.list()   # 共享记录进程已经定阅的股票
        self.__share_left_codes = self.__mp_manage.list()  # 共享记录进程剩余要定阅的股票

        self.__ns_share = self.__mp_manage.Namespace()
        self.__ns_share.is_process_ready = False
        self.__share_queue_exit = mp.Queue()
        self.__share_queue_tick = mp.Queue()

        self.__timestamp_adjust = 0  # 时间与futu server时间校准偏差 : (本地时间 - futu时间) 秒
        self.__codes_pool = []
        self.__process_list = []
        self.__loop_thread = None
        self.__tick_thread = None
        self.__is_start_run = False
        self._tick_handler = FullTickerHandleBase()

    @classmethod
    def cal_timstamp_adjust(cls, quote_ctx):
        # 计算本地时间与futu 时间的偏差, 3次取最小值
        diff_ret = None
        ret = RET_ERROR
        for x in range(3):
            while ret != RET_OK:
                ret, data = quote_ctx.get_global_state()
                if ret != RET_OK:
                    sleep(0.1)
                one_diff = (int(time.time()) - int(data['timestamp']))
            diff_ret = min(diff_ret, one_diff) if diff_ret is not None else one_diff

        return diff_ret

    @classmethod
    def cal_all_codes(cls, quote_ctx, market_list, stock_type_list, max_count):
        all_codes = []
        for market in market_list:
            for stock_type in stock_type_list:
                ret = RET_ERROR
                while ret != RET_OK:
                    ret, data = quote_ctx.get_stock_basicinfo(market, stock_type)
                    if ret != RET_OK:
                        sleep(0.1)
                    codes = list(data['code'])
                    [all_codes.append(code) for code in codes]
                    break
                if len(all_codes) >= max_count:
                    all_codes = all_codes[0: max_count]
                    break
        return all_codes

    @classmethod
    def loop_subscribe_codes(cls, quote_ctx, codes):
        ret = RET_ERROR
        while ret != RET_OK:
            ret, data = quote_ctx.subscribe(codes, SubType.TICKER)
            if ret == RET_OK:
                break
            else:
                print("loop_subscribe_codes :{}".format(data))
            sleep(1)

    @classmethod
    def loop_get_subscription(cls, quote_ctx):
        ret = RET_ERROR
        while ret != RET_OK:
            ret, data = quote_ctx.query_subscription(True)
            if ret == RET_OK:
                return data
            sleep(0.1)

    def set_handler(self, handler):
        self._tick_handler = handler

    def start(self, create_loop_run=True):
        if self.__is_start_run:
            return
        self.__is_start_run = True

        ip = self.__sub_config['ip']
        port_begin = self.__sub_config['port_begin']
        quote_ctx = OpenQuoteContext(ip, port_begin)

        # 如果外面不指定定阅股票，就从股票列表中取出
        if len(self.__codes_pool) == 0:
            self.__codes_pool = self.cal_all_codes(quote_ctx, self.__sub_config['sub_market_list'],
                                self.__sub_config['sub_stock_type_list'], self.__sub_config['sub_max'])

        if len(self.__codes_pool) == 0:
            raise Exception("codes pool is empty")

        # 共享记录剩余的要定阅的股票
        self.__share_left_codes = []
        [self.__share_left_codes.append(code) for code in self.__codes_pool]

        self.__timestamp_adjust = self.cal_timstamp_adjust(quote_ctx)
        quote_ctx.close()

        # 创建进程定阅
        port_idx = 0
        sub_one_size = self.__sub_config['sub_one_size']
        is_adjust_sub_one_size = self.__sub_config['is_adjust_sub_one_size']
        one_process_ports = self.__sub_config['one_process_ports']

        # 创建多个进程定阅ticker
        while len(self.__share_left_codes) > 0 and port_idx < self.__sub_config['port_count']:

            # 备份下遗留定阅股票
            left_codes = []
            [left_codes.append(code) for code in self.__share_left_codes]

            self.__ns_share.is_process_ready = False
            process = mp.Process(target=self.process_fun, args=(ip, port_begin+port_idx, one_process_ports, sub_one_size,
                                    is_adjust_sub_one_size, self.__share_queue_exit, self.__share_queue_tick,
                                                self.__share_sub_codes, self.__share_left_codes, self.__ns_share))
            process.start()
            while process.is_alive() and not self.__ns_share.is_process_ready:
                sleep(0.1)

            if process.is_alive():
                port_idx += one_process_ports
                self.__process_list.append(process)
            else:
                self.__share_left_codes.clear()
                [self.__share_left_codes.append(code) for code in left_codes]

        #log info
        logger.debug("all_sub_code count={} codes={}".format(len(self.__share_sub_codes), self.__share_sub_codes))
        logger.debug("process count={}".format(len(self.__process_list)))

        # 创建tick 处理线程
        self.__tick_thread = Thread(
                target=self._thread_tick_check, args=())
        self.__tick_thread.start()

        # 创建loop 线程
        if create_loop_run:
            self.__loop_thread = Thread(
                target=self._thread_loop_hold, args=())
            self.__loop_thread.start()

    def close(self):
        if not self.__is_start_run:
            return
        self.__share_queue_exit.put(True)

        for proc in self.__process_list:
            proc.join()
        self.__process_list.clear()

        self.__is_start_run = False
        if self.__loop_thread:
            self.__loop_thread.join(timeout=10)
            self.__loop_thread = None

        if self.__tick_thread:
            self.__tick_thread.join(timeout=10)
            self.__tick_thread = None

    def _thread_loop_hold(self):
        while not self.__is_start_run:
            sleep(0.1)

    def _thread_tick_check(self):
        while self.__is_start_run:
            try:
                if self.__share_queue_tick.empty() is False:
                    dict_data = self.__share_queue_tick.get_nowait()
                    if self._tick_handler:
                        self._tick_handler.on_recv_rsp(dict_data)
            except Exception as e:
                pass

    @classmethod
    def process_fun(cls, ip, port, port_count, sub_one_size, is_adjust_sub_one_size,
                share_queue_exit, share_queue_tick, share_sub_codes, share_left_codes, ns_share):
        """
        :param ip:
        :param port: 超始端口
        :param port_count: 端口个数
        :param sub_one_size: 一个端口定阅的个数
        :param is_adjust_sub_one_size:  依据当前剩余定阅量动态调整一次的定阅量(测试白名单不受定阅额度限制可置Flase)
        :param share_queue_exit:  进程共享 - 退出标志
        :param share_queue_tick:  进程共享 - tick数据队列
        :param share_sub_codes:   进程共享 - 定阅成功的股票
        :param share_left_codes:  进程共享 - 剩余需要定阅的量
        :param ns_share:          进程共享 - 变量 is_process_ready 进程定阅操作完成
        :return:
        """
        if not port or sub_one_size <= 0:
            return

        class ProcessTickerHandle(TickerHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                """数据响应回调函数"""
                ret_code, content = super(ProcessTickerHandle, self).on_recv_rsp(rsp_pb)
                if ret_code != RET_OK:
                    return RET_ERROR, content

                data_tmp = content.to_dict(orient='index')
                for dict_data in data_tmp.values():
                    share_queue_tick.put(dict_data)
                return RET_OK, content

        quote_ctx_list  = []
        def create_new_quote_ctx(host, port):
            obj = OpenQuoteContext(host=host, port=port)
            quote_ctx_list.append(obj)
            obj.set_handler(ProcessTickerHandle())
            obj.start()
            return obj

        port_index = 0
        all_sub_codes = []
        while len(share_left_codes) > 0 and port_index < port_count:
            quote_ctx = create_new_quote_ctx(ip, port)
            cur_sub_one_size = sub_one_size
            data = cls.loop_get_subscription(quote_ctx)

            # 已经定阅过的不占用额度可以直接定阅
            codes = data['sub_list'][SubType.TICKER] if SubType.TICKER in data['sub_list'] else []
            codes_to_sub = []
            for code in codes:
                if code not in share_left_codes:
                    continue
                all_sub_codes.append(code)
                share_left_codes.remove(code)
                codes_to_sub.append(code)

            if len(codes_to_sub):
                cls.loop_subscribe_codes(quote_ctx, codes_to_sub)
                cur_sub_one_size -= len(codes_to_sub)

            # 依据剩余额度，调整要定阅的数量
            data = cls.loop_get_subscription(quote_ctx)
            if is_adjust_sub_one_size:
                size_remain = int(data['remain'] / cls.TICK_WEIGHT)
                cur_sub_one_size = cur_sub_one_size if cur_sub_one_size < size_remain else size_remain

            # 执行定阅
            cur_sub_one_size = cur_sub_one_size if cur_sub_one_size < len(share_left_codes) else len(share_left_codes)
            if cur_sub_one_size > 0:
                codes = share_left_codes[0: cur_sub_one_size]
                share_left_codes = share_left_codes[cur_sub_one_size:]
                [all_sub_codes.append(x) for x in codes]
                cls.loop_subscribe_codes(quote_ctx, codes)

            port_index += 1

        # 共享记录定阅成功的股票，并标志该进程定阅完成
        [share_sub_codes.append(code) for code in all_sub_codes]
        ns_share.is_process_ready = True

        # 等待结束信息
        while share_queue_exit.empty() is True:
            sleep(0.2)

        for quote_ctx in quote_ctx_list:
            quote_ctx.close()
        quote_ctx_list = []


if __name__ =="__main__":

    tick_subcrible = SubscribeFullTick()
    tick_subcrible.set_handler(FullTickerHandleBase())
    tick_subcrible.start()

    # 运行24小时后退出
    sleep(10)
    tick_subcrible.close()









