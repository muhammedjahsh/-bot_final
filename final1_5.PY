import threading
import webbrowser
from flask import Flask, render_template, jsonify
import MetaTrader5 as mt5
import pandas as pd
import time
import logging

# Initialize logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger()

# Initialize Flask app
app = Flask(__name__)

# #MetaTrader 5 credentials 
# account_number = 48678273  # Your MT5 account number
# password = 'Demo@1234'
# server = 'HFMarketsGlobal-Demo'

account_number = 48693013  # Your MT5 account number
password = 'Demo@1234'
server = 'HFMarketsGlobal-Demo'

# Global variables
trading_active = False
trading_thread = None
symbol = "XAUUSD"
timeframe = mt5.TIMEFRAME_M5
period = 7
multiplier = 3
initial_price = None
open_trade = None
lot_size = 0.1  # Set your lot size

# Initialize MetaTrader 5
if not mt5.initialize():
    log.error("Failed to initialize MetaTrader 5. Exiting...")
    exit()


def calculate_supertrend(df, period=7, multiplier=3):
    df['high_low'] = df['high'] - df['low']
    df['high_close'] = abs(df['high'] - df['close'].shift(1))
    df['low_close'] = abs(df['low'] - df['close'].shift(1))
    df['TR'] = df[['high_low', 'high_close', 'low_close']].max(axis=1)
    df['ATR'] = df['TR'].rolling(window=period).mean()

    # Calculate bands
    hl2 = (df['high'] + df['low']) / 2
    df['Upper_Band'] = hl2 + (multiplier * df['ATR'])
    df['Lower_Band'] = hl2 - (multiplier * df['ATR'])

    # Initialize SuperTrend
    df['Supertrend'] = 0.0
    df['ST_Direction'] = 0  # 1 for uptrend, -1 for downtrend
    df['Signal'] = None

    for i in range(period, len(df)):
        if i == period:
            df.loc[i, 'ST_Direction'] = 1 if df.loc[i, 'close'] > df.loc[i, 'Upper_Band'] else -1
            df.loc[i, 'Supertrend'] = df.loc[i, 'Lower_Band'] if df.loc[i, 'ST_Direction'] == 1 else df.loc[i, 'Upper_Band']
            continue

        prev_supertrend = df.loc[i-1, 'Supertrend']
        prev_direction = df.loc[i-1, 'ST_Direction']
        curr_close = df.loc[i, 'close']
        curr_upper = df.loc[i, 'Upper_Band']
        curr_lower = df.loc[i, 'Lower_Band']

        if prev_direction == 1:
            if curr_close <= prev_supertrend:
                df.loc[i, 'ST_Direction'] = -1
                df.loc[i, 'Supertrend'] = curr_upper
                df.loc[i, 'Signal'] = 'Sell'
            else:
                df.loc[i, 'ST_Direction'] = 1
                df.loc[i, 'Supertrend'] = max(curr_lower, prev_supertrend)
                df.loc[i, 'Signal'] = 'Buy'
        else:
            if curr_close >= prev_supertrend:
                df.loc[i, 'ST_Direction'] = 1
                df.loc[i, 'Supertrend'] = curr_lower
                df.loc[i, 'Signal'] = 'Buy'
            else:
                df.loc[i, 'ST_Direction'] = -1
                df.loc[i, 'Supertrend'] = min(curr_upper, prev_supertrend)
                df.loc[i, 'Signal'] = 'Sell'

    return df

def close_all_trades(symbol=None):
    """
    Closes all open trades.
    If a symbol is provided, it will only close trades for that symbol.
    """
    if symbol:
        positions = mt5.positions_get(symbol=symbol)
    else:
        positions = mt5.positions_get()

    if not open_trade:
        print("No open positions found.")
        return False

    for position in positions:
        order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(position.symbol)
        if not tick:
            print(f"Failed to get market data for {position.symbol}")
            continue

        price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": order_type,
            "position": position.ticket,
            "price": price,
            "deviation": 100,
            "magic": 123456,
            "comment": "Python script close",
            "type_time": mt5.ORDER_TIME_GTC,
        }

        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"Trade closed successfully! Ticket: {position.ticket}, Symbol: {position.symbol}")
        else:
            print(f"Failed to close trade {position.ticket}, Symbol: {position.symbol}, Retcode: {result.retcode}, Comment: {result.comment}")

    return True

def place_order(symbol, action, lot_size=0.01):
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        print(f"Symbol {symbol} is not found.")
        return False

    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            print(f"Failed to select {symbol} for trading.")
            return False

    if symbol_info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
        print(f"Symbol {symbol} is not fully tradable. Trade mode: {symbol_info.trade_mode}")
        return False

    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        print(f"Unable to retrieve tick data for {symbol}.")
        return False

    price = tick.ask if action == "Buy" else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if action == "Buy" else mt5.ORDER_TYPE_SELL

    # Try order without type_filling first
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot_size,
        "type": order_type,
        "price": price,
        "deviation": 100,
        "magic": 123456,
        "comment": f"Python script {action}",
        "type_time": mt5.ORDER_TIME_GTC,
    }

    print(f"Submitting order: {request}")
    result = mt5.order_send(request)

    # Check for errors
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"Order failed. Retcode: {result.retcode}, Comment: {result.comment}")
        if result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
            print("Invalid filling mode. Retrying with a dynamic mode.")
            # Retry with supported filling modes dynamically
            for mode in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]:
                if symbol_info.filling_mode & mode:
                    request["type_filling"] = mode
                    result = mt5.order_send(request)
                    if result.retcode == mt5.TRADE_RETCODE_DONE:
                        print("Order placed successfully after retry.")
                        return True
        print("Order failed after retries.")
        return False

    print("Order placed successfully.")
    return True


