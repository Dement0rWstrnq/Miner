import threading
import logging
from flask import Flask, render_template, request, jsonify
from main import MartingaleGridBot

app = Flask(__name__)

# Настройка логирования для веб-сервера
logger = logging.getLogger("MartingaleWSBot")

# Глобальные переменные для управления фоновым потоком бота
bot_instance = None
bot_thread = None
bot_status = "Остановлен"
selected_symbol = "BTCUSDT"

def run_bot_in_background(api_key, api_secret, symbol, total_balance):
    global bot_instance, bot_status
    try:
        bot_status = f"Запуск на паре {symbol}..."
        bot_instance = MartingaleGridBot(
            api_key=api_key,
            api_secret=api_secret,
            symbol=symbol,
            testnet=False
        )
        
        bot_instance.total_balance = float(total_balance)
        
        # Проверяем подсейв при старте
        has_saved_state = bot_instance.recover_state_from_exchange()
        if not has_saved_state:
            bot_instance.start_new_cycle()
            
        bot_status = f"Работает 🟢 (Пара: {symbol})"
        
        # Бесконечный цикл фонового мониторинга и аудита биржи
        while bot_status.startswith("Работает"):
            # Если флаг сбросился в основном коде — запускаем новый цикл
            if not bot_instance.is_cycle_active:
                bot_instance.start_new_cycle()
                
            # ЗАЩИТНЫЙ ПРЕДОХРАНИТЕЛЬ: Проверяем реальное наличие ордеров в стакане Bybit
            try:
                res = bot_instance.session.get_open_orders(category="spot", symbol=bot_instance.symbol)
                open_orders = res.get("result", {}).get("list", [])
                
                # Если бот думает, что цикл активен, но вы руками удалили абсолютно все ордера
                if bot_instance.is_cycle_active and len(open_orders) == 0:
                    logger.warning("⚠️ Обнаружена полная ручная очистка стакана на Bybit! Принудительный перезапуск цикла...")
                    bot_instance.is_cycle_active = False
                    bot_instance.start_new_cycle()
            except Exception as exchange_error:
                # Игнорируем временные сетевые заминки при запросах
                pass

            threading.Event().wait(1)
            
    except Exception as e:
        bot_status = f"Ошибка: {str(e)} 🔴"

@app.route('/')
def index():
    global bot_status, selected_symbol, bot_instance
    current_balance = bot_instance.total_balance if bot_instance else 0.0
    return render_template('index.html', status=bot_status, symbol=selected_symbol, balance=current_balance)

@app.route('/start', methods=['POST'])
def start_bot():
    global bot_thread, bot_status, selected_symbol
    
    if bot_thread and bot_thread.is_alive():
        return jsonify({"message": "Бот уже запущен!"}), 400
        
    api_key = request.form.get('api_key')
    api_secret = request.form.get('api_secret')
    selected_symbol = request.form.get('symbol', 'BTCUSDT')
    total_balance = request.form.get('total_balance', 1000.0)
    
    bot_thread = threading.Thread(
        target=run_bot_in_background, 
        args=(api_key, api_secret, selected_symbol, total_balance),
        daemon=True
    )
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
    global bot_status, bot_instance
    current_balance = bot_instance.total_balance if bot_instance else 0.0
    return jsonify({"status": bot_status, "balance": current_balance})

if __name__ == '__main__':
    app.run(debug=True, port=5000, use_reloader=False)