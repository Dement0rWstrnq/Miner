import threading
import time
import logging
from flask import Flask, render_template, request, jsonify
from pybit.unified_trading import HTTP
from main import MartingaleGridBot

app = Flask(__name__)
logger = logging.getLogger("MartingaleWSBot")

bot_instance = None
bot_thread = None
bot_status = "Остановлен"
selected_symbol = "BTCUSDT"

# Глобальные переменные сервера для пуленепробиваемого сложного процента
gui_user_capital = 10000.0
clean_profit = 0.0

def get_all_bybit_spot_symbols():
    try:
        session = HTTP(testnet=False)
        res = session.get_instruments_info(category="spot")
        symbols_list = res.get("result", {}).get("list", [])
        return sorted([item["symbol"] for item in symbols_list if item["symbol"].endswith("USDT")])
    except Exception as e:
        logger.error(f"Не удалось загрузить список пар с Bybit: {e}")
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

def run_bot_in_background(api_key, api_secret, symbol):
    global bot_instance, bot_status, clean_profit, gui_user_capital
    try:
        bot_status = f"Запуск на паре {symbol}..."
        bot_instance = MartingaleGridBot(
            api_key=api_key,
            api_secret=api_secret,
            symbol=symbol,
            testnet=False
        )
        
        # Считаем актуальный капитал: Стартовый ввод + Накопленная прибыль
        active_capital = gui_user_capital + clean_profit
        
        has_saved_state = bot_instance.recover_state_from_exchange()
        if not has_saved_state:
            bot_instance.start_new_cycle(active_capital)
            
        bot_status = f"Работает 🟢 (Пара: {symbol})"
        
        while bot_status.startswith("Работает"):
            # Расчет сложного процента при автоматическом рестарте
            if not bot_instance.is_cycle_active:
                logger.info("🎯 Фиксация: тейк-профиты закрыты. Зачищаем стакан перед реинвестированием...")
                bot_instance.cancel_all_orders()
                time.sleep(1)
                
                # Пересчитываем капитал со свежей прибылью
                active_capital = gui_user_capital + clean_profit
                bot_instance.start_new_cycle(active_capital)
                continue
                
            try:
                res = bot_instance.session.get_open_orders(category="spot", symbol=bot_instance.symbol)
                open_orders = res.get("result", {}).get("list", [])
                
                current_exchange_sells = []
                is_insurance_buy_still_open = False
                
                for o in open_orders:
                    if o.get("side") == "Sell":
                        current_exchange_sells.append(o.get("orderId"))
                    elif o.get("side") == "Buy" and o.get("orderId") == bot_instance.next_buy_order_id:
                        is_insurance_buy_still_open = True

                # Очистка стакана при ручном обнулении
                if bot_instance.is_cycle_active and len(open_orders) == 0:
                    logger.warning("⚠️ Стакан пуст! Принудительный перезапуск...")
                    bot_instance.is_cycle_active = False
                    bot_instance.cancel_all_orders()
                    active_capital = gui_user_capital + clean_profit
                    bot_instance.start_new_cycle(active_capital)
                    continue

                # Зависание страховки
                if bot_instance.is_cycle_active and len(current_exchange_sells) == 0 and len(open_orders) > 0:
                    logger.warning("⚠️ Тейк-профиты исполнены, но страховочный ордер застрял. Перезапуск...")
                    bot_instance.is_cycle_active = False
                    bot_instance.cancel_all_orders()
                    active_capital = gui_user_capital + clean_profit
                    bot_instance.start_new_cycle(active_capital)
                    continue

                # Пропуск WebSocket пакета
                if bot_instance.is_cycle_active and bot_instance.next_buy_order_id and not is_insurance_buy_still_open:
                    if len(current_exchange_sells) > 0:
                        logger.warning(f"🔄 Резервный аудит: Страховочный ордер {bot_instance.next_buy_order_id} исполнен (пропуск WS). Переставляем сетку...")
                        mock_exec_price = bot_instance.get_market_price()
                        mock_exec_qty = bot_instance.current_buy_budget / mock_exec_price
                        
                        fake_msg = {
                            "data": [{
                                "symbol": bot_instance.symbol,
                                "orderId": bot_instance.next_buy_order_id,
                                "side": "Buy",
                                "orderStatus": "Filled",
                                "execQty": str(mock_exec_qty),
                                "avgPrice": str(mock_exec_price)
                            }]
                        }
                        bot_instance.handle_ws_message(fake_msg)

            except Exception as audit_error:
                logger.error(f"Ошибка фонового аудита: {audit_error}")

            time.sleep(1)
            
    except Exception as e:
        bot_status = f"Ошибка: {str(e)} 🔴"

@app.route('/')
def index():
    global bot_status, selected_symbol, bot_instance, clean_profit
    current_balance = bot_instance.total_balance if bot_instance else 0.0
    return render_template('index.html', status=bot_status, symbol=selected_symbol, balance=current_balance, profit=clean_profit, symbols=get_all_bybit_spot_symbols())

@app.route('/start', methods=['POST'])
def start_bot():
    global bot_thread, bot_status, selected_symbol, clean_profit, gui_user_capital
    if bot_thread and bot_thread.is_alive():
        return jsonify({"message": "Бот уже запущен!"}), 400
        
    api_key = request.form.get('api_key')
    api_secret = request.form.get('api_secret')
    selected_symbol = request.form.get('symbol', 'BTCUSDT')
    
    # Сохраняем ввод пользователя прямо в память сервера
    gui_user_capital = float(request.form.get('total_balance', 10000.0))
    clean_profit = 0.0
    
    bot_thread = threading.Thread(target=run_bot_in_background, args=(api_key, api_secret, selected_symbol), daemon=True)
    bot_thread.start()
    return jsonify({"message": "Сигнал на запуск отправлен!"})

@app.route('/stop', methods=['POST'])
def stop_bot():
    global bot_status, bot_instance
    bot_status = "Остановлен 🛑"
    if bot_instance:
        bot_instance.is_cycle_active = False
    return jsonify({"message": "Бот остановлен!"})

@app.route('/status')
def get_status():
    global bot_status, bot_instance, clean_profit
    current_balance = bot_instance.total_balance if bot_instance else 0.0
    
    # Подтягиваем профит из истории закрытых ордеров Bybit в реальном времени
    if bot_instance:
        try:
            history = bot_instance.session.get_order_history(category="spot", symbol=bot_instance.symbol, limit=50)
            history_orders = history.get("result", {}).get("list", [])
            calculated_profit = 0.0
            for order in history_orders:
                if order.get("side") == "Sell" and order.get("orderStatus") == "Filled":
                    exec_price = float(order.get("avgPrice", 0) or order.get("price", 0))
                    exec_qty = float(order.get("qty", 0))
                    exec_fee = float(order.get("execFee", 0) or 0)
                    
                    purchase_price = exec_price / 1.011
                    gross_buy_usdt = purchase_price * exec_qty
                    gross_sell_usdt = exec_price * exec_qty
                    
                    trade_profit = gross_sell_usdt - gross_buy_usdt - exec_fee
                    if trade_profit > 0:
                        calculated_profit += trade_profit
            clean_profit = calculated_profit
        except Exception:
            pass
            
    return jsonify({"status": bot_status, "balance": current_balance, "profit": round(clean_profit, 4)})

if __name__ == '__main__':
    app.run(debug=True, port=5000, use_reloader=False)