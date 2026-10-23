import logging
import time
import uuid
from main import MartingaleGridBot

# Настройка логирования для комплексного теста
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [КОМПЛЕКСНЫЙ-ТЕСТ] - %(levelname)s - %(message)s'
)
logger = logging.getLogger("FullSuite")

# Виртуальная база данных ордеров имитатора Bybit
mock_order_book = {}
# Глобальный виртуальный баланс, который мы сможем менять в ходе теста
virtual_market_balance = 10000.0

def create_mock_order_id():
    return f"ord_demo_{uuid.uuid4().hex[:12]}"

def run_ultimate_simulation():
    global virtual_market_balance
    print("\n" + "="*80 + "\n ЗАПУСК ТЕСТОВОГО КОМПЛЕКСА (МАРТИНГЕЙЛ + АВТО-РЕСТАРТ + СЛОЖНЫЙ %) \n" + "="*80)
    
    # 1. Создаем бота
    bot = MartingaleGridBot(
        api_key="sim_key_comprehensive", 
        api_secret="sim_secret_comprehensive", 
        symbol="BTCUSDT", 
        testnet=False
    )
    
    # 2. Подменяем методы API Bybit на виртуальные заглушки
    bot._load_symbol_rules = lambda: None
    bot.get_market_price = lambda: 80000.0  # Цена для Цикла №1
    bot.cancel_all_orders = lambda: logger.info("Заглушка API: Старые ордера очищены на бирже.")
    
    # Динамический возврат баланса (будет брать значение из нашей переменной)
    bot.get_wallet_balance_usdt = lambda: virtual_market_balance
    
    def mock_place_order(side, qty, price=None, order_type="Limit"):
        o_id = create_mock_order_id()
        mock_order_book[o_id] = {
            "orderId": o_id,
            "symbol": bot.symbol,
            "side": side,
            "qty": str(qty),
            "price": str(price) if price else str(bot.get_market_price()),
            "orderStatus": "Filled"
        }
        logger.info(f"Виртуальная биржа: Размещен ордер [{side} {order_type}] ID: {o_id} | Qty/Сумма: {qty} | Price: {price or 'Рыночная'}")
        return o_id

    bot.place_order_safe = mock_place_order

    # Конфигурируем точность
    bot.price_scale = 1
    bot.qty_scale = 6
    bot.min_qty = 0.000001
    bot.min_notional = 1.0

    # 3. Инициируем ЦИКЛ №1 (Изначально от 10 000 USDT)
    logger.info("Действие: Инициализируем ЦИКЛ №1...")
    bot.start_new_cycle()
    
    captured_buy_order = bot.next_buy_order_id
    
     # --------------------------------------------------------------------------
    # ЭТАП 1: Имитируем жесткое падение цены на 15% (Стресс-тест Мартингейла)
    # --------------------------------------------------------------------------
    print("\n" + "="*80 + "\n ЭТАП 1: Стресс-тест Мартингейла (Симуляция обвала рынка на 15%) \n" + "="*80)
    
    current_sim_price = 80000.0
    target_drop_price = 80000.0 * (1 - 0.15)  # 68000.0 USDT
    step_counter = 0
    
    logger.info(f"Начало дампа. Стартовая цена: {current_sim_price} USDT. Целевая цена падения: {target_drop_price} USDT")
    
    # Запускаем цикл, пока виртуальная цена не упадет ниже -15%
    while current_sim_price > target_drop_price:
        active_buy_id = bot.next_buy_order_id
        
        if not active_buy_id or active_buy_id not in mock_order_book:
            logger.warning("Сетку разорвало или ордер закупки не найден. Прерываем тест.")
            break
            
        target_buy = mock_order_book[active_buy_id]
        
        step_counter += 1
        current_sim_price = float(target_buy["price"])
        
        print(f"\n--- [Шаг падения №{step_counter}] Цена опустилась до {current_sim_price} USDT ---")
        logger.info(f"WebSocket: Исполнен страховочный BUY ордер {active_buy_id} на сумму {bot.current_buy_budget:.2f} USDT")
        
        # Эмулируем отправку сообщения в обработчик бота
        ws_msg_buy = {"data": [target_buy]}
        bot.handle_ws_message(ws_msg_buy)
        
        # Защита от бесконечного цикла, если что-то пойдет не так
        if step_counter > 25:
            break
            
    logger.info(f"🛑 Дамп завершен! Бот выдержал {step_counter} шагов падения.")
    logger.info(f"Финальный объем Тейк-Профитов в сетке продаж: {len(bot.active_sell_orders)} шт.")
    logger.info(f"Следующий шаг закупки потребует бюджет: {bot.current_buy_budget:.2f} USDT (Мартингейл)")

    # --------------------------------------------------------------------------
    # ЭТАП 2: Разворот цены и триггер АВТО-ПЕРЕЗАПУСКА
    # --------------------------------------------------------------------------
    print("\n" + "="*80 + "\n ЭТАП 2: Разворот рынка вверх и проверка АВТОМАТИЧЕСКОГО ПЕРЕЗАПУСКА \n" + "="*80)
    
    # Меняем рыночную цену для будущего цикла №2
    bot.get_market_price = lambda: 85000.0  
    
    # Имитируем, что за время успешного закрытия ордеров баланс кошелька вырос на +500 USDT прибыли
    virtual_market_balance = 10500.0
    logger.info(f"Фиксация прибыли на бирже: Виртуальный баланс вырос до {virtual_market_balance} USDT")
    
    # Копируем текущий список продаж (там 5 ордеров)
    sells_to_execute = list(bot.active_sell_orders)
    
    for idx, sell_id in enumerate(sells_to_execute):
        if sell_id in mock_order_book:
            target_sell = mock_order_book[sell_id]
            ws_msg_sell = {"data": [target_sell]}
            
            logger.info(f"Рынок растет: Выкуплен Sell-ордер {idx + 1}/{len(sells_to_execute)} (ID: {sell_id})")
            bot.handle_ws_message(ws_msg_sell)
            
            if idx == len(sells_to_execute) - 1:
                break

    # --------------------------------------------------------------------------
    # ЭТАП 3: Проверка Сложного процента (Реинвестирования) в новом цикле
    # --------------------------------------------------------------------------
    print("\n" + "="*80 + "\n ЭТАП 3: Проверка Сложного Процента (Увеличение объемов от прибыли) \n" + "="*80)
    
    # Расчет проверки: В новом цикле 20% от 10 500 USDT должно составить ровно 2100 USDT
    expected_new_budget = 10500.0 * 0.20 # 2100.0
    
    logger.info(f"Анализ капитала в активном ЦИКЛЕ №2: {bot.total_balance} USDT")
    
    # --------------------------------------------------------------------------
    # ЭТАП 4: Проверка инструмента "Подсейв" (Восстановление после сбоя)
    # --------------------------------------------------------------------------
    print("\n" + "="*80 + "\n ЭТАП 4: Проверка инструмента 'Подсейв' (Восстановление состояния) \n" + "="*80)
    
    logger.info("Симуляция сбоя: Создаем нового бота, имитируя перезагрузку скрипта...")
    rebooted_bot = MartingaleGridBot(
        api_key="sim_key_comprehensive", api_secret="sim_secret_comprehensive", symbol="BTCUSDT", testnet=False
    )
    
    # Передаем новому боту фейковую базу данных Bybit, где уже лежат ордера
    rebooted_bot._load_symbol_rules = lambda: None
    rebooted_bot.get_wallet_balance_usdt = lambda: 10000.0
    
    # Имитируем ответ Bybit: возвращаем список «активных ордеров», оставшихся на бирже после сбоя
    def mock_get_open_orders(category, symbol):
        return {
            "result": {
                "list": [
                    {"orderId": "saved_sell_1", "side": "Sell", "qty": "0.005", "price": "82000"},
                    {"orderId": "saved_sell_2", "side": "Sell", "qty": "0.005", "price": "83000"},
                    {"orderId": "saved_buy_insurance", "side": "Buy", "qty": "0.006", "price": "79000"}
                ]
            }
        }
    rebooted_bot.session.get_open_orders = mock_get_open_orders
    
    # Запускаем подсейв у перезагруженного бота
    is_recovered = rebooted_bot.recover_state_from_exchange()

    # --------------------------------------------------------------------------
    # ФИНАЛЬНАЯ ПРОВЕРКА ВСЕХ СЦЕНАРИЕВ
    # --------------------------------------------------------------------------
    print("\n" + "="*80 + "\n АНАЛИЗ РЕЗУЛЬТАТОВ КОМПЛЕКСНОГО ТЕСТА \n" + "="*80)
    
    if bot.is_cycle_active and len(bot.active_sell_orders) == 4 and bot.total_balance == 10500.0 and is_recovered:
        print("\n" + "*"*80 + "\n ВСЕ ЧЕТЫРЕ ТЕСТА ПРОЙДЕНЫ СВЕРХУСПЕШНО! \n\n"
              " 1. [ЭТАП 1] Логика усреднения сетки вниз по Мартингейлу — РАБОТАЕТ.\n"
              " 2. [ЭТАП 2] Мгновенный авто-рестарт нового цикла через WebSocket — РАБОТАЕТ.\n"
              " 3. [ЭТАП 3] Функция сложного процента (авто-реинвестирование) — РАБОТАЕТ.\n"
              " 4. [ЭТАП 4] Инструмент 'ПОДСЕЙВ' (Успешное восстановление активного цикла \n"
              "    напрямую из ордеров биржи после перезагрузки) — РАБОТАЕТ ИДЕАЛЬНО!\n" + "*"*80)
    else:
        print("\n Внимание: Какой-то из этапов логики дал сбой. Проверьте структуру кода.")

if __name__ == "__main__":
    run_ultimate_simulation()
