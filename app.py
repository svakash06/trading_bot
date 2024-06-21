from flask import Flask, render_template, request, redirect, url_for
import time
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import requests
import pyotp
from SmartApi import SmartConnect
import credentials
import json

app = Flask(__name__)

def authenticate():
    myotp = pyotp.TOTP(credentials.CD).now()
    obj = SmartConnect(api_key=credentials.API_KEY)
    data = obj.generateSession(credentials.USER_NAME, credentials.PWD, myotp)
    refreshToken = data['data']['refreshToken']
    feedToken = obj.getfeedToken()
    return obj, refreshToken, feedToken

def load_holidays(holidays_csv):
    holidays_df = pd.read_csv(holidays_csv)
    holidays = set(holidays_df['Date'].tolist())
    return holidays

def is_holiday(holidays):
    today = datetime.now().strftime('%Y-%m-%d')
    return today in holidays

def is_market_open(holidays):
    now = datetime.now()
    if is_holiday(holidays):
        return False
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close

def get_live_data(obj, exchange, tradingsymbol, symboltoken):
    try:
        ltp_data = obj.ltpData(exchange, tradingsymbol, symboltoken)
        return ltp_data['data']
    except Exception as e:
        print(f"Error fetching live data: {e}")
        return None

def get_historical_data(obj, params):
    try:
        formatted_params = {
            "exchange": params["exchange"],
            "tradingsymbol": params["tradingsymbol"],
            "symboltoken": params["symboltoken"],
            "interval": params["interval"],
            "fromdate": params["start_time"].strftime("%Y-%m-%d %H:%M"),
            "todate": params["end_time"].strftime("%Y-%m-%d %H:%M")
        }
        candle_data = obj.getCandleData(formatted_params)
        data = candle_data['data']
        columns = ['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume']
        df = pd.DataFrame(data, columns=columns)
        df['DateTime'] = pd.to_datetime(df['DateTime'])
        df.set_index('DateTime', inplace=True)
        return df
    except Exception as e:
        print(f"Error fetching historical data: {e}")
        return None

def set_stoploss_and_trigger(current_price, stoploss_percent, trigger_percent):
    stoploss_price = int(current_price * (1 - stoploss_percent / 100))
    trigger_price = int(current_price * (1 + trigger_percent / 100))
    return stoploss_price, trigger_price

def get_token_info(csv_filename, exch_seg, instrumenttype, symbol, strike_price, pe_ce):
    df = pd.read_csv(csv_filename, low_memory=False)
    strike_price = strike_price * 100

    if exch_seg == 'NSE':
        eq_df = df[(df['exch_seg'] == 'NSE') & (df['symbol'].str.contains('EQ'))]
        return eq_df[eq_df['name'] == symbol]
    elif exch_seg == 'NFO' and ((instrumenttype == 'FUTSTK') or (instrumenttype == 'FUTIDX')):
        return df[(df['exch_seg'] == 'NFO') & (df['instrumenttype'] == instrumenttype) & (df['name'] == symbol)].sort_values(by=['expiry'])
    elif exch_seg == 'NFO' and (instrumenttype == 'OPTSTK' or instrumenttype == 'OPTIDX'):
        return df[(df['exch_seg'] == 'NFO') & (df['instrumenttype'] == instrumenttype) & (df['name'] == symbol) & (df['strike'] == strike_price) & (df['symbol'].str.endswith(pe_ce))].sort_values(by=['expiry'])

def place_order(obj, tradingsymbol, symboltoken, quantity, buy_sell, exch_seg):
    try:
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": str(tradingsymbol),
            "symboltoken": symboltoken,
            "transactiontype": buy_sell,
            "exchange": exch_seg,
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": 0,
            "squareoff": "0",
            "stoploss": "0",
            "quantity": int(quantity)
        }
        order_id = obj.placeOrder(order_params)
        return order_id
    except json.JSONDecodeError:
        print("Couldn't parse the JSON response received from the server. The response might be empty or malformed.")
        return None
    except Exception as e:
        print(f"Error while placing order: {e}")
        return None