def trading_logic():
    global trading_active, open_trade, initial_price
    trade_in_trend = False  # NEW FLAG to track if a trade has been made in the current trend

    while trading_active:
        try:
            rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, 100)
            if rates is None or len(rates) == 0:
                log.warning("No data received from MT5. Retrying...")
                time.sleep(10)
                continue

            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s')

            df = calculate_supertrend(df, period, multiplier)

            last_row = df.iloc[-1]
            current_price = last_row['close']

            log.info(f"Signal: {last_row['Signal']}, Current Price: {current_price}, Initial Price: {initial_price}")

            if len(df) > 1:
                current_signal = df.iloc[-1]['Signal']
                previous_signal = df.iloc[-2]['Signal']
                if current_signal != previous_signal and previous_signal is not None:
                    log.info(f"SIGNAL CHANGE: {previous_signal} -> {current_signal}")

                # NEW CONDITION: Close all open trades if the trend reverses
                if open_trade:
                    open_action = open_trade[0]["action"] if open_trade else None
                    if open_action and open_action.lower() != current_signal.lower():
                        log.info(f"Trend reversal detected. Closing all {open_action} trades.")
                        close_all_trades(symbol)
                        open_trade = None
                        initial_price = None
                        trade_in_trend = True  # Mark that a trade was made in this trend

                # NEW CONDITION: Wait for trend reversal before taking new trades
                if trade_in_trend and current_signal == previous_signal:
                    log.info("Waiting for trend reversal before taking a new trade.")
                    time.sleep(10)
                    continue
                elif trade_in_trend and current_signal != previous_signal:
                    log.info("Trend reversed. Ready to take a new trade.")
                    trade_in_trend = False  # Reset the flag after trend reversal

                if current_signal == "Buy":
                    if initial_price is None:
                        initial_price = current_price
                        log.info(f"Buy trend started at {initial_price}, waiting for +1 increase.")

                    elif current_price >= (initial_price + 1) and (open_trade is None or len(open_trade) == 0):
                        open_trade = []
                        entry_price = current_price  # Store the entry price separately
                        for _ in range(10):
                            if place_order(symbol, "Buy", lot_size):
                                open_trade.append({
                                    "action": "Buy",
                                    "entry_price": entry_price,
                                    "lot_size": lot_size
                                })
                        log.info(f"Opened 10 Buy trades at {entry_price}, waiting for +1 increase to exit.")

                    elif open_trade and current_price >= (entry_price + 1):  # Use entry_price for comparison
                        log.info(f"Price increased to {current_price}, closing all Buy trades.")
                        close_all_trades(symbol)
                        open_trade = None
                        initial_price = None
                        trade_in_trend = True  # Mark that a trade was made in this trend

                elif current_signal == "Sell":
                    if initial_price is None:
                        initial_price = current_price
                        log.info(f"Sell trend started at {initial_price}, waiting for -1 decrease.")

                    elif current_price <= (initial_price - 1) and (open_trade is None or len(open_trade) == 0):
                        open_trade = []
                        entry_price = current_price  # Store the entry price separately
                        for _ in range(10):
                            if place_order(symbol, "Sell", lot_size):
                                open_trade.append({
                                    "action": "Sell",
                                    "entry_price": entry_price,
                                    "lot_size": lot_size
                                })
                        log.info(f"Opened 10 Sell trades at {entry_price}, waiting for -1 decrease to exit.")

                    elif open_trade and current_price <= (entry_price - 1):  # Use entry_price for comparison
                        log.info(f"Price decreased to {current_price}, closing all Sell trades.")
                        close_all_trades(symbol)
                        open_trade = None
                        initial_price = None
                        trade_in_trend = True  # Mark that a trade was made in this trend

            time.sleep(10)

        except Exception as e:
            log.error(f"Error occurred: {e}")
            time.sleep(10)



@app.route("/")
def index():
    return render_template("index.html")  # Ensure index.html is in the "templates" folder


@app.route("/start", methods=["POST"])
def start_bot():
    try:
        global trading_active, trading_thread

        if trading_thread and trading_thread.is_alive():
            return jsonify({"status": "Trading bot is already running."})

        trading_active = True
        trading_thread = threading.Thread(target=trading_logic, daemon=True)
        trading_thread.start()

        return jsonify({"status": "Trading bot started."})
    except Exception as e:
        return jsonify({"status": "Error occurred.", "error": str(e)})


@app.route("/stop", methods=["POST"])
def stop_bot():
    try:
        global trading_active, trading_thread

        if trading_active:
            trading_active = False
            if trading_thread and trading_thread.is_alive():
                trading_thread.join(timeout=5)

            return jsonify({"status": "Trading bot stopped."})
        else:
            return jsonify({"status": "Trading bot is not running."})
    except Exception as e:
        return jsonify({"status": "Error occurred.", "error": str(e)})


@app.route("/status")
def status():
    return jsonify({"status": "running" if trading_active else "stopped"})


if __name__ == "__main__":
    # Define the URL to open
    url = "http://127.0.0.1:5000"

    # Open the browser directly
    webbrowser.open_new(url)

    # Start the Flask app
    app.run(debug=True)
