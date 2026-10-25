import os
import time
import math
import logging
from pybit.unified_trading import HTTP, WebSocket

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("MartingaleWSBot")

class MartingaleGridBot:
    def __init__(self, api_key, api_secret, symbol="BTCUSDT", testnet=True):
        self.symbol = symbol.upper()
        self.testnet = testnet
        
        self.session = HTTP(
            testnet=self.testnet,
            api_key=api_key,
            api_secret=api_secret,
            recv_window=60000,
            demo=True  
        )
        
        self.total_balance = 0.0
        self.is_cycle_active = False
        self.active_sell_orders = []  
        self.next_buy_order_id = None 
        
        self.start_price = 0.0
        self.current_buy_budget = 0.0
        self.total_profit = 0.0  
        
        self._load_symbol_rules()

    def _load_symbol_rules(self):
        try:
            res = self.session.get_instruments_info(category="spot", symbol=self.symbol)
            list_info = res.get("result", {}).get("list", [])
            if not list_info:
                raise ValueError(f"Не удалось найти информацию по паре {self.symbol}")
            
            info = list_info[0]
            tick_size = float(info["priceFilter"]["tickSize"])
            base_precision = float(info["lotSizeFilter"]["basePrecision"])
            
            self.price_scale = int(-math.log10(tick_size)) if tick_size < 1 else 0
            self.qty_scale = int(-math.log10(base_precision)) if base_precision < 1 else 0
            self.min_qty = float(info["lotSizeFilter"]["minOrderQty"])
            self.min_notional = float(info["lotSizeFilter"].get("minOrderAmt", 1.0))
            
            logger.info(f"Правила биржи для {self.symbol} загружены: Цена округл.={self.price_scale}, Объем округл.={self.qty_scale}")
        except Exception as e:
            logger.error(f"Ошибка загрузки спецификации рынка: {e}. Применяются дефолтные параметры.")

    def get_wallet_balance_usdt(self):
        try:
            res = self.session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            list_info = res.get("result", {}).get("list", [])
            if list_info:
                coins = list_info[0].get("coin", [])
                for c in coins:
                    if c.get("coin") == "USDT":
                        balance = float(c.get("walletBalance", 0.0))
                        logger.info(f"💰 Баланс Единого аккаунта обновлен с биржи: {balance} USDT")
                        return balance
            return 0.0
        except Exception as e:
            logger.error(f"Не удалось получить баланс с биржи: {e}")
            return 0.0

    def recover_state_from_exchange(self):
        logger.info("🔍 Запуск модуля 'Подсейв': сканируем открытые ордера на Bybit...")
        try:
            res = self.session.get_open_orders(category="spot", symbol=self.symbol)
            open_orders = res.get("result", {}).get("list", [])
            
            sells = []
            buy_id = None
            
            for order in open_orders:
                o_id = order.get("orderId")
                side = order.get("side")
                if side == "Sell":
                    sells.append(o_id)
                elif side == "Buy":
                    buy_id = o_id  
            
            if len(sells) > 0:
                logger.info(f"⚠️ Найдена незавершенная сетка! Успешно восстановлено ордеров на продажу: {len(sells)}")
                
                self.active_sell_orders = sells
                self.is_cycle_active = True
                
                # --- АВТОМАТИЧЕСКИЙ РАСЧЕТ ВСЕГО КАПИТАЛА ПРИ ПОДСЕЙВЕ ---
                free_usdt = self.get_wallet_balance_usdt()
                frozen_usdt = 0.0
                
                # Считаем, сколько USDT сейчас заморожено во всех ордерах на продажу
                for order in open_orders:
                    if order.get("side") == "Sell":
                        # Объем монет * ориентировочная цена покупки (цена продажи / 1.011)
                        qty = float(order.get("qty", 0))
                        price_sell = float(order.get("price", 0))
                        frozen_usdt += qty * (price_sell / 1.011)
                
                # Общий капитал = Свободные USDT + Замороженные USDT
                self.total_balance = free_usdt + frozen_usdt
                logger.info(f"📊 Авторасчет капитала: Свободно={free_usdt:.2f} USDT, Заморожено в монетах={frozen_usdt:.2f} USDT | Итого капитал={self.total_balance:.2f} USDT")
                # --------------------------------------------------------

                if not hasattr(self, 'order_purchase_prices'):
                    self.order_purchase_prices = {}
                
                backup_price = self.get_market_price()
                for o_id in sells:
                    if o_id not in self.order_purchase_prices:
                        self.order_purchase_prices[o_id] = backup_price / 1.011
                
                if buy_id:
                    logger.info(f"⚠️ Восстановлен страховочный ордер закупки ID: {buy_id}")
                    self.next_buy_order_id = buy_id
                    for order in open_orders:
                        if order.get("orderId") == buy_id:
                            self.current_buy_budget = float(order.get("qty")) * float(order.get("price"))
                else:
                    logger.info("⚠️ Страховочный ордер на покупку не найден на бирже. Выставляем новый страховочный шаг...")
                    self.start_price = self.get_market_price()
                    
                    # Бюджет следующего шага страховки (5% от общего пересчитанного капитала)
                    self.current_buy_budget = self.total_balance * 0.05
                    
                    next_buy_price = self.round_price(self.start_price * (1 - 0.009))
                    next_buy_qty = self.round_qty(self.current_buy_budget / next_buy_price, next_buy_price)
                    
                    self.next_buy_order_id = self.place_order_safe(side="Buy", qty=next_buy_qty, price=next_buy_price)
                
                return True
                
            logger.info("Свободных ордеров не обнаружено. Можно начинать чистый цикл.")
            return False
        except Exception as e:
            logger.error(f"Ошибка при восстановлении состояния с биржи: {e}")
            return False

    def round_price(self, price):
        return round(float(price), self.price_scale)

    def round_qty(self, qty, price):
        qty = round(float(qty), self.qty_scale)
        if qty < self.min_qty:
            qty = self.min_qty
        if (qty * price) < self.min_notional:
            qty = math.ceil((self.min_notional / price) * (10**self.qty_scale)) / (10**self.qty_scale)
        return round(qty, self.qty_scale)

    def place_order_safe(self, side, qty, price=None, order_type="Limit"):
        """Безопасное выставление ордеров с автоматическим сплитованием при превышении лимитов Bybit"""
        try:
            qty = float(qty)
            # Задаем жесткий лимит биржи для BIRB (338,400). Для других монет ставим бесконечность.
            max_limit = 335000.0 if self.symbol == "BIRBUSDT" else 999999999.0
            
            # Если ордер лимитный и его объем превышает максимальный лимит одного ордера
            if order_type == "Limit" and qty > max_limit:
                logger.warning(f"⚠️ Объем {side}-ордера ({qty}) превышает лимит стакана ({max_limit}). Разбиваем на части...")
                
                remaining_qty = qty
                last_order_id = None
                
                while remaining_qty > 0:
                    chunk_qty = min(remaining_qty, max_limit)
                    chunk_qty = self.round_qty(chunk_qty, float(price) if price else self.get_market_price())
                    
                    if chunk_qty >= self.min_qty:
                        params = {
                            "category": "spot",
                            "symbol": self.symbol,
                            "side": side,
                            "orderType": order_type,
                            "qty": str(chunk_qty),
                        }
                        if price:
                            params["price"] = str(price)
                            
                        res = self.session.place_order(**params)
                        last_order_id = res.get("result", {}).get("orderId")
                        logger.info(f"Выставлена часть составного ордера {side}. ID: {last_order_id} ({chunk_qty} монет)")
                        time.sleep(0.2)
                        
                    remaining_qty -= chunk_qty
                    
                return last_order_id  # Возвращаем последний ID для трекинга сетки
                
            # Стандартное выставление, если объем проходит по лимитам
            params = {
                "category": "spot",
                "symbol": self.symbol,
                "side": side,
                "orderType": order_type,
                "qty": str(qty),
            }
            if price:
                params["price"] = str(price)
                
            res = self.session.place_order(**params)
            order_id = res.get("result", {}).get("orderId")
            logger.info(f"Успешно выставлен {side} {order_type} ордер. ID: {order_id}")
            return order_id
            
        except Exception as e:
            logger.error(f"Критическая ошибка при выставлении ордера {side}: {e}")
            return None

    def cancel_all_orders(self):
        try:
            self.session.cancel_all_orders(category="spot", symbol=self.symbol)
            logger.info("Все старые лимитные ордера на бирже успешно отменены.")
        except Exception as e:
            logger.error(f"Не удалось отменить ордера: {e}")

    def get_market_price(self):
        res = self.session.get_tickers(category="spot", symbol=self.symbol)
        return float(res["result"]["list"][0]["lastPrice"])

    def start_new_cycle(self, calculated_balance=10000.0):
        """Запуск торгового цикла строго от переданного капитала"""
        logger.info("=== Запуск нового торгового цикла ===")
        self.cancel_all_orders()
        
        self.active_sell_orders = []
        self.next_buy_order_id = None
        
        if not hasattr(self, 'order_purchase_prices'):
            self.order_purchase_prices = {}
            
        # Жестко фиксируем баланс, переданный сервером управления
        self.total_balance = float(calculated_balance)
        logger.info(f"📈 Работаем в текущем цикле от капитала: {self.total_balance:.4f} USDT")
        
        approx_price = self.get_market_price()
        first_buy_budget = self.total_balance * 0.20
        market_buy_amount = round(first_buy_budget, self.price_scale)
        
        logger.info(f"Ориентировочная цена: {approx_price}. Покупка 20% от капитала на сумму: {market_buy_amount} USDT")
        
        market_order_id = self.place_order_safe(side="Buy", qty=market_buy_amount, order_type="Market")
        if not market_order_id:
            logger.error("Не удалось совершить стартовую покупку. Перезапуск через 10 сек...")
            time.sleep(10)
            return self.start_new_cycle(calculated_balance)
            
        time.sleep(1)
        
        # ВЫТАСКИВАЕМ СРЕДНЮЮ ЦЕНУ ФАКТИЧЕСКОГО ИСПОЛНЕНИЯ (Идея с avgPrice)
        actual_qty = 0.0
        try:
            order_info = self.session.get_order_history(category="spot", symbol=self.symbol, orderId=market_order_id)
            orders_list = order_info.get("result", {}).get("list", [])
            if orders_list:
                market_order_data = orders_list[0]
                self.start_price = float(market_order_data.get("avgPrice", 0) or market_order_data.get("price", 0))
                actual_qty = float(market_order_data.get("cumExecQty", 0) or market_order_data.get("qty", 0))
                logger.info(f"🎯 Точная средняя цена исполнения Market Buy: {self.start_price} USDT. Куплено: {actual_qty} монет.")
        except Exception as err:
            logger.error(f"Не удалось получить точную цену исполнения: {err}. Используем ориентировочную.")
            
        # Резервный расчет, если биржа не вернула историю вовремя
        if self.start_price == 0.0:
            self.start_price = approx_price
            first_buy_qty_raw = market_buy_amount / self.start_price
            actual_qty = self.round_qty(first_buy_qty_raw, self.start_price)
            
         # Учитываем спотовую комиссию Bybit (0.1%), которая вычитается из купленных монет
        net_qty = actual_qty * 0.999
        logger.info(f"Чистый объем монет на кошельке после вычета комиссии Bybit: {net_qty}")
        
        # Округляем объем строго В МЕНЬШУЮ сторону (вниз), чтобы избежать дефицита монет на 4-м шаге
        raw_part = net_qty / 4
        # Отрезаем лишние знаки после запятой в зависимости от qty_scale (для BIRB это до целых)
        factor = 10 ** self.qty_scale
        sell_part_qty = math.floor(raw_part * factor) / factor
        sell_part_qty = self.round_qty(sell_part_qty, self.start_price)
        
        logger.info(f"Ювелирный расчет объема на 1 ордер продажи: {sell_part_qty} монет")
        
        for i in range(1, 5):
            sell_price = self.round_price(self.start_price * (1 + 0.011 * i))
            
            # Если это 4-й (последний) ордер, забираем ВЕСЬ реальный остаток монет на балансе,
            # делая защитную скидку в 0.2% на скрытые комиссии блокировки стакана Bybit
            if i == 4:
                # Отрезаем хвост под qty_scale
                factor = 10 ** self.qty_scale
                sell_part_qty = math.floor((net_qty - (sell_part_qty * 3)) * 0.998 * factor) / factor
                sell_part_qty = self.round_qty(sell_part_qty, sell_price)
                logger.info(f"Ордер №4: Зачистка остатка кошелька под лимиты Bybit. Объем: {sell_part_qty} монет")

            s_id = self.place_order_safe(side="Sell", qty=sell_part_qty, price=sell_price)
            
            if not s_id:
                logger.warning(f"⚠️ Биржа отклонила ордер Тейк-Профит №{i}. Пробуем скорректировать объем...")
                adjusted_qty = self.round_qty(sell_part_qty - self.min_qty, sell_price)
                s_id = self.place_order_safe(side="Sell", qty=adjusted_qty, price=sell_price)
                
            if s_id:
                self.active_sell_orders.append(s_id)
                self.order_purchase_prices[s_id] = self.start_price
            
        if not s_id:
                logger.warning(f"⚠️ Биржа отклонила ордер Тейк-Профит №{i}. Пробуем скорректировать объем...")
                adjusted_qty = self.round_qty(sell_part_qty - self.min_qty, sell_price) # Уменьшаем на 1 шаг
                s_id = self.place_order_safe(side="Sell", qty=adjusted_qty, price=sell_price)
                
        if s_id:
                self.active_sell_orders.append(s_id)
                self.order_purchase_prices[s_id] = self.start_price

        # 3. Выставляем страховочный ордер строго от средней цены исполнения
        self.current_buy_budget = self.total_balance * 0.05
        next_buy_price = self.round_price(self.start_price * (1 - 0.009))
        next_buy_qty = self.round_qty(self.current_buy_budget / next_buy_price, next_buy_price)
        
        logger.info(f"Выставляем страховочный ордер (-0.9%) по цене {next_buy_price}")
        self.next_buy_order_id = self.place_order_safe(side="Buy", qty=next_buy_qty, price=next_buy_price)
        
        if not self.next_buy_order_id:
            logger.error("❌ Биржа отклонила страховочный ордер на покупку. Повторная попытка через 5 сек...")
            time.sleep(5)
            self.cancel_all_orders()
            return self.start_new_cycle()
            
        self.is_cycle_active = True

    def handle_ws_message(self, message):
        if not self.is_cycle_active:
            return
            
        data_list = message.get("data", [])
        for order in data_list:
            if order.get("symbol") != self.symbol:
                continue
                
            order_id = order.get("orderId")
            side = order.get("side")
            status = order.get("orderStatus")
            
            exec_qty = float(order.get("execQty", 0) or 0)
            exec_price = float(order.get("avgPrice", 0) or order.get("price", 0))
            exec_fee = float(order.get("execFee", 0) or 0)
            
            if status in ["Filled", "PartiallyFilled"] and exec_qty > 0:
                if side == "Buy" and order_id == self.next_buy_order_id:
                    logger.info(f"Событие WS: Страховочный ордер закупки {order_id} исполнился на {exec_qty} монет")
                    
                    tp_price = self.round_price(exec_price * (1 + 0.011))
                    tp_qty = self.round_qty(exec_qty, tp_price)
                    s_id = self.place_order_safe(side="Sell", qty=tp_qty, price=tp_price)
                    if s_id:
                        self.active_sell_orders.append(s_id)
                        if not hasattr(self, 'order_purchase_prices'):
                            self.order_purchase_prices = {}
                        self.order_purchase_prices[s_id] = exec_price
                        
                    if status == "Filled":
                        logger.info("Страховочный уровень полностью заполнен. Сдвигаем сетку ниже...")
                        self.current_buy_budget = self.current_buy_budget * 1.2
                        next_buy_price = self.round_price(exec_price * (1 - 0.009))
                        next_buy_qty = self.round_qty(self.current_buy_budget / next_buy_price, next_buy_price)
                        
                        self.next_buy_order_id = self.place_order_safe(side="Buy", qty=next_buy_qty, price=next_buy_price)
                
                elif side == "Sell" and order_id in self.active_sell_orders:
                    if status == "Filled":
                        if order_id in self.active_sell_orders:
                            self.active_sell_orders.remove(order_id)
                        
                        if not hasattr(self, 'order_purchase_prices'):
                            self.order_purchase_prices = {}
                            
                        purchase_price = self.order_purchase_prices.get(order_id, exec_price / 1.011)
                        
                        gross_buy_usdt = purchase_price * exec_qty
                        gross_sell_usdt = exec_price * exec_qty
                        
                        trade_profit = gross_sell_usdt - gross_buy_usdt - exec_fee
                        
                        if trade_profit > 0.0:
                            self.total_profit += trade_profit
                            logger.info(f"💰 Сделка закрыта! Чистый профит: +{trade_profit:.4f} USDT. Всего: {self.total_profit:.4f} USDT")
                        
                        logger.info(f"Осталось ордеров на продажу: {len(self.active_sell_orders)}")
                        
                        if len(self.active_sell_orders) == 0:
                            logger.info("🎯 ВСЕ ОРДЕРА НА ПРОДАЖУ ИСПОЛНЕНЫ! Перезапуск цикла...")
                            self.is_cycle_active = False
                            self.start_new_cycle()

            elif status == "Cancelled":
                logger.warning(f"⚠️ Событие WS: ордер {order_id} ({side}) был ОТМЕНЕН ВРУЧНУЮ!")
                if side == "Sell" and order_id in self.active_sell_orders:
                    self.active_sell_orders.remove(order_id)
                    if hasattr(self, 'order_purchase_prices') and order_id in self.order_purchase_prices:
                        del self.order_purchase_prices[order_id]
                elif side == "Buy" and order_id == self.next_buy_order_id:
                    self.next_buy_order_id = None
def main():
    API_KEY = "Bbm9gT3SRxpjpc185F"
    API_SECRET = "Xhych1MeES3Axbki7NI59WcHGOG7yzXedEs7"
    
    bot = MartingaleGridBot(
        api_key=API_KEY, 
        api_secret=API_SECRET, 
        symbol="BTCUSDT", 
        testnet=False 
    )
    
    # [УМНЫЙ СТАРТ] Сначала проверяем инструмент "подсейва"
    has_saved_state = bot.recover_state_from_exchange()
    
    if not has_saved_state:
        bot.start_new_cycle()
    else:
        logger.info("Бот успешно подцепил старую сетку ордеров и продолжает слежение.")
    
    ws_private = WebSocket(
        testnet=bot.testnet,
        channel_type="private",
        api_key=API_KEY,
        api_secret=API_SECRET,
        demo=True
    )
    
    ws_private.order_stream(callback=bot.handle_ws_message)
    logger.info("WebSocket успешно подключен. Бот перешел в режим ожидания событий...")
    
    try:
        while True:
            if not bot.is_cycle_active:
                bot.start_new_cycle()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем.")

if __name__ == "__main__":
    main()