def calculate_rsi(data, period=14):
    if len(data) < period + 1:
        return None

    delta = np.diff(data)
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = np.convolve(gain, np.ones(period) / period, 'valid')
    avg_loss = np.convolve(loss, np.ones(period) / period, 'valid')
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi[-1] if len(rsi) > 0 else None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/token_info', methods=['GET', 'POST'])
def token_info():
    if request.method == 'POST':
        csv_file = 'OpenAPIScripMaster.csv'
        exch_seg = request.form['exchange_segment']
        instrumenttype = request.form['instrument_type']
        symbol = request.form['symbol']
        strike_price = int(request.form['strike_price'])
        option_type = request.form['option_type']
        
        token_info = get_token_info(csv_file, exch_seg, instrumenttype, symbol, strike_price, option_type)
        if token_info is None or token_info.empty:
            return render_template('error.html', message="Failed to get token information.")
        
        df = pd.DataFrame(token_info)
        new_index = range(0, len(df))
        df.index = new_index
        return render_template('token_info.html', tables=[df.to_html(classes='data')], titles=df.columns.values)
    
    return render_template('token_info_form.html')

@app.route('/auto_trade', methods=['POST'])
def auto_trade():
    index = int(request.form['index'])
    holidays_csv = 'holidays_list_bse_nse.csv'
    csv_file = 'OpenAPIScripMaster.csv'
    ExchangeSegment = "NFO"
    InstrumentType = "OPTIDX"
    Symbol = "BANKNIFTY"
    StrikePrice = 50500
    option_type = "CE"
    
    obj, refreshToken, feedToken = authenticate()
    holidays = load_holidays(holidays_csv)
    if not is_market_open(holidays):
        return render_template('error.html', message="Market closed. Exiting.")
    
    tokens_info = get_token_info(csv_file, ExchangeSegment, InstrumentType, Symbol, StrikePrice, option_type)
    if tokens_info is None or tokens_info.empty:
        return render_template('error.html', message="Failed to get token information.")

    token_info = tokens_info.iloc[index]
    symbol = token_info['symbol']
    token = token_info['token']
    exch_seg = token_info['exch_seg']
    quantity = token_info['lotsize']
    profit_target = 0.1
    sell_threshold = 70
    buy_threshold = 30

    start_date = datetime.now() - timedelta(days=30)
    end_date = datetime.now()
    params = {
        "exchange": exch_seg,
        "tradingsymbol": symbol,
        "symboltoken": token,
        "interval": "FIVE_MINUTE",
        "start_time": start_date,
        "end_time": end_date
    }

    get_price = get_live_data(obj, exch_seg, symbol, token)
    if get_price is None:
        return render_template('error.html', message="Failed to fetch initial live data.")
    
    current_price = get_price['ltp']
    stop_loss, trigger = set_stoploss_and_trigger(current_price, 5, 10)
    
    max_retry = 3
    retry_count = 0
    while retry_count < max_retry:
        candle_data = get_historical_data(obj, params)
        if candle_data is not None and not candle_data.empty:
            break
        else:
            retry_count += 1
            time.sleep(60)
            
    if retry_count == max_retry:
        return render_template('error.html', message="Failed to fetch historical data after multiple retries.")
        
    close_prices = candle_data['Close'].values
    rsi = calculate_rsi(close_prices)
    
    while True:
        live_data = get_live_data(obj, exch_seg, symbol, token)
        if live_data is None:
            time.sleep(60)
            continue
        
        current_price = live_data['ltp']
        close_prices = np.append(close_prices, current_price)
        rsi = calculate_rsi(close_prices)
        if rsi is None:
            time.sleep(60)
            continue
        
        if not position_held:
            if rsi <= buy_threshold:
                buy_order_id = place_order(obj, symbol, token, quantity, "BUY", exch_seg)
                if buy_order_id:
                    position_held = True
        else:
            if rsi >= sell_threshold:
                sell_order_id = place_order(obj, symbol, token, quantity, "SELL", exch_seg)
                if sell_order_id:
                    position_held = False
                    break
        time.sleep(60)
        
    return render_template('success.html', message="Trading completed successfully.")

if __name__ == '__main__':
    app.run(debug=True)
